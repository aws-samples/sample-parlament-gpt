"""End-to-end tests: the REAL Strands agent event loop + REAL MCPClient over REAL sockets,
against an in-process MCP server standing in for the AgentCore Gateway. Only the LLM and the
upstream parliament API are faked.

These prove:
  1. On-topic question -> agent calls the Gateway search tool over MCP -> returns a grounded,
     cited answer built from the normalized SpeechResult envelope.
  2. Off-topic question -> agent refuses verbatim and never calls a tool.
  3. The AgentCore `invoke` entrypoint returns {answer, sources} with real citations.
  4. The text-fallback recovery path calls the Gateway tool (not a deleted local function).
"""
from __future__ import annotations

import pytest

from strands import Agent
from parlamentgpt_agent.config import REFUSAL_MESSAGE
from parlamentgpt_agent.gateway import list_all_tools
from parlamentgpt_agent.prompts import SYSTEM_PROMPT
from fake_model import FakeBedrockModel
from mcp_gateway_fixture import InProcessGateway


@pytest.fixture()
def gateway():
    gw = InProcessGateway()
    gw.start()
    yield gw
    gw.stop()


@pytest.fixture()
def gateway_client(gateway):
    # A real MCPClient connected to the in-process gateway over streamable HTTP (no auth).
    from mcp.client.streamable_http import streamablehttp_client

    client = gateway.make_client(lambda: streamablehttp_client(gateway.mcp_url))
    client.start()
    yield client
    try:
        client.stop(None, None, None)
    except Exception:  # nosec B110 - teardown best-effort; the fixture must not mask test results
        pass


def _agent_with_fake_model(client) -> tuple[Agent, FakeBedrockModel]:
    model = FakeBedrockModel()
    tools = list_all_tools(client)
    agent = Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=tools)
    return agent, model


def test_gateway_exposes_germany_tools(gateway_client):
    names = {t.tool_name for t in list_all_tools(gateway_client)}
    assert "germany___search_debates" in names
    assert "germany___get_debate_text" in names


def test_e2e_on_topic_calls_gateway_and_grounds_answer(gateway, gateway_client):
    agent, model = _agent_with_fake_model(gateway_client)
    result = agent("Was wurde im Bundestag zum Klimaschutz in der 21. Wahlperiode gesagt?")
    text = str(result)

    # The agent actually invoked the Germany search tool over MCP.
    assert any(c["tool"] == "germany___search_debates" for c in gateway.calls)
    # The answer is grounded in / cites the normalized result.
    assert "Hubertus Heil" in text
    assert "SPD" in text
    assert "2026-06-11" in text
    assert "dserver.bundestag.de" in text
    assert len(model.invocations) == 2   # tool call, then grounded answer


def test_e2e_off_topic_refuses_without_calling_gateway(gateway, gateway_client):
    agent, model = _agent_with_fake_model(gateway_client)
    result = agent("Wie programmiere ich einen Webserver in Python?")
    text = str(result).strip()

    assert REFUSAL_MESSAGE in text
    assert gateway.calls == []           # no tool was ever called
    assert len(model.invocations) == 1


def test_e2e_agentcore_entrypoint_returns_sources(gateway, gateway_client, monkeypatch):
    import parlamentgpt_agent.main as main_mod

    agent, _ = _agent_with_fake_model(gateway_client)
    monkeypatch.setattr(main_mod, "_agent", agent)
    monkeypatch.setattr(main_mod, "_gateway_client", gateway_client)

    out = main_mod.invoke({"prompt": "Was sagte der Bundestag zum Klimaschutz?"})
    assert "answer" in out and "sources" in out
    assert isinstance(out["sources"], list) and out["sources"]
    src = out["sources"][0]
    assert src["jurisdiction"] == "de"
    assert src["group"] == "SPD"
    assert src["doc_id"] == "aktivitaet:1784775@protokoll:5798"


def test_entrypoint_executes_text_tool_call(gateway, gateway_client, monkeypatch):
    """A model that writes the tool call as text should still hit the Gateway (prose-tool-call path)."""
    import parlamentgpt_agent.main as main_mod

    class _Result:
        message = {
            "role": "assistant",
            "content": [
                {"text": 'I will call germany___search_debates(speaker="Hubertus Heil", term="21")'}
            ],
        }

    class _TextToolAgent:
        def __init__(self):
            self.messages = []

        def __call__(self, prompt):
            return _Result()

    monkeypatch.setattr(main_mod, "_agent", _TextToolAgent())
    monkeypatch.setattr(main_mod, "_gateway_client", gateway_client)

    out = main_mod.invoke({"prompt": "reden von hubertus heil"})
    assert "Hubertus Heil" in out["answer"]
    assert out["sources"][0]["doc_id"] == "aktivitaet:1784775@protokoll:5798"
    assert out["steps"][0]["type"] == "tool_call"
    assert out["steps"][0]["jurisdiction"] == "germany"
    # And it really reached the in-process gateway.
    assert any(c["tool"] == "germany___search_debates" for c in gateway.calls)


def test_e2e_entrypoint_rejects_empty_prompt():
    import parlamentgpt_agent.main as main_mod
    out = main_mod.invoke({"prompt": "   "})
    assert out["answer"] == REFUSAL_MESSAGE
    assert out["sources"] == []
