"""The query-path adapter for batch-ingested jurisdictions.

France, the Netherlands and Australia have no queryable debates API, so their Gateway tools answer
from our own index (see :mod:`gov_debates.ingest.store`) rather than calling upstream. This class
is that query path — identical for all three, so it lives here instead of being copied.

Two honesty properties matter and are enforced here:

  * When the index has no data for a jurisdiction yet, the tool says so EXPLICITLY rather than
    returning an empty result set that reads as "this parliament never discussed your topic". A
    silent empty answer would be indistinguishable from a real "no results", which is exactly the
    class of wrong answer this project keeps guarding against.
  * ``coverage_note`` is surfaced on every response, because all three sources have real coverage
    limits (Australia has no retrievable full text before ~2011; the Netherlands' recent reports
    are uncorrected; France depends on a nightly bulk refresh).
"""
from __future__ import annotations

from typing import Any, Optional

from gov_debates.contracts import to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.normalize import text as textnorm

from .documents import tokenize
from .store import SpeechStore


class IndexBackedAdapter:
    """Answers search/get_text for a jurisdiction from the ingest index."""

    def __init__(
        self,
        store: SpeechStore,
        *,
        jurisdiction: str,
        jurisdiction_label: str,
        coverage_note: str,
        ingest_hint: str,
    ) -> None:
        self._store = store
        self._jurisdiction = jurisdiction
        self._label = jurisdiction_label
        self._coverage_note = coverage_note
        self._ingest_hint = ingest_hint

    # ---- search -----------------------------------------------------------------

    def search(
        self,
        *,
        query: Optional[str] = None,
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        chamber: Optional[str] = None,
        max_results: int = 5,
        cursor: Optional[str] = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        limit = clamp_max_results(max_results)
        offset = _parse_offset(cursor)
        terms = tokenize(query)

        rows, total = self._store.query(
            jurisdiction=self._jurisdiction,
            terms=terms,
            speaker=speaker,
            date_start=date_start,
            date_end=date_end,
            chamber=chamber,
            offset=offset,
            limit=limit,
        )

        if total == 0 and self._index_is_empty():
            # Do NOT let an unpopulated index masquerade as "nothing was said about this".
            return {
                "error": "not_indexed",
                "message": (
                    f"{self._label} has no queryable debates API, so its speeches are served from "
                    f"a periodically ingested index — which currently holds no data. {self._ingest_hint}"
                ),
                "results": [], "total": 0, "jurisdiction": self._jurisdiction,
            }

        results = [r.to_speech_result(snippet_query=query) for r in rows]
        next_offset = offset + len(rows)
        envelope = to_results_envelope(
            results,
            jurisdiction=self._jurisdiction,
            total=total,
            cursor=str(next_offset) if next_offset < total else None,
            truncated=next_offset < total,
        )
        envelope["coverage_note"] = self._coverage_note
        return envelope

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        row = self._store.get(jurisdiction=self._jurisdiction, doc_id=doc_id)
        if row is None:
            return {
                "results": [], "total": 0, "jurisdiction": self._jurisdiction, "doc_id": doc_id,
                "text": "", "message": "speech not found in the index",
            }
        excerpt = textnorm.snippet_around(row.full_text, query, max_chars=max_chars)
        return {
            "results": [], "total": 0, "jurisdiction": self._jurisdiction,
            "doc_id": doc_id,
            "title": row.title,
            "date": row.date,
            "source_url": row.source_url,
            "text": excerpt,
            "text_total_chars": len(row.full_text),
            "truncated": len(row.full_text) > len(excerpt),
            "language_original": row.language_original,
            "language_text": row.language_text,
            "is_translation": row.is_translation,  # nosemgrep: is-function-without-parentheses - dataclass field
            "text_status": row.text_status,
            "coverage_note": self._coverage_note,
        }

    # ---- helpers ----------------------------------------------------------------

    def _index_is_empty(self) -> bool:
        """True when this jurisdiction has no rows at all (as opposed to no matches)."""
        _, total = self._store.query(
            jurisdiction=self._jurisdiction, terms=[], offset=0, limit=1
        )
        return total == 0


def _parse_offset(cursor: Optional[str]) -> int:
    try:
        return max(0, int(cursor))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
