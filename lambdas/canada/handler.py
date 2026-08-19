"""AWS Lambda entrypoint for the Canada (House of Commons Hansard) debate fetcher.

DISABLED BY DEFAULT — this jurisdiction is `enabled: false` in the CDK jurisdictions table, so no
Lambda or Gateway target is created for it yet. Two blockers must be cleared by a human first:

  1. Licensing: the Speaker's permission excludes reproduction "for commercial purpose of
     financial gain" and requires the material not be presented as official.
  2. robots.txt: ourcommons.ca disallows /PublicationSearch/ for all user-agents.

The adapter therefore refuses by default (`RESPECT_ROBOTS=true`). Setting RESPECT_ROBOTS=false is
an explicit, auditable decision — and even then, the compliant route for bulk use is the
whole-sitting XML under /Content/House/ (not disallowed), which would be an ingest pipeline
rather than a request-path Lambda.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient

from ourcommons import CanadaAdapter

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "www.ourcommons.ca")
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "60"))
# Defaults to refusing the disallowed search path; flipping this is a deliberate human decision.
_RESPECT_ROBOTS = os.getenv("RESPECT_ROBOTS", "true").strip().lower() != "false"


def _adapter() -> CanadaAdapter:
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={"Accept": "text/xml, application/xml"},
    )
    return CanadaAdapter(client, respect_robots=_RESPECT_ROBOTS)


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
