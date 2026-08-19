"""AWS Lambda entrypoint for the Austria (parlament.gv.at) debate fetcher.

Exposed through the AgentCore Gateway as austria___search_debates / austria___get_debate_text.
No credential needed. Data is CC BY 4.0 with attribution to the Parlamentsdirektion (rendered in
the frontend).

Note the search endpoint is POST-only and undocumented; see parlament_at.py for the traps.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient

from parlament_at import AustriaAdapter

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "www.parlament.gv.at")
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "30"))


def _adapter() -> AustriaAdapter:
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        # The search endpoint requires a JSON content type; text fetches return HTML.
        default_headers={"Accept": "application/json, text/html", "Content-Type": "application/json"},
    )
    return AustriaAdapter(client)


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
