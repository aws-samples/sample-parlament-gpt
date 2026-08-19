"""AWS Lambda entrypoints for France (Assemblée nationale).

DISABLED BY DEFAULT — `enabled: false` in the CDK jurisdictions table, so no Gateway target exists
for it yet. France has no queryable debates API, so it needs two Lambdas rather than one:

  * ``lambda_handler``        — the Gateway query path; answers from the ingest index.
  * ``ingest_lambda_handler`` — the scheduled ingest job; downloads the bulk ZIP, parses each
    sitting, and writes month shards to S3.

Splitting them is forced by the source: the bulk ZIP is 55.7 MB (324 MB unpacked) and cannot be
fetched per document, so it can never be part of a request path.
"""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Mapping

from gov_debates.gateway.handler import bad_argument, dispatch
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.ingest.query_adapter import IndexBackedAdapter
from gov_debates.ingest.store import S3SpeechStore, store_from_env

from syseron import (
    BULK_URL,
    JURISDICTION,
    JURISDICTION_LABEL,
    iter_sitting_files,
    parse_sitting,
)

_ALLOWED_HOSTS = os.getenv(
    "ALLOWED_HOSTS", "data.assemblee-nationale.fr,www.assemblee-nationale.fr"
)
# The bulk download is large; the ingest job needs a long timeout and plenty of memory.
_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "300"))
# Legislatures to ingest, newest first (17 = current).
_LEGISLATURES = [l.strip() for l in os.getenv("FR_LEGISLATURES", "17").split(",") if l.strip()]

COVERAGE_NOTE = (
    "French debates are served from a nightly bulk import of the Assemblée nationale's open-data "
    "transcripts. Party affiliation is not included in that export and is therefore omitted rather "
    "than guessed."
)
INGEST_HINT = "Run the France ingest job to populate it."


# ---- query path (Gateway tools) --------------------------------------------------


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


# ---- ingest path (scheduled) -----------------------------------------------------


def ingest_lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    """Download the bulk ZIP(s), parse every sitting, and write month shards.

    Invoked on a schedule (EventBridge), never by the Gateway. Returns a summary so the run shows
    up meaningfully in logs and metrics.
    """
    bucket = os.environ["INDEX_BUCKET"]
    store = S3SpeechStore(bucket, os.getenv("INDEX_PREFIX", "speeches"))
    client = PinnedHttpClient(_ALLOWED_HOSTS, timeout_s=_TIMEOUT_S)

    legislatures = (event or {}).get("legislatures") or _LEGISLATURES
    by_month: dict[str, list] = defaultdict(list)
    sittings = 0
    try:
        for legislature in legislatures:
            url = BULK_URL.format(legislature=legislature)
            payload = client.get(url).content
            for _name, xml_bytes in iter_sitting_files(payload):
                sittings += 1
                for speech in parse_sitting(xml_bytes, legislature=legislature):
                    by_month[speech.date[:7]].append(speech)
    finally:
        client.close()

    written = 0
    for month, speeches in sorted(by_month.items()):
        written += store.put_shard(JURISDICTION, month, speeches)

    return {
        "jurisdiction": JURISDICTION,
        "legislatures": legislatures,
        "sittings_parsed": sittings,
        "months_written": len(by_month),
        "speeches_written": written,
    }
