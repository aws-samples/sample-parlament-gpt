"""A faithful fake Strands Model for end-to-end tests (no Bedrock, no network).

It implements the Strands ``Model`` streaming contract and simulates a tool-using model:
  * For an on-topic question it emits a tool-use turn calling the Gateway search tool
    (``germany___search_debates``), then (after receiving the tool result) emits a grounded
    answer that cites the speaker/group/date from the tool output.
  * For an off-topic question it emits the fixed refusal, with NO tool call.

This lets us drive the real Strands Agent event loop against a real in-process MCP Gateway
end to end (see mcp_gateway_fixture). The tool name is discovered from ``tool_specs`` so the
fake stays correct if the Gateway prefix changes.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterable

from strands.models import Model

from parlamentgpt_agent.config import REFUSAL_MESSAGE

_OFFTOPIC_HINTS = ("python", "programम", "programmier", "hauptstadt", "witz", "kongress", "rezept")
_ONTOPIC_HINTS = ("bundestag", "rede", "plenar", "wahlperiode", "fraktion", "debatte")


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            for block in msg.get("content", []):
                if isinstance(block, dict) and "text" in block:
                    return block["text"]
    return ""


def _has_tool_result(messages: list[dict[str, Any]]) -> bool:
    for msg in messages:
        for block in msg.get("content", []) if isinstance(msg, dict) else []:
            if isinstance(block, dict) and "toolResult" in block:
                return True
    return False


class FakeBedrockModel(Model):
    """Deterministic stand-in for the Bedrock model."""

    def __init__(self) -> None:
        self._config: dict[str, Any] = {"model_id": "fake.bedrock-model"}
        self.invocations: list[dict[str, Any]] = []

    # --- Model config plumbing ---
    def get_config(self) -> dict[str, Any]:
        return self._config

    def update_config(self, **kwargs: Any) -> None:
        self._config.update(kwargs)

    async def structured_output(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise NotImplementedError

    # --- The streaming contract the Agent event loop drives ---
    async def stream(
        self,
        messages: list[dict[str, Any]],
        tool_specs: list[Any] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[dict[str, Any]]:
        self.invocations.append({"messages": messages, "tool_specs": tool_specs})
        question = _last_user_text(messages).lower()
        on_topic = any(h in question for h in _ONTOPIC_HINTS) and not any(
            h in question for h in _OFFTOPIC_HINTS
        )

        # Turn 2: we already have tool results -> produce the grounded answer.
        if _has_tool_result(messages):
            answer = self._grounded_answer(messages)
            async for ev in _text_turn(answer):
                yield ev
            return

        # Turn 1: off-topic -> refuse without any tool call.
        if not on_topic:
            async for ev in _text_turn(REFUSAL_MESSAGE):
                yield ev
            return

        # Turn 1: on-topic -> call the Gateway search tool. Discover its exact (prefixed) name
        # from the tool specs the agent advertised, defaulting to the Germany search tool.
        tool_name = _search_tool_name(tool_specs)
        tool_input = json.dumps({"speaker": "Hubertus Heil", "term": "21"})
        async for ev in _tool_use_turn(tool_name, tool_input):
            yield ev

    def _grounded_answer(self, messages: list[dict[str, Any]]) -> str:
        # Extract the first cited result from the tool output to ground the answer.
        for msg in messages:
            for block in msg.get("content", []) if isinstance(msg, dict) else []:
                tr = block.get("toolResult") if isinstance(block, dict) else None
                if not tr:
                    continue
                for item in tr.get("content", []):
                    data = _tool_item_json(item)
                    if isinstance(data, dict) and data.get("results"):
                        r = data["results"][0]
                        return (
                            f"In the {r.get('jurisdiction_label')}, {r.get('speaker')} "
                            f"({r.get('group')}) spoke on {r.get('date')} "
                            f"(session {r.get('session_ref')}). Source: {r.get('source_url')}"
                        )
                    if isinstance(data, dict):
                        return "No relevant debates were found on this topic."
        return "No debates were found on this topic."


def _search_tool_name(tool_specs: list[Any] | None) -> str:
    """Find the advertised search tool name (handles the Gateway prefix), default to Germany."""
    for spec in tool_specs or []:
        name = spec.get("name") if isinstance(spec, dict) else getattr(spec, "name", None)
        if isinstance(name, str) and name.endswith("search_debates"):
            return name
    return "germany___search_debates"


def _tool_item_json(item: Any) -> Any:
    """Strands serialises tool results either as {'json': {...}} or {'text': '<json>'}."""
    if not isinstance(item, dict):
        return None
    if isinstance(item.get("json"), dict):
        return item["json"]
    if isinstance(item.get("text"), str):
        try:
            return json.loads(item["text"])
        except json.JSONDecodeError:
            return None
    return None


async def _text_turn(text: str) -> AsyncIterable[dict[str, Any]]:
    yield {"messageStart": {"role": "assistant"}}
    yield {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}
    yield {"contentBlockStop": {"contentBlockIndex": 0}}
    yield {"messageStop": {"stopReason": "end_turn"}}


async def _tool_use_turn(name: str, input_json: str) -> AsyncIterable[dict[str, Any]]:
    yield {"messageStart": {"role": "assistant"}}
    yield {
        "contentBlockStart": {
            "contentBlockIndex": 0,
            "start": {"toolUse": {"toolUseId": "tool-1", "name": name}},
        }
    }
    yield {"contentBlockDelta": {"delta": {"toolUse": {"input": input_json}}, "contentBlockIndex": 0}}
    yield {"contentBlockStop": {"contentBlockIndex": 0}}
    yield {"messageStop": {"stopReason": "tool_use"}}
