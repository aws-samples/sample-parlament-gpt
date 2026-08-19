"""The AgentCore Gateway Lambda-target contract, shared by all fetcher Lambdas.

Verified against the AgentCore devguide "Lambda function input format" plus official
awslabs handler samples (docs/multi-gov/ADR-001 §0, gateway-contract recon):

  * The ``event`` is a FLAT map of the tool's inputSchema properties to values — the MCP
    ``params.arguments`` object, unwrapped. There is NO envelope: not ``{"body": ...}``, not
    API-Gateway-proxy shaped, not base64.
  * The tool name is NOT in the event. It is in
    ``context.client_context.custom["bedrockAgentCoreToolName"]``, arriving prefixed as
    ``{target_name}___{tool_name}`` (three underscores). Target names cannot contain
    underscores, so ``split("___")[1]`` is unambiguous.
  * ``context.client_context`` can be ``None`` (e.g. a plain ``aws lambda invoke`` without
    ClientContext), so it must be guarded — otherwise unit tests diverge from production.
  * The handler returns a bare JSON-serializable value; the Gateway wraps it into an MCP
    ``CallToolResult``. We do NOT construct MCP envelopes, ``content`` arrays, or ``isError``.
  * ``statusCode``/``body`` dicts are NOT interpreted by the Gateway — they are handed to the
    model verbatim as a *successful* result. So we return clean domain payloads and raise on
    genuine faults (which surface as ToolExecutionError/500 in gateway metrics).

Usage in an adapter's ``handler.py``::

    from gov_debates.gateway.handler import dispatch, bad_argument

    def search_debates(args): ...       # returns a results-envelope dict
    def get_debate_text(args): ...

    TOOLS = {"search_debates": search_debates, "get_debate_text": get_debate_text}

    def lambda_handler(event, context):
        return dispatch(event, context, TOOLS)
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

TOOL_NAME_KEY = "bedrockAgentCoreToolName"

# A tool implementation takes the flat arguments dict and returns a JSON-serializable value.
ToolFn = Callable[[Mapping[str, Any]], Any]


class ToolDispatchError(RuntimeError):
    """Raised for unrecoverable dispatch faults (unknown tool, missing tool name).

    Raising (rather than returning) makes the Gateway surface a ToolExecutionError, so genuine
    misconfiguration shows up in metrics instead of being silently handed to the model.
    """


def extract_tool_name(context: Any) -> str:
    """Return the un-prefixed tool name from the Lambda context.

    Reads ``context.client_context.custom['bedrockAgentCoreToolName']`` and strips the
    ``{target}___`` prefix. Raises :class:`ToolDispatchError` if the context is missing or the
    key is absent.
    """
    client_context = getattr(context, "client_context", None)
    if client_context is None:
        raise ToolDispatchError(
            "context.client_context is None; the tool name is unavailable. This Lambda must be "
            "invoked through the AgentCore Gateway."
        )
    custom = getattr(client_context, "custom", None) or {}
    extended = custom.get(TOOL_NAME_KEY)
    if not extended:
        raise ToolDispatchError(f"{TOOL_NAME_KEY} missing from client_context.custom")
    # Target names contain no underscores, so the first "___" is the delimiter.
    return extended.split("___", 1)[1] if "___" in extended else extended


def dispatch(event: Any, context: Any, tools: Mapping[str, ToolFn]) -> Any:
    """Route a Gateway invocation to the right tool implementation.

    ``event`` is the flat arguments map. Resolves the tool name from ``context`` and calls
    ``tools[name](event)``. Raises :class:`ToolDispatchError` for an unknown tool.
    """
    name = extract_tool_name(context)
    fn = tools.get(name)
    if fn is None:
        raise ToolDispatchError(
            f"unrecognized tool {name!r}; known tools: {sorted(tools)}"
        )
    args = event if isinstance(event, Mapping) else {}
    return fn(args)


def bad_argument(message: str, **fields: Any) -> dict[str, Any]:
    """Build a SOFT error payload the model can read and correct.

    Returned (not raised) for anything the model could fix — a missing required argument, an
    unparseable date. The Gateway delivers it as a successful tool result whose text describes
    the problem, so the model can retry with corrected arguments. Includes an empty
    ``results`` list so downstream ``results``-key detection still sees a well-formed shape.
    """
    payload = {"error": "bad_argument", "message": message, "results": [], "total": 0}
    payload.update(fields)
    return payload


def no_results(jurisdiction: str, **fields: Any) -> dict[str, Any]:
    """Build an empty but valid results envelope (a legitimate 'nothing matched')."""
    payload = {"results": [], "total": 0, "jurisdiction": jurisdiction, "truncated": False}
    payload.update(fields)
    return payload
