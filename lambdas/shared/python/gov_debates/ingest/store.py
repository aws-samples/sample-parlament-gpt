"""Storage and query for the batch-ingested jurisdictions.

Two implementations behind one interface:

  * :class:`InMemorySpeechStore` — used by tests and by a warm Lambda that has already loaded a
    shard. No dependencies.
  * :class:`S3SpeechStore` — reads newline-delimited JSON shards from S3, partitioned by
    jurisdiction and month, with a small in-process cache. Ingest jobs write the same layout.

The S3 layout is deliberately simple and month-partitioned::

    s3://<bucket>/<prefix>/<jurisdiction>/<YYYY-MM>.jsonl

Month partitioning matches how every one of these three sources must be crawled anyway (France and
Australia have no date-range query worth trusting, so ingestion walks windows), and it means a
date-bounded query reads only the shards it needs instead of scanning the corpus.

This is intentionally NOT a search engine. It is an honest, cheap index that supports the filters
the tool schema actually exposes (free text, speaker, date range, chamber). If relevance ranking or
fuzzy matching is needed later, the store interface is the seam to swap in OpenSearch or a vector
index without touching the adapters.
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable, Iterator, Optional, Protocol

from .documents import IndexedSpeech, matches, tokenize


class SpeechStore(Protocol):
    """The query surface the batch-source adapters depend on."""

    def query(
        self,
        *,
        jurisdiction: str,
        terms: list[str],
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        chamber: Optional[str] = None,
        offset: int = 0,
        limit: int = 5,
    ) -> tuple[list[IndexedSpeech], int]:
        """Return (page of matches, total match count)."""
        ...

    def get(self, *, jurisdiction: str, doc_id: str) -> Optional[IndexedSpeech]:
        """Fetch one speech by id, or None."""
        ...


def _in_range(date: str, start: Optional[str], end: Optional[str]) -> bool:
    # ISO dates compare correctly as strings, which is why the contract mandates ISO.
    if start and date < start:
        return False
    if end and date > end:
        return False
    return True


def _filter_and_page(
    rows: Iterable[IndexedSpeech],
    *,
    terms: list[str],
    speaker: Optional[str],
    date_start: Optional[str],
    date_end: Optional[str],
    chamber: Optional[str],
    offset: int,
    limit: int,
) -> tuple[list[IndexedSpeech], int]:
    from .documents import fold

    speaker_folded = fold(speaker) if speaker else None
    chamber_folded = fold(chamber) if chamber else None

    hits: list[IndexedSpeech] = []
    for row in rows:
        if not _in_range(row.date, date_start, date_end):
            continue
        if speaker_folded and speaker_folded not in fold(row.speaker):
            continue
        if chamber_folded and chamber_folded not in fold(row.chamber):
            continue
        if terms and not matches(row, terms):
            continue
        hits.append(row)

    # Newest first, matching every live adapter's default ordering.
    hits.sort(key=lambda r: r.date, reverse=True)
    return hits[offset : offset + limit], len(hits)


class InMemorySpeechStore:
    """A store backed by a list. Used in tests and for a pre-loaded shard."""

    def __init__(self, speeches: Iterable[IndexedSpeech] = ()) -> None:
        self._rows: list[IndexedSpeech] = list(speeches)

    def add(self, speech: IndexedSpeech) -> None:
        self._rows.append(speech)

    def __len__(self) -> int:
        return len(self._rows)

    def query(
        self,
        *,
        jurisdiction: str,
        terms: list[str],
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        chamber: Optional[str] = None,
        offset: int = 0,
        limit: int = 5,
    ) -> tuple[list[IndexedSpeech], int]:
        rows = (r for r in self._rows if r.jurisdiction == jurisdiction)
        return _filter_and_page(
            rows, terms=terms, speaker=speaker, date_start=date_start, date_end=date_end,
            chamber=chamber, offset=offset, limit=limit,
        )

    def get(self, *, jurisdiction: str, doc_id: str) -> Optional[IndexedSpeech]:
        for row in self._rows:
            if row.jurisdiction == jurisdiction and row.doc_id == doc_id:
                return row
        return None


class S3SpeechStore:
    """Reads month-partitioned JSONL shards from S3.

    Shard key: ``{prefix}/{jurisdiction}/{YYYY-MM}.jsonl``. A date-bounded query reads only the
    shards covering that range; an unbounded one lists the jurisdiction's prefix. Shards are cached
    per container to keep warm invocations cheap.
    """

    def __init__(
        self,
        bucket: Optional[str] = None,
        prefix: str = "speeches",
        *,
        client: Any = None,
        max_shards: int = 24,
    ) -> None:
        self._bucket = bucket or os.getenv("INDEX_BUCKET", "")
        if not self._bucket:
            raise ValueError("INDEX_BUCKET is not configured for the batch-ingest store")
        self._prefix = prefix.strip("/")
        self._client = client
        self._max_shards = max_shards
        self._cache: dict[str, list[IndexedSpeech]] = {}

    # -- s3 plumbing ---------------------------------------------------------------

    @property
    def s3(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily so unit tests need no AWS deps

            self._client = boto3.client("s3")
        return self._client

    def shard_key(self, jurisdiction: str, month: str) -> str:
        return f"{self._prefix}/{jurisdiction}/{month}.jsonl"

    def _load_shard(self, jurisdiction: str, month: str) -> list[IndexedSpeech]:
        key = self.shard_key(jurisdiction, month)
        if key in self._cache:
            return self._cache[key]
        rows: list[IndexedSpeech] = []
        try:
            body = self.s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except Exception:
            # A missing shard simply means no sittings that month.
            self._cache[key] = rows
            return rows
        text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(IndexedSpeech.from_dict(json.loads(line)))
            except (ValueError, TypeError):
                continue
        self._cache[key] = rows
        return rows

    def _months(self, jurisdiction: str, start: Optional[str], end: Optional[str]) -> list[str]:
        if start and end:
            return _month_range(start, end, cap=self._max_shards)
        # No bounds: list what exists, newest first, capped.
        return self._list_months(jurisdiction)[: self._max_shards]

    def _list_months(self, jurisdiction: str) -> list[str]:
        months: list[str] = []
        token: Optional[str] = None
        prefix = f"{self._prefix}/{jurisdiction}/"
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = self.s3.list_objects_v2(**kwargs)
            except Exception:
                break
            for obj in page.get("Contents") or []:
                name = str(obj.get("Key", "")).rsplit("/", 1)[-1]
                if name.endswith(".jsonl"):
                    months.append(name[: -len(".jsonl")])
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return sorted(months, reverse=True)

    # -- query surface -------------------------------------------------------------

    def query(
        self,
        *,
        jurisdiction: str,
        terms: list[str],
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        chamber: Optional[str] = None,
        offset: int = 0,
        limit: int = 5,
    ) -> tuple[list[IndexedSpeech], int]:
        rows: list[IndexedSpeech] = []
        for month in self._months(jurisdiction, date_start, date_end):
            rows.extend(self._load_shard(jurisdiction, month))
        return _filter_and_page(
            rows, terms=terms, speaker=speaker, date_start=date_start, date_end=date_end,
            chamber=chamber, offset=offset, limit=limit,
        )

    def get(self, *, jurisdiction: str, doc_id: str) -> Optional[IndexedSpeech]:
        # A doc_id carries its month prefix where the adapters can supply it; otherwise scan the
        # most recent shards. Adapters for these sources embed the date in the id to make this cheap.
        month = _month_from_doc_id(doc_id)
        months = [month] if month else self._list_months(jurisdiction)[: self._max_shards]
        for m in months:
            for row in self._load_shard(jurisdiction, m):
                if row.doc_id == doc_id:
                    return row
        return None

    # -- write side (used by ingest jobs) -------------------------------------------

    def put_shard(self, jurisdiction: str, month: str, speeches: Iterable[IndexedSpeech]) -> int:
        """Write (replace) one month shard. Returns the row count written."""
        lines = [json.dumps(s.to_dict(), ensure_ascii=False) for s in speeches]
        body = ("\n".join(lines) + "\n").encode("utf-8")
        self.s3.put_object(
            Bucket=self._bucket,
            Key=self.shard_key(jurisdiction, month),
            Body=body,
            ContentType="application/x-ndjson",
        )
        self._cache.pop(self.shard_key(jurisdiction, month), None)
        return len(lines)


def _month_range(start: str, end: str, *, cap: int) -> list[str]:
    """Inclusive list of YYYY-MM shards spanning [start, end], newest first, capped."""
    try:
        sy, sm = int(start[0:4]), int(start[5:7])
        ey, em = int(end[0:4]), int(end[5:7])
    except (ValueError, IndexError):
        return []
    months: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em) and len(months) < cap * 4:
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return list(reversed(months))[:cap]


def _month_from_doc_id(doc_id: str) -> Optional[str]:
    """Extract a YYYY-MM prefix from a doc_id that embeds its date."""
    if not isinstance(doc_id, str) or len(doc_id) < 7:
        return None
    import re

    m = re.search(r"(\d{4})-(\d{2})", doc_id)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def store_from_env(*, client: Any = None) -> SpeechStore:
    """Build the configured store. Falls back to an empty in-memory store when unconfigured."""
    bucket = os.getenv("INDEX_BUCKET", "")
    if not bucket:
        return InMemorySpeechStore()
    return S3SpeechStore(bucket, os.getenv("INDEX_PREFIX", "speeches"), client=client)
