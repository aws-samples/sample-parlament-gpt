"""Egress allowlist tests — the per-Lambda egress control (ported from the original DIP tests).

This is the security-critical suite (ADR risk R2). It must retain the two pin-bypass cases
from the original ``agent/tests/test_egress.py`` verbatim in spirit — the suffix-confusion host
and the instance-metadata IP — and now additionally proves the fail-closed empty-allowlist
behaviour and per-jurisdiction pinning.
"""
import httpx
import pytest

from gov_debates.http.pinned_client import (
    EgressViolation,
    PinnedHttpClient,
    normalize_host,
    parse_allowlist,
)

# Real per-jurisdiction hosts (from docs/multi-gov/source-profiles). Each Lambda is pinned to
# only its own row.
JURISDICTION_HOSTS = {
    "de": ["search.dip.bundestag.de", "dserver.bundestag.de"],
    "uk": ["hansard-api.parliament.uk", "members-api.parliament.uk"],
    "eu": ["data.europarl.europa.eu"],
    "ch": ["ws.parlament.ch"],
    "at": ["www.parlament.gv.at"],
    "us": ["api.govinfo.gov", "www.govinfo.gov"],
    "ca": ["www.ourcommons.ca"],
}

# Hosts that must NEVER be reachable, regardless of jurisdiction.
FORBIDDEN_URLS = [
    "https://example.com",
    "http://localhost:8080/admin",
    "https://search.dip.bundestag.de.evil.com/api",   # suffix-confusion
    "https://attacker.example.com/api/v1",
    "https://169.254.169.254/latest/meta-data/",       # SSRF to instance metadata
]


def _client(hosts):
    # A MockTransport that would 200 anything — so a passing "blocked" assertion proves the
    # pin refused BEFORE any network call, not that the network happened to fail.
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    return PinnedHttpClient(hosts, client=httpx.Client(transport=transport))


def test_empty_allowlist_fails_closed():
    with pytest.raises(EgressViolation):
        PinnedHttpClient("")
    with pytest.raises(EgressViolation):
        PinnedHttpClient([])
    with pytest.raises(EgressViolation):
        PinnedHttpClient("   ,  ,")  # only blanks


@pytest.mark.parametrize("jur,hosts", JURISDICTION_HOSTS.items())
def test_allowed_hosts_pass(jur, hosts):
    client = _client(hosts)
    for h in hosts:
        client.assert_allowed(f"https://{h}/some/path")


@pytest.mark.parametrize("jur,hosts", JURISDICTION_HOSTS.items())
@pytest.mark.parametrize("bad", FORBIDDEN_URLS)
def test_forbidden_hosts_rejected_for_every_jurisdiction(jur, hosts, bad):
    client = _client(hosts)
    with pytest.raises(EgressViolation):
        client.assert_allowed(bad)


@pytest.mark.parametrize("jur,hosts", JURISDICTION_HOSTS.items())
@pytest.mark.parametrize("bad", FORBIDDEN_URLS)
def test_get_to_forbidden_host_raises_before_network(jur, hosts, bad):
    client = _client(hosts)
    with pytest.raises(EgressViolation):
        client.get(bad)


def test_one_jurisdiction_cannot_reach_anothers_host():
    # A DE-pinned client must not reach a UK host, and vice versa — blast-radius isolation.
    de = _client(JURISDICTION_HOSTS["de"])
    with pytest.raises(EgressViolation):
        de.assert_allowed("https://hansard-api.parliament.uk/search")
    uk = _client(JURISDICTION_HOSTS["uk"])
    with pytest.raises(EgressViolation):
        uk.assert_allowed("https://search.dip.bundestag.de/api/v1/aktivitaet")


def test_cross_host_redirect_is_blocked():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/"})

    client = PinnedHttpClient(
        JURISDICTION_HOSTS["de"], client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(EgressViolation):
        client.get("https://search.dip.bundestag.de/api/v1/aktivitaet")


def test_default_headers_reach_the_wire_even_with_an_injected_client():
    """Regression: default headers must merge into every request.

    Previously they were only baked into a client the class built itself, so an injected client
    silently dropped them. That is a correctness bug for some sources, not cosmetic — the EP API
    returns RDF/XML instead of JSON when `Accept` is missing.
    """
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    client = PinnedHttpClient(
        "example.org",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        default_headers={"Accept": "application/ld+json", "User-Agent": "test-agent/1.0"},
    )
    client.get("https://example.org/x")
    assert seen.get("accept") == "application/ld+json"
    assert seen.get("user-agent") == "test-agent/1.0"


def test_absolute_url_query_string_is_preserved():
    """Regression: an embedded query string must survive.

    httpx REPLACES a URL's query when `params` is passed — even an empty dict. Server-driven
    pagination (OData `__next` / `$skiptoken`) hands back absolute next-page URLs whose query IS
    the continuation token, so dropping it silently re-requests page 1 forever — an infinite loop
    that looks like "the source only has one page".
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = PinnedHttpClient(
        "example.org", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.get("https://example.org/Transcript?$skiptoken=1005L,'DE'&$top=200")
    assert "$skiptoken=1005L" in seen[0]
    assert "$top=200" in seen[0]


def test_explicit_params_still_apply_to_a_bare_url():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = PinnedHttpClient(
        "example.org", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    client.get("https://example.org/search", params={"q": "climate", "empty": None})
    assert "q=climate" in seen[0]
    assert "empty" not in seen[0]   # None-valued params are dropped


def test_per_request_headers_override_defaults():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json={})

    client = PinnedHttpClient(
        "example.org",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        default_headers={"Accept": "application/json"},
    )
    client.get("https://example.org/x", headers={"Authorization": "ApiKey k", "Accept": "text/xml"})
    assert seen.get("accept") == "text/xml"          # per-request wins
    assert seen.get("authorization") == "ApiKey k"   # and is still applied


def test_normalize_host_handles_trailing_dot_and_case():
    assert normalize_host("Search.DIP.Bundestag.DE.") == "search.dip.bundestag.de"
    assert normalize_host(None) == ""


def test_parse_allowlist_drops_blanks_and_normalizes():
    assert parse_allowlist("A.com, , b.com ,") == frozenset({"a.com", "b.com"})
    assert parse_allowlist(["X.io", "x.io"]) == frozenset({"x.io"})
