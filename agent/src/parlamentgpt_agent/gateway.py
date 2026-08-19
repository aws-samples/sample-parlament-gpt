"""AgentCore Gateway MCP client wiring for the agent.

The agent no longer holds local @tool functions that call parliament APIs directly. Instead
it consumes tools from an AgentCore Gateway (MCP over streamable HTTP), one Lambda target per
government. This module owns the long-lived MCP client and the request-auth seam.

Lifecycle (behaviour of strands-agents 1.52.0, pinned exactly in requirements.txt /
pyproject.toml — see docs/multi-gov/ADR-001 §0):
  * ``MCPClient`` started at module scope stays alive across many event loops and concurrent
    calls, so ``main.py`` keeps building the agent ONCE at cold start. The session lifetime is
    bound to the client's background thread, NOT to a ``with`` block.
  * Tools are registered with the explicit-list form ``tools=list(list_tools())`` (zero
    consumers) so a transient/GC'd sub-Agent can never tear down the shared session.
  * A 401 or network blip tears down the WHOLE session; ``ensure_session()`` reconnects. It is
    the first line of every request path.

Auth: two interchangeable paths behind the same ``httpx.Auth`` seam.
  * ``AWS_IAM`` (default): SigV4-sign each request for service ``bedrock-agentcore`` using the
    runtime's IAM role. No Cognito, no token endpoint, no secret. This is the primary design,
    validated against the live Gateway (the SigV4-signed MCP transport is not shown in any AWS
    sample — ADR risk R4, retired by that validation and by the e2e test).
  * ``COGNITO`` (fallback): OAuth2 client_credentials Bearer token with proactive refresh. Flip
    ``GATEWAY_AUTH_MODE=cognito`` and provide the Cognito env vars; no other code changes.

Never pass a static ``headers={"Authorization": ...}`` dict to the transport — httpx snapshots
it at connect time, so a token refreshed later never reaches the wire.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import httpx
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient

log = logging.getLogger(__name__)

GATEWAY_MCP_URL = os.getenv("GATEWAY_MCP_URL", "")
GATEWAY_AUTH_MODE = os.getenv("GATEWAY_AUTH_MODE", "iam").strip().lower()  # "iam" | "cognito"
_AWS_REGION = os.getenv("AWS_REGION", "eu-central-1")
_HTTP_TIMEOUT_S = float(os.getenv("GATEWAY_HTTP_TIMEOUT_S", "60"))
_SSE_READ_TIMEOUT_S = float(os.getenv("GATEWAY_SSE_READ_TIMEOUT_S", "300"))
# AgentCore signs requests for this service namespace.
_SIGV4_SERVICE = "bedrock-agentcore"


# ----------------------------------------------------------------- IAM (SigV4) auth
class SigV4Auth(httpx.Auth):
    """Sign each outgoing request with AWS SigV4 for the bedrock-agentcore service.

    Credentials come from the default provider chain (the runtime's IAM role in production).
    Resolved lazily and refreshed by botocore as needed. Runs on the MCPClient background
    thread, so it must be thread-safe — botocore credential resolution is.
    """

    def __init__(self, region: str, service: str = _SIGV4_SERVICE) -> None:
        import boto3
        from botocore.auth import SigV4Auth as _BotoSigV4
        from botocore.awsrequest import AWSRequest

        self._region = region
        self._service = service
        self._session = boto3.Session()
        self._signer_cls = _BotoSigV4
        self._awsrequest_cls = AWSRequest

    def auth_flow(self, request: httpx.Request):
        creds = self._session.get_credentials()
        if creds is None:
            raise RuntimeError("No AWS credentials available to sign the Gateway request.")
        frozen = creds.get_frozen_credentials()
        body = request.content or b""
        aws_req = self._awsrequest_cls(
            method=request.method,
            url=str(request.url),
            data=body,
            headers=dict(request.headers),
        )
        self._signer_cls(frozen, self._service, self._region).add_auth(aws_req)
        # Copy the SigV4 headers (Authorization, X-Amz-Date, X-Amz-Security-Token, ...) back.
        for key, value in aws_req.headers.items():
            request.headers[key] = value
        yield request


# ----------------------------------------------------------------- Cognito Bearer auth
class CognitoTokenProvider:
    """Thread-safe cached client_credentials token with proactive refresh (fallback path)."""

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        margin_s: float = 120.0,
    ) -> None:
        self._endpoint = token_endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._margin = margin_s
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at = 0.0

    def token(self) -> str:
        now = time.monotonic()
        if self._token and now < self._expires_at - self._margin:
            return self._token
        with self._lock:
            now = time.monotonic()
            if self._token and now < self._expires_at - self._margin:
                return self._token
            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                data["scope"] = self._scope
            resp = httpx.post(
                self._endpoint,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
            self._token = payload["access_token"]
            self._expires_at = now + float(payload.get("expires_in", 3600))
            return self._token


class BearerAuth(httpx.Auth):
    """Inject a FRESH bearer token on every request (never a static header dict)."""

    def __init__(self, provider: CognitoTokenProvider) -> None:
        self._provider = provider

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self._provider.token()}"
        yield request


def _build_auth() -> Optional[httpx.Auth]:
    if GATEWAY_AUTH_MODE == "cognito":
        provider = CognitoTokenProvider(
            token_endpoint=os.environ["COGNITO_TOKEN_ENDPOINT"],
            client_id=os.environ["COGNITO_CLIENT_ID"],
            client_secret=os.environ["COGNITO_CLIENT_SECRET"],
            scope=os.getenv("COGNITO_SCOPE"),
        )
        return BearerAuth(provider)
    # Default: IAM / SigV4.
    return SigV4Auth(_AWS_REGION)


def _make_transport():
    """Transport factory re-invoked by MCPClient on every start() (so auth is re-read)."""
    if not GATEWAY_MCP_URL:
        raise RuntimeError(
            "GATEWAY_MCP_URL is not set; the agent cannot reach the AgentCore Gateway. "
            "It is injected by CDK (gateway.attrGatewayUrl + '/mcp')."
        )
    return streamablehttp_client(
        GATEWAY_MCP_URL,
        auth=_build_auth(),
        timeout=_HTTP_TIMEOUT_S,
        sse_read_timeout=_SSE_READ_TIMEOUT_S,
    )


def build_client(transport_callable: Optional[Any] = None) -> MCPClient:
    """Construct the module-scope MCP client. Tests inject an in-process transport."""
    return MCPClient(transport_callable or _make_transport, startup_timeout=30)


def list_all_tools(client: MCPClient) -> list:
    """Drain every page of tools/list (AgentCore Gateway paginates it)."""
    tools: list = []
    token: Optional[str] = None
    while True:
        page = client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = getattr(page, "pagination_token", None)
        if token is None:
            return tools


def ensure_session(client: MCPClient) -> None:
    """Reconnect the client if the session died (401, network blip). Cheap when healthy.

    A dead session raises on start-if-running and re-raises the stored cause on stop(), so we
    guard on the (private) ``_is_session_active`` and swallow the stop() exception. That
    accessor is not public API, which is exactly why strands-agents is pinned to ==1.52.0 in
    requirements.txt; a rename in a future release would otherwise silently push every request
    down the teardown/rebuild path below. The warning log is the tripwire for that case.
    """
    try:
        active = client._is_session_active()
    except Exception as exc:
        log.warning("MCP session-state check failed (private accessor changed?): %s", exc)
        active = False
    if active:
        return
    try:
        client.stop(None, None, None)
    except Exception:  # nosec B110 - stop() of an already-dead session may fail; start() below is the recovery
        pass
    client.start()
