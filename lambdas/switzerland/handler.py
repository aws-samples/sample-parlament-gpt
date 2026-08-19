"""AWS Lambda entrypoint for the Switzerland (ws.parlament.ch) debate fetcher.

Exposed through the AgentCore Gateway as switzerland___search_debates /
switzerland___get_debate_text. No credential needed.

Timeout note: this service is SLOW. A bounded Text search still runs for many seconds and an
unbounded one measured 43-85 s cold, so the socket timeout here is deliberately generous and the
Lambda timeout (set in CDK) must exceed it. The adapter always bounds the date range.

Terms of use are binding: cite "Parlamentsdienste der Bundesversammlung, Bern", do not alter
content, and do not present the data as an official publication. The attribution is rendered in
the frontend.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient

from parlament_ch import SwissAdapter

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "ws.parlament.ch,www.parlament.ch")
# Generous by necessity: see the latency note above.
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "120"))


def _adapter() -> SwissAdapter:
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={"Accept": "application/json"},
    )
    return SwissAdapter(client)


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
