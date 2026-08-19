"""US (GovInfo Congressional Record) adapter tests, on VERIFIED shapes (us.json).

Pins the traps: the mandatory collection:CREC scope (without which statute text leaks in),
POST-only search, the N+1 enrichment for speaker/party, granule-is-not-a-speech, exclusion of
Extensions of Remarks / Daily Digest, the keyless text host (so the API key cannot leak), and the
malformed .htm payload.
"""
import json

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from govinfo import (
    COLLECTION,
    GovInfoAdapter,
    _build_query,
    _chamber_letter,
    _clean_granule_text,
    _congress,
    _member_token,
    _package_id_from_granule,
    _page_prefix,
)

HIT_HOUSE = {
    "granuleId": "CREC-2026-07-20-pt1-PgH4667",
    "packageId": "CREC-2026-07-20",
    "title": "NEXT-GENERATION GEOTHERMAL ENERGY ACT",
    "dateIssued": "2026-07-20",
}
HIT_EXTENSION = {
    "granuleId": "CREC-2026-07-20-pt1-PgE1234",   # Extensions of Remarks — NOT spoken
    "packageId": "CREC-2026-07-20",
    "title": "TRIBUTE",
    "dateIssued": "2026-07-20",
}
HIT_SENATE = {
    "granuleId": "CREC-2026-07-20-pt1-PgS4146-7",
    "packageId": "CREC-2026-07-20",
    "title": "NOMINATIONS",
    "dateIssued": "2026-07-20",
}

SUMMARY = {
    "title": "NEXT-GENERATION GEOTHERMAL ENERGY ACT",
    "dateIssued": "2026-07-20",
    "congress": "119",
    "granuleClass": "HOUSE",
    "pagePrefix": "H4667",
    "detailsLink": "https://www.govinfo.gov/app/details/CREC-2026-07-20/CREC-2026-07-20-pt1-PgH4667",
    "governmentAuthor": ["Congress"],
    "members": [
        {"role": "SPEAKING", "memberName": "Babin, Brian", "party": "R", "bioGuideId": "B001291",
         "chamber": "HOUSE"},
        {"role": "SPEAKING", "memberName": "Salinas, Andrea", "party": "D", "bioGuideId": "S001226",
         "chamber": "HOUSE"},
    ],
}

GRANULE_HTML = (
    "</pre></body></html><html><head><title>Congressional Record, Volume 172 Issue 118"
    "</title></head><body><pre>[Congressional Record Volume 172, Number 118]\n[House]\n"
    "[Pages H4667-H4670]\nNEXT-GENERATION GEOTHERMAL ENERGY ACT\n"
    "The Clerk read the title of the bill.\n"
    "Mr. BABIN. Madam Speaker, geothermal energy is the future of baseload power.\n"
    "Ms. SALINAS. Madam Speaker, I rise in support of this measure.\n"
)


def _adapter(seen, search_payload=None, summary=None, html=GRANULE_HTML):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("api.govinfo.gov", "www.govinfo.gov")
        entry = {"method": request.method, "host": request.url.host, "path": request.url.path,
                 "query": str(request.url.query.decode()),
                 "headers": dict(request.headers),
                 "body": request.content.decode() if request.content else ""}
        seen.append(entry)
        if request.method == "POST":
            return httpx.Response(200, json=search_payload or {"count": 9433, "results": [HIT_HOUSE],
                                                               "offsetMark": "NEXTMARK"})
        if "/summary" in request.url.path:
            return httpx.Response(200, json=SUMMARY if summary is None else summary)
        return httpx.Response(200, text=html)
    client = PinnedHttpClient(
        "api.govinfo.gov,www.govinfo.gov",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return GovInfoAdapter(client, api_key="test-key")


def _search_body(seen):
    for e in seen:
        if e["method"] == "POST":
            return json.loads(e["body"])
    raise AssertionError("no search POST captured")


# --- search ----------------------------------------------------------------------

def test_search_enriches_and_emits_one_row_per_speaker():
    # A granule is an agenda item with several speakers; one row each keeps `speaker` honest.
    seen = []
    out = _adapter(seen).search(query="geothermal energy", max_results=10)
    speakers = [r["speaker"] for r in out["results"]]
    assert speakers == ["Babin, Brian", "Salinas, Andrea"]
    for row in out["results"]:
        validate(row)
        assert row["jurisdiction"] == "us"
        assert row["chamber"] == "House"
        assert row["term"] == "119"
        assert row["group"] is None           # US members have a party, not a caucus group
        assert row["extras"]["speakers"] == ["Babin, Brian", "Salinas, Andrea"]
    assert out["results"][0]["party"] == "Republican"
    assert out["results"][1]["party"] == "Democrat"
    assert out["total"] == 9433


def test_collection_scope_is_always_present():
    # Without collection:CREC, GovInfo searches the CFR, US Code and bills — statute text.
    seen = []
    _adapter(seen).search(query="climate")
    assert f"collection:{COLLECTION}" in _search_body(seen)["query"]


def test_search_is_a_post_with_the_key_in_a_header():
    seen = []
    _adapter(seen).search(query="climate")
    post = [e for e in seen if e["method"] == "POST"][0]
    assert post["host"] == "api.govinfo.gov"
    assert post["headers"].get("x-api-key") == "test-key"
    # The key must NOT be in the query string of anything.
    assert "api_key" not in post["query"]


def test_extensions_of_remarks_are_excluded():
    # E pages are words inserted, never spoken aloud; D pages are the Daily Digest.
    seen = []
    payload = {"count": 2, "results": [HIT_EXTENSION, HIT_HOUSE], "offsetMark": "M"}
    out = _adapter(seen, search_payload=payload).search(query="x", max_results=10)
    ids = {r["doc_id"] for r in out["results"]}
    assert HIT_EXTENSION["granuleId"] not in ids
    assert HIT_HOUSE["granuleId"] in ids


def test_chamber_filter_selects_by_page_prefix():
    payload = {"count": 2, "results": [HIT_HOUSE, HIT_SENATE], "offsetMark": "M"}
    out = _adapter([], search_payload=payload).search(query="x", chamber="Senate", max_results=10)
    assert all(r["doc_id"].startswith("CREC-2026-07-20-pt1-PgS") for r in out["results"])


def test_date_range_and_congress_go_into_the_query_string():
    seen = []
    _adapter(seen).search(query="x", date_start="2026-01-01", date_end="2026-03-31", term="119")
    q = _search_body(seen)["query"]
    assert "publishdate:range(2026-01-01,2026-03-31)" in q
    assert "congress:119" in q


def test_speaker_prefers_bioguide_id():
    seen = []
    _adapter(seen).search(speaker="B001291")
    assert "member:B001291" in _search_body(seen)["query"]


def test_cursor_is_forwarded_and_returned():
    seen = []
    out = _adapter(seen).search(query="x", cursor="PREVMARK")
    assert _search_body(seen)["offsetMark"] == "PREVMARK"
    assert out["cursor"] == "NEXTMARK"


def test_no_snippet_is_claimed_because_the_api_provides_none():
    out = _adapter([]).search(query="x")
    assert all(r["snippet"] is None for r in out["results"])


def test_enrichment_failure_degrades_gracefully():
    # A rate-limited summary call must not fail the whole search.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"count": 1, "results": [HIT_HOUSE], "offsetMark": "M"})
        return httpx.Response(429, json={"error": "rate limited"})

    client = PinnedHttpClient(
        "api.govinfo.gov,www.govinfo.gov",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    out = GovInfoAdapter(client, api_key="k").search(query="x")
    row = out["results"][0]
    validate(row)
    assert row["speaker"] is None      # unknown rather than invented
    assert row["party"] is None


def test_granule_with_no_members_still_validates():
    out = _adapter([], summary={"congress": "119", "granuleClass": "HOUSE"}).search(query="x")
    assert len(out["results"]) == 1
    validate(out["results"][0])
    assert out["results"][0]["speaker"] is None


# --- get_text ---------------------------------------------------------------------

def test_get_text_uses_the_keyless_content_host():
    # Citation/text URLs must never carry the api_key.
    seen = []
    out = _adapter(seen).get_text(doc_id=HIT_HOUSE["granuleId"], query="geothermal")
    fetch = [e for e in seen if e["method"] == "GET"][0]
    assert fetch["host"] == "www.govinfo.gov"
    assert "api_key" not in fetch["query"]
    assert "geothermal" in out["text"]


def test_get_text_strips_the_malformed_wrapper_and_header_block():
    out = _adapter([]).get_text(doc_id=HIT_HOUSE["granuleId"])
    assert "<pre>" not in out["text"] and "<html>" not in out["text"]
    assert "[Congressional Record" not in out["text"]
    assert "[Pages H4667-H4670]" not in out["text"]


def test_get_text_drops_procedural_lines():
    out = _adapter([]).get_text(doc_id=HIT_HOUSE["granuleId"])
    assert "Clerk read the title" not in out["text"]


def test_get_text_warns_about_interleaved_statute_text():
    # Granules mix speech with quoted bill text; the caller must be told.
    out = _adapter([]).get_text(doc_id=HIT_HOUSE["granuleId"])
    assert "bill text" in out["note"]


def test_get_text_rejects_a_non_granule_doc_id():
    assert _adapter([]).get_text(doc_id="not-a-granule")["error"] == "bad_argument"
    assert _adapter([]).get_text(doc_id="")["error"] == "bad_argument"


# --- pure helpers -----------------------------------------------------------------

def test_build_query_puts_collection_first_and_quotes_phrases():
    q = _build_query(query="climate change", speaker=None, date_start=None, date_end=None, term=None)
    assert q.startswith(f"collection:{COLLECTION}")
    assert '"climate change"' in q


def test_build_query_single_word_is_not_quoted():
    q = _build_query(query="geothermal", speaker=None, date_start=None, date_end=None, term=None)
    assert "geothermal" in q and '"geothermal"' not in q


def test_build_query_open_ended_date_bounds():
    q = _build_query(query=None, speaker=None, date_start=None, date_end="2026-03-31", term=None)
    assert "publishdate:range(1994-01-01,2026-03-31)" in q


@pytest.mark.parametrize("granule,expected", [
    ("CREC-2026-07-20-pt1-PgH4667", "H"),
    ("CREC-2026-07-20-pt1-PgS4146-7", "S"),
    ("CREC-2026-07-20-pt1-PgE1234", "E"),
    ("nonsense", None),
])
def test_page_prefix(granule, expected):
    assert _page_prefix(granule) == expected


@pytest.mark.parametrize("value,expected", [
    ("House", "H"), ("house", "H"), ("Senate", "S"), ("Bundestag", None), (None, None),
])
def test_chamber_letter(value, expected):
    assert _chamber_letter(value) == expected


def test_package_id_derivation():
    assert _package_id_from_granule("CREC-2026-07-20-pt1-PgH4667") == "CREC-2026-07-20"
    assert _package_id_from_granule("BILLS-119hr1") is None


@pytest.mark.parametrize("value,expected", [
    ("119", 119), ("1", 1), ("0", None), ("999", None), ("abc", None), (None, None),
])
def test_congress_bounds(value, expected):
    assert _congress(value) == expected


def test_member_token_prefers_bioguide_then_surname():
    assert _member_token("B001291") == "B001291"
    assert _member_token("b001291") == "B001291"
    assert _member_token("Brian Babin") == "Babin"


def test_clean_granule_text_handles_empty():
    assert _clean_granule_text("") == ""
