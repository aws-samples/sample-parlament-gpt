"""Pagination helpers for the three upstream models the sources actually use.

Verified per-source (docs/multi-gov/source-profiles):
  * cursor_pages  — opaque forward-only cursor that is ALWAYS present; terminate when the
    cursor stops changing (DIP/Solr cursorMark). NOT "stop when cursor absent".
  * offset_pages  — skip/take or offset/limit with a reported total (UK Hansard `take<=100`
    + `TotalResultCount`; EU `offset<10000`).
  * token_pages   — a nextLink / continuation token that is absent on the last page
    (OData `@odata.nextLink`).

Each helper is a generator over raw upstream page payloads and enforces a hard page cap so a
misbehaving source (e.g. a cursor that never stabilizes) cannot loop forever inside a Lambda.
Callers stop early once they have collected ``max_results`` rows.
"""
from __future__ import annotations

from typing import Any, Callable, Iterator, Optional

# Absolute ceiling on upstream round-trips per tool call, regardless of what the source
# reports. Protects the Lambda timeout and the 6 MB response cap.
DEFAULT_PAGE_CAP = 25


def cursor_pages(
    fetch: Callable[[Optional[str]], Any],
    *,
    extract_cursor: Callable[[Any], Optional[str]],
    start_cursor: Optional[str] = None,
    page_cap: int = DEFAULT_PAGE_CAP,
) -> Iterator[Any]:
    """Yield pages from a forward-only opaque-cursor source (e.g. DIP Solr cursorMark).

    ``fetch(cursor)`` returns a page payload. ``extract_cursor(page)`` returns the cursor
    for the next request. Terminates when the returned cursor equals the one we sent (the
    documented DIP termination condition) or is falsy, or when ``page_cap`` is reached.
    """
    sent = start_cursor
    seen: set[str] = set()
    for _ in range(page_cap):
        page = fetch(sent)
        yield page
        nxt = extract_cursor(page)
        if not nxt or nxt == sent or nxt in seen:
            return
        seen.add(nxt)
        sent = nxt


def offset_pages(
    fetch: Callable[[int, int], Any],
    *,
    extract_total: Callable[[Any], Optional[int]],
    page_size: int,
    start_offset: int = 0,
    max_offset: Optional[int] = None,
    page_cap: int = DEFAULT_PAGE_CAP,
) -> Iterator[Any]:
    """Yield pages from an offset/limit source that reports a total.

    ``fetch(offset, size)`` returns a page. ``extract_total(page)`` returns the grand total
    (read once, from the first page). ``max_offset`` guards a source that 500s past a deep
    offset (EU rejects ``offset>=10000``).
    """
    offset = start_offset
    total: Optional[int] = None
    for _ in range(page_cap):
        if max_offset is not None and offset >= max_offset:
            return
        page = fetch(offset, page_size)
        yield page
        if total is None:
            total = extract_total(page)
        offset += page_size
        if total is not None and offset >= total:
            return


def token_pages(
    fetch: Callable[[Optional[str]], Any],
    *,
    extract_token: Callable[[Any], Optional[str]],
    start_token: Optional[str] = None,
    page_cap: int = DEFAULT_PAGE_CAP,
) -> Iterator[Any]:
    """Yield pages from a next-token/nextLink source that omits the token on the last page.

    Terminates when ``extract_token(page)`` returns falsy or ``page_cap`` is reached.
    """
    token = start_token
    seen: set[str] = set()
    for _ in range(page_cap):
        page = fetch(token)
        yield page
        nxt = extract_token(page)
        if not nxt or nxt in seen:
            return
        seen.add(nxt)
        token = nxt


def clamp_max_results(value: Any, *, lo: int = 1, hi: int = 50, default: int = 5) -> int:
    """Clamp a caller-supplied ``max_results`` into ``[lo, hi]``.

    The tool schema subset has no numeric bounds (no ``minimum``/``maximum``), so bounds are
    enforced here. Non-integer input falls back to ``default``.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))
