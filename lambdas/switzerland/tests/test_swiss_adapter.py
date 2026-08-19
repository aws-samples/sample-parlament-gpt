"""Swiss (ws.parlament.ch OData v2) adapter tests, on VERIFIED response shapes (ch.json).

Each test pins a trap that would otherwise silently corrupt results: missing Language pin (5x
duplicates), Language mistaken for a translation axis, unfiltered Type (vote/agenda rows), v4
contains() syntax, unbounded date range (43-85 s latency), surname-first speaker names, and the
pd_text/control-token envelope.
"""
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from parlament_ch import (
    CORPUS_END,
    CORPUS_START,
    LANGUAGE_AXIS,
    SPOKEN_TYPE,
    SwissAdapter,
    _chamber_code,
    _clean_speech_text,
    _date_window,
    _speaker_name,
    _split_doc_id,
    _unwrap,
)

TRANSCRIPT = {
    "ID": "377739",                    # Int64 serialized as a STRING
    "Language": "DE",
    "LanguageOfText": "FR",            # a French speech on the DE label axis
    "Text": "<pd_text><p>[GZ]Monsieur le President, parlons du <i>climat</i>.[PAGE 12]</p></pd_text>",
    "SpeakerFullName": "Rösti Albert",  # SURNAME FIRST
    "SpeakerFunction": "BR-M",
    "ParlGroupAbbreviation": "V",
    "ParlGroupName": "Fraktion der Schweizerischen Volkspartei",
    "CantonAbbreviation": "BE",
    "CouncilName": "Bundesrat",
    "MeetingCouncilAbbreviation": "N",
    "MeetingDate": "20241218",         # YYYYMMDD STRING
    "IdSession": "5206",               # string on Transcript
    "IdSubject": "66699",
    "PersonNumber": 4246,
    "Type": 1,
}

SUBJECT_BUSINESS = {
    "d": {"results": [{"IdSubject": "66699", "TitleDE": "Klimapolitik", "TitleFR": "Politique climatique",
                       "TitleIT": None}]}
}


def _payload(rows, count="1", next_url=None):
    d = {"results": rows, "__count": count}
    if next_url:
        d["__next"] = next_url
    return {"d": d}


def _adapter(seen, transcript_payload=None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("ws.parlament.ch", "www.parlament.ch")
        path = request.url.path
        seen.append((path, parse_qs(request.url.query.decode())))
        if "SubjectBusiness" in path:
            return httpx.Response(200, json=SUBJECT_BUSINESS)
        return httpx.Response(200, json=transcript_payload or _payload([TRANSCRIPT]))
    client = PinnedHttpClient(
        "ws.parlament.ch,www.parlament.ch",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return SwissAdapter(client)


def _filter_of(seen):
    for path, qs in seen:
        if path.endswith("/Transcript") and "$filter" in qs:
            return qs["$filter"][0]
    raise AssertionError("no Transcript query captured")


# --- search ----------------------------------------------------------------------

def test_search_normalizes_a_transcript():
    seen = []
    out = _adapter(seen).search(query="climat", date_start="2024-01-01", date_end="2024-12-31")
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "ch"
    assert row["speaker"] == "Albert Rösti"          # reordered from surname-first
    assert row["group"] == "V"                        # Fraktion
    assert row["party"] is None                       # never inferred from the Fraktion
    assert row["role"] == "BR-M"
    assert row["date"] == "2024-12-18"                # YYYYMMDD string normalized
    assert row["chamber"] == "Nationalrat"
    assert row["title"] == "Klimapolitik"             # via the SubjectBusiness join
    assert row["doc_id"] == "377739@DE"               # composite key
    assert row["session_ref"] == "5206"
    assert out["total"] == 1
    # Swiss terms require displaying the download date (COMPLIANCE.md C2): every record
    # must carry an ISO retrieval date for the UI to render beside the attribution.
    import re as _re

    assert _re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["extras"]["retrieved_at"])


def test_language_of_text_is_used_not_the_language_axis():
    # The row is on the DE label axis but the speech was delivered in French. Reporting "de"
    # here would mislabel every French and Italian speech in the corpus.
    out = _adapter([]).search(query="climat")
    row = out["results"][0]
    assert row["language_original"] == "fr"
    assert row["language_text"] == "fr"
    assert row["is_translation"] is False   # Text is always as-spoken


def test_language_is_always_pinned():
    # Without the pin the composite key (ID, Language) yields 5x duplicate rows per speech.
    seen = []
    _adapter(seen).search(query="climat")
    assert f"Language eq '{LANGUAGE_AXIS}'" in _filter_of(seen)


def test_type_is_always_filtered_to_spoken_contributions():
    # Type=2 is vote-tally text and Type=3 is agenda headings; both have null speakers.
    seen = []
    _adapter(seen).search(query="climat")
    assert f"Type eq {SPOKEN_TYPE}" in _filter_of(seen)


def test_date_range_is_always_bounded_even_without_caller_dates():
    # An unbounded substringof over Text measured 43-85 s cold and would blow the Lambda timeout.
    seen = []
    _adapter(seen).search(query="climat")
    f = _filter_of(seen)
    assert "MeetingDate ge '" in f and "MeetingDate le '" in f


def test_freetext_uses_odata_v2_substringof_argument_order():
    # v4's contains() returns HTTP 400 here, and v2's argument order is (needle, haystack).
    seen = []
    _adapter(seen).search(query="Klimawandel")
    f = _filter_of(seen)
    assert "substringof('Klimawandel',Text)" in f
    assert "contains(" not in f


def test_single_quotes_in_a_query_are_escaped():
    seen = []
    _adapter(seen).search(query="l'énergie")
    assert "substringof('l''énergie',Text)" in _filter_of(seen)


def test_speaker_filter_is_order_independent():
    # SpeakerFullName is surname-first, so eq on "Albert Rösti" would return zero rows.
    seen = []
    _adapter(seen).search(speaker="Albert Rösti")
    f = _filter_of(seen)
    assert "substringof('Albert',SpeakerFullName)" in f
    assert "substringof('Rösti',SpeakerFullName)" in f


def test_chamber_filter_maps_to_council_code():
    seen = []
    _adapter(seen).search(query="x", chamber="Nationalrat")
    assert "MeetingCouncilAbbreviation eq 'N'" in _filter_of(seen)


def test_select_excludes_nothing_needed_and_forces_object_form():
    seen = []
    _adapter(seen).search(query="x")
    _, qs = [(p, q) for p, q in seen if p.endswith("/Transcript")][0]
    assert qs["$inlinecount"] == ["allpages"]   # forces {"d":{...}} so __next can appear
    assert "Text" in qs["$select"][0]
    assert qs["$orderby"] == ["MeetingDate desc"]


def test_server_driven_next_link_is_followed_verbatim():
    next_url = "https://ws.parlament.ch/odata.svc/Transcript?$skiptoken=1005L,'DE'"
    seen = []
    out = _adapter(seen, transcript_payload=_payload([TRANSCRIPT], next_url=next_url)).search(query="x")
    assert out["cursor"] == next_url
    assert out["truncated"] is True

    # Following it must hit that exact URL, not a hand-built skiptoken.
    seen2 = []
    _adapter(seen2).search(cursor=next_url)
    transcript_calls = [qs for path, qs in seen2 if path.endswith("/Transcript")]
    assert transcript_calls, "cursor was not followed"
    assert "$skiptoken" in transcript_calls[0]
    # And it must NOT rebuild the filter/order params (the token already encodes them).
    assert "$filter" not in transcript_calls[0]


def test_degraded_list_response_shape_is_handled():
    # With $format=json and no $inlinecount the service can return {"d": [...]} with no __next.
    out = _adapter([], transcript_payload={"d": [TRANSCRIPT]}).search(query="x")
    assert len(out["results"]) == 1
    # The envelope omits `cursor` entirely when there is no next page (rather than nulling it).
    assert out.get("cursor") is None
    assert out["truncated"] is False


# --- get_text ---------------------------------------------------------------------

def test_get_text_uses_the_composite_key_and_scrubs_markup():
    seen = []
    out = _adapter(seen).get_text(doc_id="377739@DE", query="climat")
    path = [p for p, _ in seen if "Transcript(" in p]
    assert path, "composite-key fetch not made"
    assert "ID=377739L" in path[0] and "Language='DE'" in path[0]
    # The pd_text envelope and control tokens must not leak into the text.
    assert "<pd_text>" not in out["text"] and "[GZ]" not in out["text"] and "[PAGE" not in out["text"]
    assert "climat" in out["text"]
    assert out["language_original"] == "fr"


def test_get_text_defaults_the_language_axis_for_a_bare_id():
    seen = []
    _adapter(seen).get_text(doc_id="377739")
    path = [p for p, _ in seen if "Transcript(" in p][0]
    assert "Language='DE'" in path


def test_get_text_requires_doc_id():
    assert _adapter([]).get_text(doc_id="")["error"] == "bad_argument"


# --- pure helpers -----------------------------------------------------------------

def test_date_window_defaults_to_corpus_edges():
    assert _date_window(None, None) == (CORPUS_START, CORPUS_END)
    assert _date_window("2024-01-01", "2024-12-31") == ("20240101", "20241231")
    assert _date_window("2024-01-01", None) == ("20240101", CORPUS_END)


@pytest.mark.parametrize("value,expected", [
    ("N", "N"), ("S", "S"), ("n", "N"),
    ("Nationalrat", "N"), ("National Council", "N"),
    ("Ständerat", "S"), ("Council of States", "S"),
    ("Bundestag", None), (None, None),
])
def test_chamber_code(value, expected):
    assert _chamber_code(value) == expected


def test_clean_speech_text_strips_envelope_and_tokens():
    raw = "<pd_text><p>[GZ]Hello [NB]world[PAGE 3]</p></pd_text>"
    assert _clean_speech_text(raw) == "Hello world"
    assert _clean_speech_text(None) == ""


@pytest.mark.parametrize("full,expected", [
    ("Rösti Albert", "Albert Rösti"),
    ("Hübscher Martin", "Martin Hübscher"),
    ("Von Der Leyen Ursula X", "Von Der Leyen Ursula X"),  # >2 tokens left as-is
    (None, None), ("", None),
])
def test_speaker_name_reordering(full, expected):
    assert _speaker_name(full) == expected


@pytest.mark.parametrize("doc_id,expected", [
    ("377739@DE", ("377739", "DE")),
    ("377739@FR", ("377739", "FR")),
    ("377739", ("377739", "DE")),
    ("", (None, "DE")),
    (None, (None, "DE")),
])
def test_split_doc_id(doc_id, expected):
    assert _split_doc_id(doc_id) == expected


def test_unwrap_handles_both_shapes_and_string_counts():
    rows, total, nxt = _unwrap(_payload([TRANSCRIPT], count="42"))
    assert len(rows) == 1 and total == 42 and nxt is None
    rows, total, nxt = _unwrap({"d": [TRANSCRIPT]})
    assert len(rows) == 1 and total is None
    assert _unwrap(None) == ([], None, None)
