"""Tests for the Gateway auth seam (SigV4 IAM path and Cognito Bearer fallback)."""
import httpx
import pytest


@pytest.fixture()
def aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_sigv4_signs_a_well_formed_request(aws_creds):
    from parlamentgpt_agent.gateway import SigV4Auth

    auth = SigV4Auth("us-east-1")
    req = httpx.Request(
        "POST",
        "https://abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )
    signed = next(auth.auth_flow(req))
    assert signed.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "x-amz-date" in signed.headers
    assert signed.headers.get("x-amz-security-token") == "session-token"
    # The signed request must still carry the body unchanged.
    assert b"tools/list" in signed.content


def test_sigv4_raises_without_credentials(monkeypatch):
    from parlamentgpt_agent.gateway import SigV4Auth

    # Build the auth with creds present, then simulate the provider returning None.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    auth = SigV4Auth("us-east-1")
    monkeypatch.setattr(auth._session, "get_credentials", lambda: None)
    req = httpx.Request("POST", "https://x.example/mcp", content=b"{}")
    with pytest.raises(RuntimeError):
        next(auth.auth_flow(req))


def test_cognito_bearer_refreshes_and_injects(monkeypatch):
    from parlamentgpt_agent import gateway as gw

    calls = {"n": 0}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            calls["n"] += 1
            return {"access_token": f"tok-{calls['n']}", "token_type": "Bearer", "expires_in": 3600}

    monkeypatch.setattr(gw.httpx, "post", lambda *a, **k: FakeResp())
    provider = gw.CognitoTokenProvider("https://token", "cid", "secret", margin_s=120)
    auth = gw.BearerAuth(provider)

    req = httpx.Request("POST", "https://x.example/mcp", content=b"{}")
    signed = next(auth.auth_flow(req))
    assert signed.headers["authorization"] == "Bearer tok-1"
    # Cached: a second call within TTL does not re-mint.
    signed2 = next(auth.auth_flow(httpx.Request("POST", "https://x.example/mcp", content=b"{}")))
    assert signed2.headers["authorization"] == "Bearer tok-1"
    assert calls["n"] == 1


def test_build_auth_defaults_to_iam(monkeypatch, aws_creds):
    from parlamentgpt_agent import gateway as gw

    monkeypatch.setattr(gw, "GATEWAY_AUTH_MODE", "iam")
    assert isinstance(gw._build_auth(), gw.SigV4Auth)


def test_build_auth_cognito_mode(monkeypatch):
    from parlamentgpt_agent import gateway as gw

    monkeypatch.setattr(gw, "GATEWAY_AUTH_MODE", "cognito")
    monkeypatch.setenv("COGNITO_TOKEN_ENDPOINT", "https://token")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "cid")
    monkeypatch.setenv("COGNITO_CLIENT_SECRET", "secret")
    assert isinstance(gw._build_auth(), gw.BearerAuth)
