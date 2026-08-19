"""Guardrail / refusal tests.

We cannot invoke Bedrock live in CI, so we assert:
  1. The fixed German refusal message is consistently defined.
  2. The system prompt reinforces the topic lock and instructs the exact refusal.
  3. build_agent() wires the Bedrock Guardrail into the model when configured.
  4. The off-topic refusal contract: an agent backed by a stub model that honours the
     system prompt returns exactly the refusal for off-topic input.
"""
import parlamentgpt_agent.agent as agent_mod
from parlamentgpt_agent.config import REFUSAL_MESSAGE, Settings
from parlamentgpt_agent.prompts import SYSTEM_PROMPT


def test_refusal_message_is_fixed():
    assert REFUSAL_MESSAGE == "I only answer questions about parliamentary debates and speeches."


def test_refusal_message_matches_the_cdk_guardrail():
    """The refusal string is hand-duplicated in TypeScript with no build-time link.

    A drift here means the Guardrail blocks with different wording than the agent refuses with,
    which is exactly the kind of mismatch nobody notices until a user sees two different
    messages for the same rejection.
    """
    import pathlib
    import re

    stack = pathlib.Path(__file__).resolve().parents[2] / "infra" / "lib" / "security-stack.ts"
    source = stack.read_text(encoding="utf-8")
    match = re.search(r'const REFUSAL\s*=\s*"([^"]+)"', source)
    assert match, "could not find `const REFUSAL = \"...\"` in security-stack.ts"
    assert match.group(1) == REFUSAL_MESSAGE


def test_system_prompt_reinforces_guardrail():
    # Collapse wrapping so assertions are not sensitive to where lines break.
    flat = " ".join(SYSTEM_PROMPT.split()).lower()
    assert REFUSAL_MESSAGE in SYSTEM_PROMPT
    assert "prompt injection" in flat
    assert "speeches by hubertus heil 2026" in flat
    assert "you must not refuse such requests" in flat
    # No other sources / tools allowed.
    assert "no other knowledge" in flat
    assert "may not use any other sources" in flat


def test_system_prompt_is_multi_jurisdiction():
    """The prompt must not re-acquire the German-only assumptions it was rewritten to drop."""
    lowered = SYSTEM_PROMPT.lower()
    # The harmful translate-to-German rule must be gone.
    assert "into german" not in lowered
    # The hardcoded DIP tool/resource names must be gone (tools come from tools/list).
    for stale in ("search_bundestag_speeches", "get_bundestag_document_text", "aktivitaet",
                  "plenarprotokoll", "drucksache", "vorgangsposition"):
        assert stale not in lowered, f"stale German-only token in system prompt: {stale}"
    # Jurisdiction-selection guidance must be present.
    assert "jurisdiction" in lowered
    assert "___search_debates" in SYSTEM_PROMPT
    # Honesty about translated / provisional text.
    assert "is_translation" in SYSTEM_PROMPT
    assert "text_status" in SYSTEM_PROMPT


class _FakeMCPClient:
    """Stand-in for the Gateway MCP client: no network, advertises no tools."""

    def _is_session_active(self):
        return True

    def start(self):
        return self

    def stop(self, *a):
        pass

    def list_tools_sync(self, pagination_token=None):
        from strands.types.collections import PaginatedList

        return PaginatedList([], token=None)


def test_build_agent_attaches_guardrail(monkeypatch):
    captured = {}

    class FakeModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeAgent:
        def __init__(self, model, system_prompt, tools):
            self.model = model
            self.system_prompt = system_prompt
            self.tools = tools

    monkeypatch.setattr(agent_mod, "BedrockModel", FakeModel)
    monkeypatch.setattr(agent_mod, "Agent", FakeAgent)

    settings = Settings(
        model_id="google.gemma-3-27b-it",
        region="eu-central-1",
        guardrail_id="gr-123",
        guardrail_version="1",
        gateway_mcp_url="https://gw.example/mcp",
    )
    a, client = agent_mod.build_agent(settings, mcp_client=_FakeMCPClient())
    assert captured["guardrail_id"] == "gr-123"
    assert captured["guardrail_version"] == "1"
    assert captured["model_id"] == "google.gemma-3-27b-it"
    assert a.system_prompt is SYSTEM_PROMPT


def test_offtopic_refusal_contract(monkeypatch):
    """A model honouring the system prompt must refuse off-topic input verbatim."""
    class StubModel:
        def __init__(self, **kwargs):
            pass

    class StubAgent:
        def __init__(self, model, system_prompt, tools):
            self.system_prompt = system_prompt

        def __call__(self, prompt: str):
            on_topic = any(w in prompt.lower() for w in ("bundestag", "rede", "plenar"))
            return REFUSAL_MESSAGE if not on_topic else "..."

    monkeypatch.setattr(agent_mod, "BedrockModel", StubModel)
    monkeypatch.setattr(agent_mod, "Agent", StubAgent)
    a, client = agent_mod.build_agent(
        Settings(model_id="m", region="eu-central-1", guardrail_id=None,
                 guardrail_version=None, gateway_mcp_url=None),
        mcp_client=_FakeMCPClient(),
    )
    assert a("Wie programmiere ich in Python?") == REFUSAL_MESSAGE
    assert a("Was wurde im Bundestag zur Rente gesagt?") != REFUSAL_MESSAGE
