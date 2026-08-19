"""AWS Lambda entrypoint for the US Congress (GovInfo) debate fetcher.

Exposed through the AgentCore Gateway as uscongress___search_debates /
uscongress___get_debate_text.

Requires a free GovInfo API key (register at https://www.govinfo.gov/api-signup — email only,
issued instantly). CDK creates the secret empty; the operator fills it. The shared DEMO_KEY is
throttled to ~10 requests/hour and is unusable here.

Two hosts are required, not one: api.govinfo.gov (search + metadata, keyed) and www.govinfo.gov
(keyless verbatim text). Citation links deliberately use the keyless host so the API key can
never leak into a URL shown to a user.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.secrets import get_api_key

from govinfo import GovInfoAdapter

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "api.govinfo.gov,www.govinfo.gov")
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "45"))


def _adapter() -> GovInfoAdapter:
    api_key = get_api_key(env_var="GOVINFO_API_KEY")
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    return GovInfoAdapter(client, api_key)


def search_debates(args: Mapping[str, Any]) -> Any:
    adapter = _adapter()
    try:
        return adapter.search(
            query=args.get("query"),
            speaker=args.get("speaker"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            term=args.get("term"),
            chamber=args.get("chamber"),
            cursor=args.get("cursor"),
            max_results=args.get("max_results", 5),
        )
    finally:
        adapter._client.close()


def get_debate_text(args: Mapping[str, Any]) -> Any:
    doc_id = args.get("doc_id")
    if not doc_id:
        return bad_argument("doc_id is required (use a value returned by search_debates)")
    adapter = _adapter()
    try:
        return adapter.get_text(
            doc_id=doc_id,
            query=args.get("query"),
            max_chars=args.get("max_chars", 6000),
        )
    finally:
        adapter._client.close()


TOOLS = {"search_debates": search_debates, "get_debate_text": get_debate_text}


def lambda_handler(event: Any, context: Any) -> Any:
    return dispatch(event, context, TOOLS)
