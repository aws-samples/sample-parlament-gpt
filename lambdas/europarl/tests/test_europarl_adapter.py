"""European Parliament adapter tests, built on the VERIFIED live response shapes (eu.json).

Each test here pins a trap that would otherwise fail SILENTLY or produce wrong data:
204-empty-body, missing Accept header, single-day date semantics, offset ceiling, relevance
sort, written statements leaking in, and machine translation presented as verbatim speech.
"""
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from europarl import (
    ACTIVITY_TYPE,
    MAX_OFFSET,
    EuroparlAdapter,
    EuThrottled,
    _date_window,
    _delivered_language,
    _is_translation,
    _original_language,
    _pick_fragment,
    _source_url,
    _speaker_and_group,
    _term,
)

# A real-shaped Spanish-original speech with an English MT variant.
XML_ES = (
    '<oralStatements><speech startTime="10:01"><from>'
    '<person refersTo="epdata:person/28298">Iratxe Garcia Perez</person>, '
    '<role>on behalf of the Group <organization refersTo="epdata:org/7038">S&amp;D</organization></role>'
    "</from><blockContainer><p>Senora Presidenta, hablemos de Ucrania.</p></blockContainer></speech></oralStatements>"
)
XML_EN_MT = (
    '<oralStatements><speech startTime="10:01"><from>'
    '<person refersTo="epdata:person/28298">Iratxe Garcia Perez</person>, '
    '<role>on behalf of the Group <organization refersTo="epdata:org/7038">S&amp;D</organization></role>'
    "</from><blockContainer><p>Madam President, let us talk about Ukraine.</p></blockContainer></speech></oralStatements>"
)

SPEECH_ROW = {
    "id": "eli/dl/event/MTG-PL-2024-12-18-OTH-2017008949660",
    "activity_id": "MTG-PL-2024-12-18-OTH-2017008949660",
    "activity_date": "2024-12-18",
    "activity_label": {"en": "Preparation of the European Council of 19-20 December 2024"},
    "notation_speechId": "ITM-006-01",
    "had_participation": {"participation_in_name_of": "org/S_D"},
    "recorded_in_a_realization_of": [
        {
            "identifier": "CRE-10-2024-12-18",
            "is_part_of": "eli/dl/doc/CRE-10-2024-12-18-ITM-006",
            "originalLanguage": ["http://publications.europa.eu/resource/authority/language/SPA"],
            "api:xmlFragment": {"es": XML_ES, "es-t-en-mtec": XML_EN_MT},
        }
    ],
}

PAYLOAD = {"meta": {"total": 113}, "data": [SPEECH_ROW]}


def _client(handler):
    return PinnedHttpClient(
        "data.europarl.europa.eu,www.europarl.europa.eu",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        default_headers={"Accept": "application/ld+json"},
    )


def _adapter(seen, payload=PAYLOAD, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("data.europarl.europa.eu", "www.europarl.europa.eu")
        # The Accept header is mandatory: without it the API returns RDF/XML, not JSON.
        assert "application/ld+json" in request.headers.get("accept", "")
        seen.append((request.url.path, parse_qs(request.url.query.decode())))
        if status == 204:
            return httpx.Response(204, content=b"")   # zero-byte body, as the real API sends
        return httpx.Response(status, json=payload)
    return EuroparlAdapter(_client(handler))


# --- search ----------------------------------------------------------------------

def test_search_normalizes_and_labels_machine_translation():
    seen = []
    out = _adapter(seen).search(query="Ukraine", date_start="2024-12-01", date_end="2024-12-31",
                                max_results=5, language="es")
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "eu"
    assert row["speaker"] == "Iratxe Garcia Perez"
    assert row["group"] == "S&D"
    assert row["party"] is None            # national party needs a /meps join; never guessed
    assert row["date"] == "2024-12-18"
    assert row["chamber"] == "Plenary"
    assert row["term"] == "10"             # derived from the CRE identifier
    assert row["language_original"] == "es"
    # The Spanish variant IS the member's own words.
    assert row["language_text"] == "es"
    assert row["is_translation"] is False
    assert out["total"] == 113


def test_english_variant_of_a_spanish_speech_is_flagged_as_translation():
    # This is the correctness point: serving MT English as a verbatim quote would be a factual
    # claim we cannot support.
    out = _adapter([]).search(query="Ukraine", max_results=1, language="en")
    row = out["results"][0]
    assert row["is_translation"] is True
    assert row["language_original"] == "es"
    # The MT variant key is "es-t-en-mtec": source Spanish, DELIVERED English.
    assert row["language_text"] == "en"
    assert "Madam President" in row["snippet"]   # the English text, not the Spanish original


def test_requesting_a_language_returns_that_language_s_text():
    """Regression: the BCP-47 transform tag must not be read as a prefix.

    "es-t-en-mtec" means Spanish source translated TO English, so its CONTENT is English.
    Reading the "es" prefix would return Spanish text while labelling it English.
    """
    es = _adapter([]).search(query="x", max_results=1, language="es")["results"][0]
    en = _adapter([]).search(query="x", max_results=1, language="en")["results"][0]
    assert "Senora Presidenta" in es["snippet"] and es["is_translation"] is False
    assert "Madam President" in en["snippet"] and en["is_translation"] is True


def test_http_204_empty_body_does_not_crash():
    # The real API returns 204 with a ZERO-BYTE body for empty result sets; json.loads() on that
    # throws, which would crash a naive Lambda on the very first no-results query.
    out = _adapter([], status=204).search(query="nothingmatchesthis", max_results=5)
    assert out["results"] == []
    assert out["total"] == 0
    assert "coverage starts" in out["message"]


def test_throttle_raises_instead_of_reporting_no_results():
    """A throttled request must NOT look like 'this parliament never discussed your topic'.

    The EP throttle drops the connection or returns a 200 with an empty body — no 429, no
    Retry-After. Reporting that as an empty result set is a silent wrong answer.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")     # the throttle's 200-empty shape

    adapter = EuroparlAdapter(_client(handler))
    with pytest.raises(EuThrottled):
        adapter.search(query="Ukraine")


def test_connection_error_is_treated_as_a_throttle_not_a_data_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset")

    adapter = EuroparlAdapter(_client(handler))
    with pytest.raises(EuThrottled):
        adapter.search(query="Ukraine")


def test_always_filters_to_spoken_floor_speeches():
    # Without activity-type, /speeches also returns written statements never spoken aloud.
    seen = []
    _adapter(seen).search(query="x", max_results=1)
    _, qs = seen[0]
    assert qs["activity-type"] == [ACTIVITY_TYPE]


def test_sort_is_explicit_not_relevance():
    seen = []
    _adapter(seen).search(query="x", max_results=1)
    _, qs = seen[0]
    assert qs["sort-by"] == ["sitting-date:desc"]


def test_single_date_bound_is_expanded_to_an_explicit_window():
    # `sitting-date` alone means ONE DAY in this API, not "from here onward".
    seen = []
    _adapter(seen).search(query="x", date_start="2024-12-01", max_results=1)
    _, qs = seen[0]
    assert qs["sitting-date"] == ["2024-12-01"]
    assert qs["sitting-date-end"] == ["2024-12-01"]


def test_offset_ceiling_is_reported_not_paged_into_a_404():
    out = _adapter([]).search(query="x", cursor=str(MAX_OFFSET), max_results=5)
    assert out["results"] == []
    assert "cannot page beyond" in out["message"]


def test_saturated_total_is_reported_as_truncated():
    payload = {"meta": {"total": MAX_OFFSET}, "data": [SPEECH_ROW]}
    out = _adapter([], payload=payload).search(query="x", max_results=1)
    assert out["truncated"] is True


def test_highlights_are_joined_from_the_parallel_array_by_id():
    payload = {
        "meta": {"total": 1},
        "data": [SPEECH_ROW],
        "searchResults": {
            "hits": [{"id": SPEECH_ROW["id"], "highlights": {"xml_text": "matched <inline>Ukraine</inline> here"}}]
        },
    }
    out = _adapter([], payload=payload).search(query="Ukraine", max_results=1)
    assert "Ukraine" in out["results"][0]["snippet"]


def test_source_url_is_constructed_from_the_cre_reference():
    out = _adapter([]).search(query="x", max_results=1)
    url = out["results"][0]["source_url"]
    assert url == "https://www.europarl.europa.eu/doceo/document/CRE-10-2024-12-18-ITM-006_EN.html"


def test_debate_title_comes_from_activity_label():
    out = _adapter([]).search(query="x", max_results=1)
    assert out["results"][0]["title"].startswith("Preparation of the European Council")


# --- get_text ---------------------------------------------------------------------

def test_get_text_uses_the_bare_activity_id():
    seen = []
    out = _adapter(seen).get_text(doc_id=SPEECH_ROW["activity_id"], query="Ucrania", language="es")
    path, _ = seen[0]
    assert path.endswith(f"/speeches/{SPEECH_ROW['activity_id']}")
    assert "Ucrania" in out["text"]
    assert out["is_translation"] is False


def test_get_text_requires_doc_id():
    out = _adapter([]).get_text(doc_id="")
    assert out["error"] == "bad_argument"


def test_get_text_handles_204():
    out = _adapter([], status=204).get_text(doc_id="MTG-PL-nonexistent")
    assert out["text"] == ""
    assert out["message"] == "speech not found"


# --- pure helpers -----------------------------------------------------------------

@pytest.mark.parametrize("start,end,expected", [
    ("2024-01-01", "2024-01-31", ("2024-01-01", "2024-01-31")),
    ("2024-01-01", None, ("2024-01-01", "2024-01-01")),   # single-day semantics made explicit
    (None, "2024-01-31", ("2021-07-01", "2024-01-31")),   # floored at coverage start
    (None, None, (None, None)),
])
def test_date_window(start, end, expected):
    assert _date_window(start, end) == expected


@pytest.mark.parametrize("term,expected", [("10", 10), ("9", 9), ("8", None), ("abc", None), (None, None)])
def test_term_accepts_only_9_and_10(term, expected):
    assert _term(term) == expected


def test_original_language_maps_authority_uri_to_iso2():
    assert _original_language(
        {"originalLanguage": ["http://publications.europa.eu/resource/authority/language/SPA"]}
    ) == "es"
    assert _original_language({"originalLanguage": "ENG"}) == "en"
    assert _original_language({}) is None


@pytest.mark.parametrize("frag_lang,original,expected", [
    ("es", "es", False),
    ("es-t-en-mtec", "es", True),      # -mtec marks machine translation
    ("en", "es", True),                # different delivered language
    (None, "es", False),
])
def test_is_translation(frag_lang, original, expected):
    assert _is_translation(frag_lang, original) is expected


@pytest.mark.parametrize("key,expected", [
    ("es", "es"),
    ("en", "en"),
    ("es-t-en-mtec", "en"),   # source es, DELIVERED en — not "es"
    ("de-t-fr-mtec", "fr"),
    ("pt-BR", "pt"),
])
def test_delivered_language_reads_the_transform_target(key, expected):
    assert _delivered_language(key) == expected


def test_pick_fragment_selects_by_delivered_language():
    realization = {"api:xmlFragment": {"es": "SPANISH", "es-t-en-mtec": "ENGLISH_MT"}}
    assert _pick_fragment(realization, "es") == ("SPANISH", "es")
    assert _pick_fragment(realization, "en") == ("ENGLISH_MT", "es-t-en-mtec")
    # Unknown language falls back to some variant rather than returning nothing.
    text, key = _pick_fragment(realization, "fi")
    assert text and key
    assert _pick_fragment({}, "en") == ("", None)


def test_speaker_and_group_parsed_from_xml_from_block():
    speaker, group = _speaker_and_group(XML_ES)
    assert speaker == "Iratxe Garcia Perez"
    assert group == "S&D"
    assert _speaker_and_group("") == (None, None)


def test_source_url_requires_a_cre_reference():
    assert _source_url({}) is None
    assert _source_url({"is_part_of": "eli/dl/doc/OTHER-1"}) is None
