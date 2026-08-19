"""AWS Lambda entrypoints for the Netherlands (Tweede Kamer).

DISABLED BY DEFAULT — `enabled: false` in the CDK jurisdictions table. The Dutch OData portal
cannot search debates ($search is silently ignored and no field holds speech text), so this
jurisdiction needs an ingest job plus an index-backed query path.

  * ``lambda_handler``        — Gateway query path; answers from the index.
  * ``ingest_lambda_handler`` — scheduled job: enumerate reports, dedup ~10-per-sitting, download
    each whole-meeting XML (0.9-3 MB), parse, write month shards.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.ingest.query_adapter import IndexBackedAdapter
from gov_debates.ingest.store import S3SpeechStore, store_from_env

from vlos import (
    JURISDICTION,
    JURISDICTION_LABEL,
    ODATA_BASE,
    parse_transcript,
    pick_report_per_meeting,
)

_ALLOWED_HOSTS = os.getenv(
    "ALLOWED_HOSTS", "gegevensmagazijn.tweedekamer.nl,opendata.tweedekamer.nl"
)
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "120"))
# Reports to pull per ingest run (each is a multi-MB download, so keep runs bounded).
_MAX_REPORTS = int(os.getenv("NL_MAX_REPORTS", "50"))

COVERAGE_NOTE = (
    "Dutch debates are served from a periodic import of Tweede Kamer meeting transcripts, because "
    "the source has no full-text search. Recent reports are often uncorrected — check text_status. "
    "Party affiliation is not present in the transcripts and is omitted rather than guessed."
)
INGEST_HINT = "Run the Netherlands ingest job to populate it."


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
    """Enumerate reports, dedup per sitting, download + parse transcripts, write shards."""
    bucket = os.environ["INDEX_BUCKET"]
    store = S3SpeechStore(bucket, os.getenv("INDEX_PREFIX", "speeches"))
    client = PinnedHttpClient(_ALLOWED_HOSTS, timeout_s=_TIMEOUT_S)

    limit = int((event or {}).get("max_reports") or _MAX_REPORTS)
    by_month: dict[str, list] = defaultdict(list)
    parsed = 0
    try:
        # Step 1: enumerate reports (metadata only — these rows carry no speech text).
        listing = client.get_json(
            f"{ODATA_BASE}/Verslag",
            params={
                "$filter": "Verwijderd eq false",
                "$orderby": "GewijzigdOp desc",
                "$top": min(limit * 10, 250),   # over-fetch: ~10 reports per sitting get deduped
                "$count": "true",
            },
        )
        reports = pick_report_per_meeting(listing.get("value") or [])[:limit]

        # Step 2: one multi-MB transcript download per surviving report.
        for report in reports:
            report_id = report.get("Id")
            if not report_id:
                continue
            xml_bytes = client.get(f"{ODATA_BASE}/Verslag/{report_id}/resource").content
            for speech in parse_transcript(xml_bytes, report=report):
                by_month[speech.date[:7]].append(speech)
            parsed += 1
    finally:
        client.close()

    written = 0
    for month, speeches in sorted(by_month.items()):
        # Merge with what is already stored for that month so incremental runs do not truncate.
        existing, _ = store.query(
            jurisdiction=JURISDICTION, terms=[],
            date_start=f"{month}-01", date_end=f"{month}-31", offset=0, limit=100000,
        )
        merged = {s.doc_id: s for s in existing}
        merged.update({s.doc_id: s for s in speeches})
        written += store.put_shard(JURISDICTION, month, list(merged.values()))

    return {
        "jurisdiction": JURISDICTION,
        "reports_parsed": parsed,
        "months_written": len(by_month),
        "speeches_written": written,
    }
