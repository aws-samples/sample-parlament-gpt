"""AWS Lambda entrypoint for the UK (Hansard) debate fetcher.

Exposed through the AgentCore Gateway as uk___search_debates / uk___get_debate_text.
No credential needed — the Hansard API is open (Open Parliament Licence; attribution required
and rendered in the frontend).
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient

from hansard import HansardAdapter

_ALLOWED_HOSTS = os.getenv(
    "ALLOWED_HOSTS", "hansard-api.parliament.uk,members-api.parliament.uk"
)
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "30"))
# The estate sits behind Cloudflare; a descriptive UA reduces bot-mitigation risk.
_USER_AGENT = os.getenv("HTTP_USER_AGENT", "parlamentgpt/1.0 (parliamentary research)")


def _adapter() -> HansardAdapter:
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    return HansardAdapter(client)


def search_debates(args: Mapping[str, Any]) -> Any:
    adapter = _adapter()
    try:
        return adapter.search(
            query=args.get("query"),
            speaker=args.get("speaker"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
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
