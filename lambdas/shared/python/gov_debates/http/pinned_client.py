"""Host-pinned HTTP client — the per-Lambda egress control.

This is a direct descendant of the repo's original ``agent/src/parlamentgpt_agent/dip_client.py``
and it inherits that file's security role: it is, at any layer, the ONLY thing restricting
where a fetcher Lambda can send traffic. The workload has no network-layer egress backstop
(the Network Firewall and NAT gateways were removed for cost — see docs/architecture.md and
docs/threat-model.md M2), and these Lambdas run outside the VPC, so this application-layer
pin IS the egress control.

Compared to the original single-host pin, the guarantee is *strictly better in blast radius*:
each Lambda is constructed with its OWN allowlist, so a compromise of one fetcher can reach
only that jurisdiction's hosts, and the agent process itself now holds zero parliament
credentials and zero parliament egress.

Security properties (all covered by tests, ported from the original test_egress.py):
  * Refuses any host not in the allowlist — exact hostname match, so
    ``search.dip.bundestag.de.evil.com`` is rejected (suffix-confusion) and
    ``169.254.169.254`` (SSRF to instance metadata) is rejected.
  * Fails CLOSED: constructing a client with an empty allowlist raises immediately. An
    empty allowlist must never silently mean "allow everything".
  * The allowlist is injected at construction from configuration (in production, from the
    Lambda's ``ALLOWED_HOSTS`` env var set by CDK), NEVER from a caller/tool argument.
  * Follows NO redirects at all (follow_redirects=False; any redirect raises EgressViolation).
    Deliberately stricter than refusing only cross-host redirects — do not loosen this to
    same-host following without treating it as an egress-control change.
  * Timeouts are always set.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urljoin, urlparse

import httpx


class EgressViolation(RuntimeError):
    """Raised when something attempts to reach a host outside the allowlist."""


def normalize_host(host: str | None) -> str:
    """Lowercase and strip a trailing dot from a hostname for exact comparison."""
    if not host:
        return ""
    return host.strip().rstrip(".").lower()


def parse_allowlist(raw: str | Iterable[str]) -> frozenset[str]:
    """Parse a host allowlist from a comma-separated string or an iterable of hosts.

    In production the allowlist arrives as the ``ALLOWED_HOSTS`` env var (comma-joined by
    CDK from the jurisdictions table). Blank entries are dropped; hosts are normalized.
    """
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    return frozenset(h for h in (normalize_host(x) for x in items) if h)


class PinnedHttpClient:
    """An httpx-backed client that refuses to talk to any host outside ``allowed_hosts``.

    Args:
        allowed_hosts: comma-separated string or iterable of exact hostnames. MUST be
            non-empty — an empty allowlist raises :class:`EgressViolation` (fail closed).
        timeout_s: request timeout in seconds (always applied).
        default_headers: headers sent on every request (e.g. ``Accept``). Auth headers
            should be supplied per-request via ``headers=`` so keys are not retained here.
        client: optional pre-built ``httpx.Client`` (tests inject a MockTransport).
    """

    def __init__(
        self,
        allowed_hosts: str | Iterable[str],
        *,
        timeout_s: float = 20.0,
        default_headers: Optional[Mapping[str, str]] = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._allowed = parse_allowlist(allowed_hosts)
        if not self._allowed:
            # Fail closed: never let an empty allowlist mean "allow everything".
            raise EgressViolation(
                "PinnedHttpClient constructed with an empty host allowlist; refusing to "
                "allow unrestricted egress."
            )
        self._timeout_s = timeout_s
        # Retained and merged into EVERY request rather than only baked into a client we build.
        # An injected client (tests, or a caller supplying its own transport) would otherwise
        # silently drop these — and for some sources that is a correctness bug, not cosmetic:
        # the EP API returns RDF/XML instead of JSON when `Accept` is missing.
        self._default_headers: dict[str, str] = dict(
            default_headers or {"Accept": "application/json"}
        )
        self._client = client or httpx.Client(
            timeout=timeout_s,
            follow_redirects=False,
            headers=dict(self._default_headers),
        )

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return self._allowed

    def assert_allowed(self, url: str) -> None:
        """Raise :class:`EgressViolation` unless ``url``'s host is in the allowlist."""
        host = normalize_host(urlparse(url).hostname)
        if host not in self._allowed:
            raise EgressViolation(
                f"Blocked egress to {host!r}. Allowed hosts: {sorted(self._allowed)}."
            )

    def get(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        """GET an absolute URL after asserting its host is allowed.

        ``params`` values that are ``None`` are dropped (a convenience the DIP API and
        others need, since a bare ``?cursor=`` differs from an absent cursor).
        """
        self.assert_allowed(url)
        # Per-request headers win over the defaults, but the defaults are always present.
        merged = {**self._default_headers, **dict(headers or {})}
        cleaned = _clean_params(params)
        # httpx REPLACES a URL's existing query string when `params` is passed — even an empty
        # dict. Server-driven pagination hands us absolute next-page URLs whose query carries the
        # continuation token (OData `$skiptoken`), so passing params={} would silently drop it and
        # re-request page 1 forever. Only pass params when we actually have some.
        if cleaned:
            resp = self._client.get(url, params=cleaned, headers=merged)
        else:
            resp = self._client.get(url, headers=merged)
        # httpx Response.is_redirect is a property, not a method (scanner false positive).
        if resp.is_redirect:  # nosemgrep: is-function-without-parentheses
            location = resp.headers.get("location", "")
            # A redirect to another host is an egress violation, not a silent follow.
            self.assert_allowed(urljoin(url, location))
            raise EgressViolation(f"Unexpected redirect to {location!r} blocked.")
        resp.raise_for_status()
        return resp

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        return self.get(url, params=params, headers=headers).json()

    def get_text(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        return self.get(url, params=params, headers=headers).text

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        """POST a JSON body after asserting the host is allowed.

        Some sources expose their debate search only over POST (Austria's filter API returns
        HTTP 405 for GET), so the pin must cover POST too — a GET-only allowlist would be a hole.
        """
        self.assert_allowed(url)
        merged = {**self._default_headers, **dict(headers or {})}
        cleaned = _clean_params(params)
        kwargs: dict[str, Any] = {"headers": merged}
        if cleaned:
            kwargs["params"] = cleaned
        if json is not None:
            kwargs["json"] = json
        resp = self._client.post(url, **kwargs)
        # httpx Response.is_redirect is a property, not a method (scanner false positive).
        if resp.is_redirect:  # nosemgrep: is-function-without-parentheses
            location = resp.headers.get("location", "")
            self.assert_allowed(urljoin(url, location))
            raise EgressViolation(f"Unexpected redirect to {location!r} blocked.")
        resp.raise_for_status()
        return resp

    def post_json(
        self,
        url: str,
        *,
        json: Any = None,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> Any:
        return self.post(url, json=json, params=params, headers=headers).json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PinnedHttpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _clean_params(params: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not params:
        return {}
    return {k: v for k, v in params.items() if v is not None}
