"""Austria (parlament.gv.at VTS) adapter tests, on VERIFIED response shapes (at.json).

Pins the traps: the UTC date shift that loses a day, the unforgiving date_range format, the
mandatory category scope (without which press releases dominate), POST-only access, the absent
speaker/party fields, the JSON-encoded weiterfuehrender_link, and the era-dependent formats.
"""
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from parlament_at import (
    ITEM_TYPE_PAGE,
    ITEM_TYPE_SCAN,
    ITEM_TYPE_SPEECH,
    AustriaAdapter,
    _chamber_value,
    _date_range,
    _parse_title,
    _session_ref,
    _term_from_link,
    _term_value,
    _text_status,
)

# A 15 December 2023 sitting, stamped as the previous day in UTC (the classic trap).
ROW = {
    "item_id": "22012722",
    "item_id_with_type": "D22012722",
    "item_type": ITEM_TYPE_SPEECH,
    "title": "Teil des Stenogr. Protokolls der 247. Sitzung der XXVII. GP des Nationalrats, "
             "15.12.2023 / 09:05: Abgeordneter Max Mustermann (ÖVP)",
    "datetime": "2023-12-14T23:00:00.000Z",
    "description": 'Der <span class="highlight">Klimaschutz</span> ist...zentral',
    "link": "/dokument/XXVII/NRSITZ/247/imfname_1672234.html",
    "relatedLink": "/XXVII/NRSITZ/247",
    "weiterfuehrender_link": '{"url":"/gegenstand/XXVII/NRSITZ/247","text":"Sitzung"}',
}

PAYLOAD = {"count": 1210, "pages": 242, "meta": {"totalHits": 1210}, "rows": [ROW]}


def _adapter(seen, payload=None, html="<p>Der Klimaschutz ist zentral. (Beifall bei der ÖVP.)</p>"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.parlament.gv.at"
        entry = {
            "method": request.method,
            "path": request.url.path,
            "query": parse_qs(request.url.query.decode()),
            "body": request.content.decode() if request.content else "",
        }
        seen.append(entry)
        if request.method == "POST":
            return httpx.Response(200, json=payload or PAYLOAD)
        return httpx.Response(200, text=html)
    client = PinnedHttpClient(
        "www.parlament.gv.at",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return AustriaAdapter(client)


def _body(seen):
    import json
    for e in seen:
        if e["method"] == "POST":
            return json.loads(e["body"])
    raise AssertionError("no POST captured")


# --- search ----------------------------------------------------------------------

def test_search_normalizes_a_row():
    seen = []
    out = _adapter(seen).search(query="Klimaschutz")
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "at"
    assert row["speaker"] == "Max Mustermann"
    assert row["group"] == "ÖVP"
    assert row["party"] is None
    assert row["role"] == "Abgeordneter"
    assert row["chamber"] == "Nationalrat"
    assert row["term"] == "XXVII"
    assert row["session_ref"] == "XXVII/247"
    assert row["source_url"].startswith("https://www.parlament.gv.at/dokument/")
    assert out["total"] == 1210


def test_utc_stamped_date_is_converted_back_to_the_local_sitting_day():
    # "2023-12-14T23:00:00.000Z" IS the 15 December sitting. Naive slicing would report the 14th.
    out = _adapter([]).search(query="Klimaschutz")
    assert out["results"][0]["date"] == "2023-12-15"


def test_category_scope_is_always_sent():
    # Without category=["Protokolle"] the results are dominated by press releases (33k vs 1.2k).
    seen = []
    _adapter(seen).search(query="Klimaschutz")
    assert _body(seen)["category"] == ["Protokolle"]


def test_search_scope_and_type_are_set_for_full_document_search():
    seen = []
    _adapter(seen).search(query="Klimaschutz")
    body = _body(seen)
    assert body["searchScope"] == ["all"]    # search whole document text, not just titles
    assert body["searchType"] == ["all"]


def test_query_is_a_plain_string_not_an_array():
    # s.sm.query is the one body dimension that is NOT an array.
    seen = []
    _adapter(seen).search(query="Klimaschutz")
    assert _body(seen)["s.sm.query"] == "Klimaschutz"


def test_search_uses_post_not_get():
    # The endpoint returns HTTP 405 for GET.
    seen = []
    _adapter(seen).search(query="x")
    assert seen[0]["method"] == "POST"


def test_pagination_uses_page_not_pagenumber():
    # &pageNumber= belongs to the legacy filter type and returns HTTP 500 here.
    seen = []
    _adapter(seen).search(query="x", max_results=20)
    qs = seen[0]["query"]
    assert qs["page"] == ["1"]
    assert "pageNumber" not in qs
    assert qs["pagesize"] == ["20"]
    assert qs["FBEZ"] == ["VTS_01"]


def test_speaker_folds_into_free_text_because_no_speaker_filter_exists():
    # name_nvg looks like the answer but does NOT index floor speakers (verified count=0).
    seen = []
    _adapter(seen).search(query="Klimaschutz", speaker="Mustermann")
    body = _body(seen)
    assert "name_nvg" not in body
    assert "Mustermann" in body["s.sm.query"]


def test_missing_query_returns_a_soft_error():
    out = _adapter([]).search(query=None)
    assert out["error"] == "bad_argument"
    assert out["results"] == []


def test_date_range_is_sent_in_the_exact_required_format():
    seen = []
    _adapter(seen).search(query="x", date_start="2023-01-01", date_end="2023-12-31")
    rng = _body(seen)["date_range"]
    assert rng == ["2023-01-01T00:00:00.000Z", "2023-12-31T23:59:59.000Z"]


def test_no_date_range_key_when_no_bounds_given():
    seen = []
    _adapter(seen).search(query="x")
    assert "date_range" not in _body(seen)


def test_chamber_and_term_filters():
    seen = []
    _adapter(seen).search(query="x", chamber="Nationalrat", term="XXVII")
    body = _body(seen)
    assert body["gremium"] == ["Nationalrat"]
    assert body["gp_liste"] == ["XXVII"]


def test_unknown_chamber_or_term_is_dropped_not_sent():
    # Unknown body keys/values are silently ignored by the API, so sending junk would degrade to
    # "return everything" — drop it client-side instead.
    seen = []
    _adapter(seen).search(query="x", chamber="House of Commons", term="99")
    body = _body(seen)
    assert "gremium" not in body
    assert "gp_liste" not in body


def test_snippet_markup_is_stripped():
    out = _adapter([]).search(query="Klimaschutz")
    snippet = out["results"][0]["snippet"]
    assert "<span" not in snippet and "highlight" not in snippet
    assert "Klimaschutz" in snippet


def test_weiterfuehrender_link_is_double_parsed_into_extras():
    out = _adapter([]).search(query="x")
    assert out["results"][0]["extras"]["further_link"] == "/gegenstand/XXVII/NRSITZ/247"


def test_page_segmented_records_are_marked_uncorrected():
    row = dict(ROW, item_type=ITEM_TYPE_PAGE)
    out = _adapter([], payload={"count": 1, "rows": [row]}).search(query="x")
    assert out["results"][0]["text_status"] == "uncorrected"


def test_scanned_records_are_marked_scanned():
    row = dict(ROW, item_type=ITEM_TYPE_SCAN)
    out = _adapter([], payload={"count": 1, "rows": [row]}).search(query="x")
    assert out["results"][0]["text_status"] == "scanned"


# --- get_text ---------------------------------------------------------------------

def test_get_text_fetches_and_strips_html():
    seen = []
    out = _adapter(seen).get_text(doc_id=ROW["link"], query="Klimaschutz")
    assert seen[0]["method"] == "GET"
    assert "<p>" not in out["text"]
    assert "Klimaschutz" in out["text"]
    assert out["language_original"] == "de"


def test_get_text_refuses_scanned_pdfs_with_an_explanation():
    # Pre-1996 records are scanned images with no text layer; claiming to extract text would lie.
    out = _adapter([]).get_text(doc_id="/dokument/XVIII/NRSITZ/50/imfname_142035.pdf")
    assert out["text"] == ""
    assert out["text_status"] == "scanned"
    assert "scanned PDF image" in out["message"]


def test_get_text_requires_doc_id():
    assert _adapter([]).get_text(doc_id="")["error"] == "bad_argument"


# --- pure helpers -----------------------------------------------------------------

def test_date_range_format_variants():
    assert _date_range(None, None) is None
    lo, hi = _date_range("2023-05-01", None)
    assert lo == "2023-05-01T00:00:00.000Z" and hi.endswith("Z")
    lo, hi = _date_range(None, "2023-05-01")
    assert hi == "2023-05-01T23:59:59.000Z"
    assert _date_range("garbage", "garbage")[0].endswith("Z")


@pytest.mark.parametrize("value,expected", [
    ("Nationalrat", "Nationalrat"), ("nationalrat", "Nationalrat"),
    ("Bundesrat", "Bundesrat"), ("Bundesversammlung", "Bundesversammlung"),
    ("National Council", "Nationalrat"), ("Commons", None), (None, None),
])
def test_chamber_value(value, expected):
    assert _chamber_value(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("XXVII", "XXVII"), ("xxvii", "XXVII"), ("27", None), ("", None), (None, None),
])
def test_term_value_requires_roman_numerals(value, expected):
    assert _term_value(value) == expected


def test_parse_title_extracts_speaker_club_and_role():
    speaker, group, role = _parse_title(ROW["title"])
    assert speaker == "Max Mustermann"
    assert group == "ÖVP"
    assert role == "Abgeordneter"


def test_parse_title_handles_a_minister_without_a_club():
    title = "Teil des Stenogr. Protokolls ..., 15.12.2023 / 10:00: Bundesministerin Anna Beispiel"
    speaker, group, role = _parse_title(title)
    assert speaker == "Anna Beispiel"
    assert group is None
    assert role == "Bundesministerin"


def test_parse_title_returns_nothing_for_an_unparseable_title():
    assert _parse_title("Random heading") == (None, None, None)
    assert _parse_title("") == (None, None, None)


@pytest.mark.parametrize("link,expected", [
    ("/XXVII/NRSITZ/247", "XXVII"),
    ("/dokument/XXI/BRSITZ/994/x.html", "XXI"),
    ("/no/term/here", None),
])
def test_term_from_link(link, expected):
    assert _term_from_link(link) == expected


def test_session_ref_formatting():
    assert _session_ref("/XXVII/NRSITZ/247") == "XXVII/247"
    assert _session_ref("") is None


@pytest.mark.parametrize("item_type,expected", [
    (ITEM_TYPE_SPEECH, "final"), (ITEM_TYPE_PAGE, "uncorrected"),
    (ITEM_TYPE_SCAN, "scanned"), ("", "final"),
])
def test_text_status_by_era(item_type, expected):
    assert _text_status(item_type) == expected
