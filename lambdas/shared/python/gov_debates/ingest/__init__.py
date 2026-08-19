"""Batch-ingest support for sources that have NO queryable debates API.

Three of the ten supported jurisdictions cannot be served by a request-path Lambda, and each for a
different, verified reason:

  * **France** — the official search returns HTML only, its ``seance_date`` and ``limit`` params
    are inert (a silent-wrong-results trap), and there is no date-range query at all. The real
    data is a 55.7 MB ZIP per legislature (324 MB unpacked, 601 sitting XML files) that cannot be
    fetched per-document.
  * **Netherlands** — OData ``$search`` is accepted with HTTP 200 and **silently ignored**, and no
    field anywhere holds speech text. Transcripts are 0.9–3 MB whole-meeting XML blobs behind a
    separate ``/resource`` endpoint.
  * **Australia** — search is HTML scraping, full text needs an undocumented per-item endpoint,
    and that text is empty for every record before ~2011.

For these, the only honest design is: ingest on a schedule into our own index, then answer queries
from the index. This package holds the pieces that are common to all three:

  * ``documents`` — the ingest-side document model (a normalized speech plus its index terms).
  * ``store``     — a storage/query interface with an S3-backed implementation and an in-memory
                    one for tests.

The query-path adapters for these jurisdictions read the store; they never call the upstream
source at request time.
"""
from __future__ import annotations

__all__ = ["documents", "store"]
