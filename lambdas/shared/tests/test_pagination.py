"""Tests for the three pagination models, including the DIP termination trap."""
from gov_debates.http.pagination import (
    clamp_max_results,
    cursor_pages,
    offset_pages,
    token_pages,
)


def test_cursor_pages_terminates_when_cursor_stops_changing():
    # DIP: cursor is ALWAYS present; stop when it repeats, NOT when it is absent.
    pages = {None: {"c": "A"}, "A": {"c": "B"}, "B": {"c": "B"}}  # B -> B is the terminal
    seen = list(cursor_pages(lambda c: pages[c], extract_cursor=lambda p: p["c"]))
    assert [p["c"] for p in seen] == ["A", "B", "B"]


def test_cursor_pages_respects_page_cap():
    seen = list(cursor_pages(lambda c: {"c": "never-stable" + str(c)},
                             extract_cursor=lambda p: p["c"], page_cap=3))
    assert len(seen) == 3


def test_offset_pages_stops_at_total():
    total = 5
    calls = []

    def fetch(offset, size):
        calls.append((offset, size))
        return {"total": total, "rows": list(range(offset, min(offset + size, total)))}

    pages = list(offset_pages(fetch, extract_total=lambda p: p["total"], page_size=2))
    assert calls == [(0, 2), (2, 2), (4, 2)]     # 3 pages cover 5 rows
    assert len(pages) == 3


def test_offset_pages_honors_max_offset():
    # EU rejects offset >= 10000; stop before requesting it.
    calls = []

    def fetch(offset, size):
        calls.append(offset)
        return {"total": 1_000_000}

    list(offset_pages(fetch, extract_total=lambda p: p["total"], page_size=4000, max_offset=10000))
    assert max(calls) < 10000


def test_token_pages_stops_when_token_absent():
    pages = {None: {"t": "n1"}, "n1": {"t": "n2"}, "n2": {"t": None}}
    seen = list(token_pages(lambda t: pages[t], extract_token=lambda p: p["t"]))
    assert len(seen) == 3


def test_clamp_max_results():
    assert clamp_max_results(999) == 50
    assert clamp_max_results(0) == 1
    assert clamp_max_results("garbage") == 5
    assert clamp_max_results(20) == 20
