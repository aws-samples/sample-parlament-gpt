"""Amazon Bedrock AgentCore Runtime entrypoint.

Packaged as a container and deployed to AgentCore Runtime. The runtime calls our handler
with the user payload; we run the Strands agent and return the answer.
"""
from __future__ import annotations

import asyncio
import ast
import logging
import re
import threading
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from .agent import build_agent
from .config import REFUSAL_MESSAGE
from .gateway import ensure_session

log = logging.getLogger(__name__)

app = BedrockAgentCoreApp()

# Build once per container (cold start) and reuse. build_agent starts the shared Gateway MCP
# client and returns it alongside the agent so we can reconnect it per request if the session
# died (a 401/blip tears down the whole session — see gateway.ensure_session).
#
# Guarded so the module is importable without a live Gateway (tests inject their own agent +
# client). A production container always has GATEWAY_MCP_URL set and will fail fast on a real
# connection error at first request via ensure_session.
try:
    _agent, _gateway_client = build_agent()
except Exception:  # pragma: no cover - only hit when no Gateway is reachable at import
    _agent, _gateway_client = None, None

MAX_INPUT_CHARS = 2000

# Serialises access to the module-global agent. The agent instance is reused across requests
# and its message list is reset per request, which is only correct while a single
# conversation runs at a time. AgentCore currently serves one request per container, so this
# lock is normally uncontended; it exists so that a concurrent invocation waits instead of
# interleaving two users' messages (threat model M1). Held across the whole model/tool loop.
_AGENT_LOCK = threading.Lock()

# Tool names exposed by the Gateway are prefixed "{jurisdiction}___{tool}". The default search
# tool the text-fallback recovery path targets when a model writes a call as plain text.
_DEFAULT_SEARCH_JURISDICTION = "germany"


@app.entrypoint
def invoke(payload: dict[str, Any]) -> Any:
    prompt = (payload or {}).get("prompt", "")
    stream = bool((payload or {}).get("stream"))
    history = _coerce_history((payload or {}).get("history"))

    if not isinstance(prompt, str) or not prompt.strip():
        if stream:
            return _stream_refusal()
        return {"answer": REFUSAL_MESSAGE, "sources": [], "steps": []}
    if len(prompt) > MAX_INPUT_CHARS:
        prompt = prompt[:MAX_INPUT_CHARS]

    # Reconnect the Gateway MCP session if it died since the last request. Cheap when healthy;
    # the only thing standing between a single 401 and a permanently dead container.
    ensure_session(_gateway_client)

    if stream:
        # Live SSE: emit reasoning + tool calls as they happen, then the final answer.
        return _invoke_stream(prompt, history)

    # Stateless per request: history is supplied by the caller (never persisted on the
    # server), so prior turns inform the answer without leaking between users/sessions.
    #
    # The lock enforces what the reset assumes: one conversation on the shared agent at a
    # time. Without it, two concurrent requests in the same container would interleave
    # their messages and could surface another user's turns (threat model M1/I8).
    with _AGENT_LOCK:
        _agent.messages.clear()
        _agent.messages.extend(history)
        history_len = len(history)
        result = _agent(prompt)
        answer = _extract_answer_text(result)
        messages = _agent.messages[history_len:]
    sources = _extract_sources(messages)
    fallback = _run_text_tool_call(answer, prompt, sources)
    if fallback:
        answer = fallback["answer"]
        sources = fallback["sources"]
        return {
            "answer": answer,
            "sources": sources,
            "steps": fallback["steps"],
        }
    # If the guardrail intervened, surface it as a trace step and return the refusal.
    steps = [{"type": "model_call", "model": _model_id()}]
    steps.extend(_extract_steps(messages, answer))
    metrics_step = _metrics_step(result)
    if metrics_step:
        steps.append(metrics_step)
    guardrail_step = _detect_guardrail(result, answer)
    if guardrail_step:
        steps.append(guardrail_step)
        answer = REFUSAL_MESSAGE
    elif not answer.strip():
        answer = REFUSAL_MESSAGE
    return {
        "answer": answer,
        "sources": sources,
        "steps": steps,
    }


# Cap how much prior conversation a caller may replay into context.
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 4000


def _coerce_history(raw: Any) -> list[dict[str, Any]]:
    """Normalise caller-supplied prior turns into Strands messages.

    Accepts a list of ``{"role": "user"|"assistant", "text": str}`` and returns the most
    recent turns as ``{"role", "content": [{"text"}]}``. Tool calls/results are intentionally
    NOT replayed — only the visible conversation — to keep context compact and avoid
    dangling tool-use references.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        text = item.get("text")
        if role not in ("user", "assistant") or not isinstance(text, str) or not text.strip():
            continue
        out.append({"role": role, "content": [{"text": text[:MAX_HISTORY_CHARS]}]})
    # A valid history must start with a user turn and not duplicate the new prompt.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


async def _stream_refusal():
    yield {"type": "answer", "answer": REFUSAL_MESSAGE, "sources": []}


async def _invoke_stream(prompt: str, history: list[dict[str, Any]] | None = None):
    """Async generator yielding step events for live display, then a final answer event.

    Event shapes (each delivered as one SSE ``data:`` line by AgentCore):
      {"type": "reasoning",   "text": str}
      {"type": "tool_call",   "tool": str, "input": dict}
      {"type": "tool_result", "count": int | None}
      {"type": "answer",      "answer": str, "sources": list}
    """
    import json

    # Same serialisation as the non-streaming path (see _AGENT_LOCK). Acquired off the event
    # loop so waiting for a concurrent request never blocks it; released in the finally
    # below, which also runs when the client abandons the stream.
    await asyncio.to_thread(_AGENT_LOCK.acquire)
    try:
        async for ev in _stream_events(prompt, history, model_id=_model_id()):
            yield ev
    finally:
        _AGENT_LOCK.release()


async def _stream_events(prompt: str, history: list[dict[str, Any]] | None, *, model_id: str):
    """The streaming run itself; the caller holds _AGENT_LOCK for its whole duration."""
    import json

    _agent.messages.clear()
    _agent.messages.extend(history or [])
    history_len = len(_agent.messages)
    result_obj: Any = None
    try:
        async for ev in _agent.stream_async(prompt):
            if not isinstance(ev, dict):
                continue
            if "result" in ev:
                result_obj = ev["result"]
                continue
            # Raw Bedrock stream chunks (ConverseStream shapes) pass through under "event".
            # They power the under-the-hood view: one model_call per model invocation in
            # the tool loop, its stop reason, token usage and latency, and live guardrail
            # redactions — so even an off-topic prompt shows its path through the stack.
            chunk = ev.get("event")
            if isinstance(chunk, dict):
                if "messageStart" in chunk:
                    yield {"type": "model_call", "model": model_id}
                elif "messageStop" in chunk:
                    stop = chunk["messageStop"].get("stopReason") if isinstance(chunk["messageStop"], dict) else None
                    yield {"type": "model_stop", "stop_reason": stop}
                elif "metadata" in chunk and isinstance(chunk["metadata"], dict):
                    usage = chunk["metadata"].get("usage") or {}
                    metrics = chunk["metadata"].get("metrics") or {}
                    yield {
                        "type": "model_metrics",
                        "input_tokens": usage.get("inputTokens"),
                        "output_tokens": usage.get("outputTokens"),
                        "total_tokens": usage.get("totalTokens"),
                        "latency_ms": metrics.get("latencyMs"),
                    }
                elif "redactContent" in chunk:
                    yield {
                        "type": "guardrail",
                        "action": "content_redacted",
                        "detail": "The guardrail redacted content in this response stream.",
                    }
                continue
            msg = ev.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            content = msg.get("content", [])
            if role == "assistant":
                tool_uses = [b for b in content if isinstance(b, dict) and b.get("toolUse")]
                # Only narration that precedes a tool call is "thinking"; the final
                # answer (a text-only assistant turn) arrives via the result event.
                if tool_uses:
                    for b in content:
                        if isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip():
                            yield {"type": "reasoning", "text": b["text"].strip()}
                    for b in tool_uses:
                        tu = b["toolUse"]
                        yield {
                            "type": "tool_call",
                            "tool": tu.get("name"),
                            "input": tu.get("input", {}),
                        }
            elif role == "user":
                for b in content:
                    tr = b.get("toolResult") if isinstance(b, dict) else None
                    if not tr:
                        continue
                    count, raw, truncated = _summarise_tool_result(tr)
                    yield {"type": "tool_result", "count": count, "raw": raw, "truncated": truncated}
    except Exception as exc:  # pragma: no cover - surfaced to the client as an answer
        err_msg = str(exc)
        if "guardrail" in err_msg.lower() or "redacted" in err_msg.lower():
            # Make the intervention visible in the trace instead of failing silently;
            # the user-facing answer stays the standard refusal.
            yield {"type": "guardrail", "action": "stream_error", "detail": err_msg[:200]}
            yield {"type": "answer", "answer": REFUSAL_MESSAGE, "sources": []}
        else:
            # Cap and classify like the guardrail branch above: SDK exception strings can
            # carry ARNs, model ids and the Gateway URL, and this string goes verbatim into
            # the client stream (threat model I5). Full detail goes to the service log only.
            log.exception("agent stream failed")
            yield {
                "type": "answer",
                "answer": f"Error: {type(exc).__name__}: {err_msg[:200]}",
                "sources": [],
            }
        return

    answer = _extract_answer_text(result_obj) if result_obj is not None else ""
    sources = _extract_sources(_agent.messages[history_len:])
    fallback = _run_text_tool_call(answer, prompt, sources)
    if fallback:
        yield {
            "type": "tool_call",
            "tool": _SEARCH_TOOL_SUFFIX,
            "jurisdiction": fallback["steps"][0].get("jurisdiction"),
            "input": fallback["input"],
        }
        yield {"type": "tool_result", "count": len(fallback["sources"])}
        yield {"type": "answer", "answer": fallback["answer"], "sources": fallback["sources"]}
        return
    guardrail_step = _detect_guardrail(result_obj, answer)
    if guardrail_step:
        yield guardrail_step
        answer = REFUSAL_MESSAGE
    elif not answer.strip():
        answer = REFUSAL_MESSAGE
    yield {"type": "answer", "answer": answer, "sources": sources}


_REDACTION_MARKER = "[Assistant output redacted.]"


def _detect_guardrail(result: Any, answer: str) -> dict[str, Any] | None:
    """Detect a Bedrock Guardrail intervention on a finished run.

    Two observable signals: the redaction marker the guardrail writes into the output,
    and a guardrail stop reason on the result object. Returns a trace event for the UI
    (the transparent pipeline view), or None.
    """
    if _REDACTION_MARKER in (answer or ""):
        return {"type": "guardrail", "action": "output_redacted",
                "detail": "The guardrail redacted the model output; the standard refusal is returned instead."}
    stop_reason = getattr(result, "stop_reason", None)
    if isinstance(stop_reason, str) and "guardrail" in stop_reason.lower():
        return {"type": "guardrail", "action": stop_reason,
                "detail": "The guardrail stopped this response; the standard refusal is returned instead."}
    return None


# Tool names the recovery path recognises when a model writes a call as plain text. Kept in
# sync with the Gateway search tool suffix. This path exists for models that emit tool calls
# as prose instead of structured tool-use blocks.
_SEARCH_TOOL_SUFFIX = "search_debates"
# Match either a bare "search_debates(...)" or a Gateway-prefixed "germany___search_debates(...)".
_TOOL_CALL_RE = re.compile(r"(?:(?P<jur>\w+)___)?search_debates\s*\((?P<args>.*?)\)", re.DOTALL)


def _run_text_tool_call(answer: str, prompt: str, existing_sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Recover when a model writes a tool call as text instead of using tool-use blocks.

    Re-plumbed from the old local-@tool reach-in to a Gateway ``call_tool_sync``: the local
    functions no longer exist. The kwarg allowlist is derived from the current search tool
    schema; unknown kwargs fail closed (return None) so a stale allowlist silently disables
    recovery rather than sending garbage to the Gateway.
    """
    if existing_sources:
        return None
    parsed = _parse_search_tool_call(answer)
    if parsed is None:
        return None
    jurisdiction, tool_input = parsed
    tool_input.setdefault("query", prompt)
    tool_name = f"{jurisdiction}___{_SEARCH_TOOL_SUFFIX}"
    result = _call_gateway_search(tool_name, tool_input)
    sources = result.get("results", []) if isinstance(result, dict) else []
    if not isinstance(sources, list):
        sources = []
    return {
        "answer": _format_search_answer(sources),
        "sources": sources,
        "input": tool_input,
        "steps": [
            {"type": "tool_call", "tool": _SEARCH_TOOL_SUFFIX, "jurisdiction": jurisdiction, "input": tool_input},
            {"type": "tool_result", "count": len(sources)},
        ],
    }


# Kwargs accepted by the Gateway search_debates tool (mirrors its inputSchema). Fail-closed:
# any kwarg outside this set aborts recovery.
_SEARCH_ALLOWED_KWARGS = {
    "query",
    "speaker",
    "date_start",
    "date_end",
    "term",
    "chamber",
    "cursor",
    "max_results",
    "year",
}


def _parse_search_tool_call(answer: str) -> tuple[str, dict[str, Any]] | None:
    """Parse a plain-text search tool call. Returns (jurisdiction, kwargs) or None."""
    if not isinstance(answer, str) or _SEARCH_TOOL_SUFFIX not in answer:
        return None
    match = _TOOL_CALL_RE.search(answer)
    if not match:
        return None
    jurisdiction = match.group("jur") or _DEFAULT_SEARCH_JURISDICTION
    try:
        # RecursionError: ast.parse on model-generated text — pathological nesting must
        # degrade to "no recovery" instead of killing the request.
        expr = ast.parse(f"f({match.group('args')})", mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return None
    if not isinstance(expr, ast.Call) or expr.args:
        return None
    out: dict[str, Any] = {}
    for kw in expr.keywords:
        if kw.arg not in _SEARCH_ALLOWED_KWARGS:
            return None
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, TypeError):
            return None
    # Convenience: a bare year expands to a date range (the schema has no `year`).
    year = out.pop("year", None)
    if isinstance(year, int):
        out.setdefault("date_start", f"{year:04d}-01-01")
        out.setdefault("date_end", f"{year:04d}-12-31")
    out.setdefault("max_results", 20)
    return jurisdiction, out


def _call_gateway_search(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Invoke a Gateway search tool by name and return its parsed JSON result envelope."""
    try:
        result = _gateway_client.call_tool_sync(
            tool_use_id="text-fallback",
            name=tool_name,
            arguments=tool_input,
        )
    except Exception:
        return {}
    return _parse_tool_result(result)


def _parse_tool_result(result: Any) -> dict[str, Any]:
    """Extract the JSON envelope from an MCP tool result (the {'text': '<json>'} shape)."""
    import json

    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    for item in content or []:
        text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
        if isinstance(text, str):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return {}


def _format_search_answer(sources: list[dict[str, Any]]) -> str:
    if not sources:
        return "I found no matching speeches in the parliamentary records."
    lines = ["I found the following speeches in the parliamentary records:"]
    for idx, source in enumerate(sources, start=1):
        title = source.get("title") or "Speech"
        date = source.get("date") or "no date"
        speaker = source.get("speaker") or "no speaker given"
        session = source.get("session_ref")
        url = source.get("source_url")
        detail = f"{idx}. {date}: {speaker} - {title}"
        if session:
            detail += f" (session {session})"
        if url:
            detail += f"\n   Source: {url}"
        lines.append(detail)
    return "\n".join(lines)


# Cap for raw payload previews in the trace: enough to show the real Lambda/Gateway reply
# without blowing up SSE frames or the persisted session size.
_RAW_PREVIEW_CHARS = 4000


def _model_id() -> str:
    """Best-effort model id for the trace (never raises)."""
    try:
        cfg = _agent.model.get_config()  # type: ignore[union-attr]
        return str(cfg.get("model_id") or "unknown")
    except Exception:
        return "unknown"


def _summarise_tool_result(tool_result: dict[str, Any]) -> tuple[int | None, str | None, bool]:
    """Summarise a toolResult block: (result count, raw JSON preview, truncated?).

    The raw preview is the actual payload the Gateway Lambda returned (pretty-printed),
    capped at _RAW_PREVIEW_CHARS — the "under the hood" view for debug mode.
    """
    import json

    count: int | None = None
    raw: str | None = None
    for item in tool_result.get("content", []):
        data = None
        if isinstance(item, dict) and isinstance(item.get("json"), dict):
            data = item["json"]
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            try:
                data = json.loads(item["text"])
            except json.JSONDecodeError:
                raw = raw or item["text"]
                continue
        if isinstance(data, dict):
            raw = json.dumps(data, ensure_ascii=False, indent=2)
            if isinstance(data.get("results"), list):
                count = len(data["results"])
    truncated = bool(raw and len(raw) > _RAW_PREVIEW_CHARS)
    if raw and truncated:
        raw = raw[:_RAW_PREVIEW_CHARS]
    return count, raw, truncated


def _metrics_step(result: Any) -> dict[str, Any] | None:
    """Accumulated usage/latency for the non-streaming path (from the Strands result)."""
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) or {}
    latency = (getattr(metrics, "accumulated_metrics", None) or {}).get("latencyMs")
    if not usage and latency is None:
        return None
    return {
        "type": "model_metrics",
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "total_tokens": usage.get("totalTokens"),
        "latency_ms": latency,
    }


def _extract_steps(messages: Any, final_answer: str) -> list[dict[str, Any]]:
    """Surface the agent's reasoning trace for the UI: intermediate narration the model
    produced and each search it ran (with the query parameters and result count).

    The final answer text is excluded so it is not duplicated as a 'thinking' step.
    """
    import json

    steps: list[dict[str, Any]] = []
    try:
        for msg in messages or []:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                if role == "assistant" and isinstance(block.get("text"), str):
                    text = block["text"].strip()
                    if text and text != final_answer:
                        steps.append({"type": "reasoning", "text": text})
                elif isinstance(block.get("toolUse"), dict):
                    tu = block["toolUse"]
                    steps.append(
                        {
                            "type": "tool_call",
                            "tool": tu.get("name"),
                            "input": tu.get("input", {}),
                        }
                    )
                elif isinstance(block.get("toolResult"), dict):
                    count, raw, truncated = _summarise_tool_result(block["toolResult"])
                    steps.append({"type": "tool_result", "count": count, "raw": raw, "truncated": truncated})
    except Exception as exc:  # nosec B110 - trace extraction is best-effort; a malformed block must not fail the answer
        # ERROR, not WARNING: operators alarming on ERROR must see the paths that degrade
        # answer fidelity (threat model T10 / asset A6).
        log.error("trace extraction failed on a malformed block: %s", exc)
    return steps


def _extract_answer_text(result: Any) -> str:
    """Pull plain assistant text out of a Strands result.

    ``result.message`` is a dict like ``{'role': 'assistant', 'content': [{'text': ...}]}``.
    We concatenate the text blocks; fall back to ``str`` only as a last resort.
    """
    message = getattr(result, "message", result)
    if isinstance(message, dict):
        parts = [
            block["text"]
            for block in message.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        if parts:
            return "\n".join(parts).strip()
    return str(message)


def _extract_sources(messages: Any) -> list[dict[str, Any]]:
    """Pull citable sources from tool results in the conversation, if available.

    Strands serialises a tool result either as {'json': {...}} or as {'text': '<json>'}.
    """
    import json

    sources: list[dict[str, Any]] = []
    try:
        for msg in messages or []:
            for block in msg.get("content", []) if isinstance(msg, dict) else []:
                tr = block.get("toolResult") if isinstance(block, dict) else None
                if not tr:
                    continue
                for item in tr.get("content", []):
                    data = None
                    if isinstance(item, dict) and isinstance(item.get("json"), dict):
                        data = item["json"]
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        try:
                            data = json.loads(item["text"])
                        except json.JSONDecodeError:
                            data = None
                    if isinstance(data, dict) and isinstance(data.get("results"), list):
                        sources.extend(data["results"])
    except Exception as exc:  # nosec B110 - source extraction is best-effort; a malformed block must not fail the answer
        # ERROR, because empty sources also triggers the text-fallback recovery path, which
        # would silently replace a real answer with the canned listing (threat model T10 /
        # asset A6 — the worst-case product failure must not hide below alarm level).
        log.error("source extraction failed on a malformed block: %s", exc)
    return sources


if __name__ == "__main__":
    app.run()
