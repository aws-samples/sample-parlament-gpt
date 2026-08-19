"""AWS Lambda entrypoint for the Germany (Bundestag / DIP) debate fetcher.

Exposed through the AgentCore Gateway as two tools:
  * germany___search_debates
  * germany___get_debate_text

The Gateway passes tool arguments as the flat ``event`` and the tool name in
``context.client_context.custom`` (see gov_debates.gateway.handler). We build a host-pinned
client from the ``ALLOWED_HOSTS`` env var (set by CDK — never from a caller) and read the DIP
API key from this function's own secret.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.secrets import get_api_key

from dip import DipAdapter

# Read once per container (cold start). ALLOWED_HOSTS is injected by CDK from the jurisdictions
# table; DIP needs both the API host and the document host (dserver) for citation links.
_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "search.dip.bundestag.de,dserver.bundestag.de")
_TIMEOUT_S = float(os.getenv("DIP_TIMEOUT_S", "30"))   # /plenarprotokoll-text is multi-MB


def _adapter() -> DipAdapter:
    # SECRET_ARN is set by CDK; DIP_API_KEY is a local-dev/test fallback only.
    api_key = get_api_key(env_var="DIP_API_KEY")
    client = PinnedHttpClient(_ALLOWED_HOSTS, timeout_s=_TIMEOUT_S)
    return DipAdapter(client, api_key)


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
