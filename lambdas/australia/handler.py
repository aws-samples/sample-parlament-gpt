"""AWS Lambda entrypoints for Australia (APH Hansard).

DISABLED BY DEFAULT — `enabled: false` in the CDK jurisdictions table. Two reasons, both needing a
human decision:

  1. LICENCE: CC BY-NC-ND 3.0 AU. The NonCommercial term conflicts with a monetised product, and
     NoDerivatives sits badly with snippeting and summarisation.
  2. TECHNICAL: search is HTML scraping of a WebForms page, full text comes from an undocumented
     endpoint, and that text is empty for every record before ~2011.

  * ``lambda_handler``        — Gateway query path; answers from the index.
  * ``ingest_lambda_handler`` — scheduled job: page the search HTML, then fetch each transcript
    (N+1 by necessity — search returns no text and no snippet).
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.ingest.query_adapter import IndexBackedAdapter
from gov_debates.ingest.store import S3SpeechStore, store_from_env

from aph import (
    BASE_URL,
    JURISDICTION,
    JURISDICTION_LABEL,
    MAX_PAGE_SIZE,
    SEARCH_PATH,
    TRANSCRIPT_PATH,
    extract_system_ids,
    last_page,
    parse_transcript,
    search_params,
)

_ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "www.aph.gov.au")
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "60"))
# Bound the N+1 fan-out per ingest run.
_MAX_ITEMS = int(os.getenv("AU_MAX_ITEMS", "200"))

COVERAGE_NOTE = (
    "Australian debates are served from a periodic import, because APH provides no debates API. "
    "The verbatim text is only available for sittings from about 2011 onward; earlier records are "
    "citable but their words are only in the official PDF. Party affiliation is not published in "
    "the transcript feed and is omitted rather than guessed."
)
INGEST_HINT = "Run the Australia ingest job to populate it."


# ---- query path -------------------------------------------------------------------


def _adapter() -> IndexBackedAdapter:
    return IndexBackedAdapter(
        store_from_env(),
        jurisdiction=JURISDICTION,
        jurisdiction_label=JURISDICTION_LABEL,
        coverage_note=COVERAGE_NOTE,
        ingest_hint=INGEST_HINT,
    )


def search_debates(args: Mapping[str, Any]) -> Any:
    return _adapter().search(
        query=args.get("query"),
        speaker=args.get("speaker"),
        date_start=args.get("date_start"),
        date_end=args.get("date_end"),
        chamber=args.get("chamber"),
        cursor=args.get("cursor"),
        max_results=args.get("max_results", 5),
    )


def get_debate_text(args: Mapping[str, Any]) -> Any:
    doc_id = args.get("doc_id")
    if not doc_id:
        return bad_argument("doc_id is required (use a value returned by search_debates)")
    return _adapter().get_text(
        doc_id=doc_id,
        query=args.get("query"),
        max_chars=args.get("max_chars", 6000),
    )


TOOLS = {"search_debates": search_debates, "get_debate_text": get_debate_text}


def lambda_handler(event: Any, context: Any) -> Any:
    return dispatch(event, context, TOOLS)


# ---- ingest path ------------------------------------------------------------------


def ingest_lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    """Scrape the search pages for a window, then fetch each transcript."""
    bucket = os.environ["INDEX_BUCKET"]
    store = S3SpeechStore(bucket, os.getenv("INDEX_PREFIX", "speeches"))
    client = PinnedHttpClient(
        _ALLOWED_HOSTS,
        timeout_s=_TIMEOUT_S,
        default_headers={
            "Accept": "text/html, application/json",
            # A descriptive UA with a contact is polite on a WAF-fronted estate.
            "User-Agent": os.getenv("HTTP_USER_AGENT", "parlamentgpt/1.0 (research)"),
        },
    )

    payload = event or {}
    query = payload.get("query") or ""
    date_start = payload.get("date_start")
    date_end = payload.get("date_end")
    chamber = payload.get("chamber")
    max_items = int(payload.get("max_items") or _MAX_ITEMS)

    by_month: dict[str, list] = defaultdict(list)
    fetched = 0
    pages_walked = 0
    try:
        first = client.get_text(
            f"{BASE_URL}{SEARCH_PATH}",
            params=search_params(
                query=query, date_start=date_start, date_end=date_end, chamber=chamber,
                page=1, page_size=MAX_PAGE_SIZE,
            ),
        )
        total_pages = last_page(first)
        page_html = first
        page = 1
        while page <= total_pages and fetched < max_items:
            pages_walked += 1
            for system_id in extract_system_ids(page_html):
                if fetched >= max_items:
                    break
                try:
                    doc = client.get_json(
                        f"{BASE_URL}{TRANSCRIPT_PATH}", params={"id": system_id}
                    )
                except Exception:  # nosec B112 - deliberate: a single bad item must not abort the batch run
                    continue
                speech = parse_transcript(doc, system_id)
                fetched += 1
                if speech is not None:
                    by_month[speech.date[:7]].append(speech)
            page += 1
            if page <= total_pages and fetched < max_items:
                page_html = client.get_text(
                    f"{BASE_URL}{SEARCH_PATH}",
                    params=search_params(
                        query=query, date_start=date_start, date_end=date_end, chamber=chamber,
                        page=page, page_size=MAX_PAGE_SIZE,
                    ),
                )
    finally:
        client.close()

    written = 0
    for month, speeches in sorted(by_month.items()):
        existing, _ = store.query(
            jurisdiction=JURISDICTION, terms=[],
            date_start=f"{month}-01", date_end=f"{month}-31", offset=0, limit=100000,
        )
        merged = {s.doc_id: s for s in existing}
        merged.update({s.doc_id: s for s in speeches})
        written += store.put_shard(JURISDICTION, month, list(merged.values()))

    return {
        "jurisdiction": JURISDICTION,
        "pages_walked": pages_walked,
        "items_fetched": fetched,
        "months_written": len(by_month),
        "speeches_written": written,
    }
