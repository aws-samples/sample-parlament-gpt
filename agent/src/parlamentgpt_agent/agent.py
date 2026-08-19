"""Agent assembly: Strands Agent + Bedrock model + Guardrail + AgentCore Gateway tools.

The debate/speech tools are no longer local Python functions calling parliament APIs; they are
served by an AgentCore Gateway (one Lambda per government) and consumed over MCP. This module
builds the model, starts the shared Gateway MCP client, and registers its tools on the agent.

The client is started here (at cold start, from ``main.py`` module scope) and the tools are
registered with the explicit-list form so a transient sub-Agent's GC can never tear down the
shared session (see gateway.py / ADR §0).

For local development / tests without a live Gateway, pass an ``MCPClient`` built over an
in-process transport via ``mcp_client=``; ``build_agent`` will not construct the production
SigV4 transport in that case.
"""
from __future__ import annotations

import os
from typing import Optional

from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from .config import Settings, load_settings
from .gateway import build_client, ensure_session, list_all_tools
from .prompts import SYSTEM_PROMPT


def build_model(settings: Settings) -> BedrockModel:
    model_kwargs: dict = {
        "model_id": settings.model_id,
        "region_name": settings.region,
        "streaming": True,
    }
    # Some Bedrock-hosted models reject the `temperature` parameter. It is not set by
    # default; set BEDROCK_TEMPERATURE only for models that accept it.
    temp = os.getenv("BEDROCK_TEMPERATURE")
    if temp:
        model_kwargs["temperature"] = float(temp)
    # Attach the Bedrock Guardrail to EVERY model invocation when configured.
    if settings.guardrail_id and settings.guardrail_version:
        model_kwargs.update(
            guardrail_id=settings.guardrail_id,
            guardrail_version=settings.guardrail_version,
            guardrail_redact_input=True,
            guardrail_redact_output=True,
        )
    return BedrockModel(**model_kwargs)


def build_agent(
    settings: Settings | None = None,
    *,
    mcp_client: Optional[MCPClient] = None,
) -> tuple[Agent, MCPClient]:
    """Build the agent and return it together with the live Gateway MCP client.

    The caller (main.py) keeps the client to run ``ensure_session`` before each request. The
    client is started here; tools are listed once and registered explicitly.
    """
    settings = settings or load_settings()
    client = mcp_client or build_client()

    # Start (idempotently) and register the Gateway tools by value, not via the ToolProvider
    # path, to avoid the consumer/GC teardown footgun.
    ensure_session(client)
    tools = list_all_tools(client)

    agent = Agent(
        model=build_model(settings),
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
    )
    return agent, client
