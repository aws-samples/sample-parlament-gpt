"""AWS Lambda entrypoint for the European Parliament debate fetcher.

Exposed through the AgentCore Gateway as europarl___search_debates / europarl___get_debate_text.
No credential needed (CC BY 4.0; attribution rendered in the frontend).

Two headers matter here:
  * `Accept: application/ld+json` is REQUIRED — without it the API silently returns RDF/XML.
  * A descriptive `User-Agent` is "strongly recommended" by the EP spec for platform monitoring.
    Per Regulation (EU) 2018/1725 it must contain NO personal data.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient

from europarl import EuroparlAdapter

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "data.europarl.europa.eu,www.europarl.europa.eu")
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "30"))
# Format {user-id}-{environment}-{version}; no personal data.
_USER_AGENT = os.getenv("HTTP_USER_AGENT", "parlamentgpt-prd-1.0.0")


def _adapter() -> EuroparlAdapter:
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={
            # Mandatory: omitting this yields RDF/XML instead of JSON.
            "Accept": "application/ld+json",
            "User-Agent": _USER_AGENT,
        },
    )
    return EuroparlAdapter(client)


def search_debates(args: Mapping[str, Any]) -> Any:
    adapter = _adapter()
    try:
        return adapter.search(
            query=args.get("query"),
            speaker=args.get("speaker"),
            date_start=args.get("date_start"),
            date_end=args.get("date_end"),
            term=args.get("term"),
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
