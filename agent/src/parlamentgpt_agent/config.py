"""Configuration for the multi-government debate agent.

All values come from environment variables so they can be swapped without code changes. The
agent no longer talks to any parliament API directly — debate data comes from the AgentCore
Gateway (see ``gateway.py``), and per-source credentials live in each fetcher Lambda. So this
module only carries model/guardrail/gateway settings now; the DIP host pin and API key moved to
``lambdas/germany`` and ``lambdas/shared``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# The fixed refusal message used everywhere the agent must decline. Kept in one place so the
# guardrail config, system prompt, and code all agree. MUST stay byte-identical to
# infra/lib/security-stack.ts REFUSAL — the two are hand-duplicated across languages with no
# build-time link, so a test asserts they match.
REFUSAL_MESSAGE = "I only answer questions about parliamentary debates and speeches."


@dataclass(frozen=True)
class Settings:
    model_id: str
    region: str
    guardrail_id: str | None
    guardrail_version: str | None
    gateway_mcp_url: str | None


def load_settings() -> Settings:
    return Settings(
        # A global cross-region inference-profile ID (not an ARN): resolvable from any
        # account without knowing account numbers. Deployments override via BEDROCK_MODEL_ID.
        model_id=os.getenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"),
        region=os.getenv("AWS_REGION", "eu-central-1"),
        guardrail_id=os.getenv("GUARDRAIL_ID") or None,
        guardrail_version=os.getenv("GUARDRAIL_VERSION") or None,
        gateway_mcp_url=os.getenv("GATEWAY_MCP_URL") or None,
    )
