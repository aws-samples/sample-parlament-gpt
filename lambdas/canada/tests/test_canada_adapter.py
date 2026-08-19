"""Canada (ourcommons.ca Hansard) adapter tests, on VERIFIED shapes (ca.json).

The adapter is BUILT BUT DISABLED (robots.txt + licensing), so the first tests assert it refuses
by default. The rest exercise the parsing/pagination logic with the guard explicitly lifted, so
the code is proven correct and ready the moment the two blockers are cleared.
"""
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from ourcommons import (
    COVERAGE_FLOOR,
    PUB_TYPE_HANSARD,
    ROBOTS_DISALLOWED_PATHS,
    XML_ITEM_CAP,
    CanadaAdapter,
    _parl_ses,
    _person_id,
)

SEARCH_XML = """<?xml version="1.0" encoding="utf-8"?>
<Publication RecordsFound="239" Parliament="45" Session="1" Organization="House of Commons"
             Title="Sitting 139">
  <PublicationItem Id="14204437" EventId="322923" Date="2026-06-18" Hour="14" Minute="38">
    <OrderOfBusiness>Oral Questions</OrderOfBusiness>
    <SubjectOfBusiness>Carbon Pricing</SubjectOfBusiness>
    <Person Id="25524">
      <Honorific>Hon.</Honorific>
      <FirstName>Pierre</FirstName>
      <LastName>Poilievre</LastName>
      <Caucus Abbr="CPC">Conservative Caucus</Caucus>
    </Person>
    <XmlContent>
      <Intervention>
        <PersonSpeaking><Affiliation>Hon. Pierre Poilievre</Affiliation></PersonSpeaking>
        <Content>
          <FloorLanguage language="EN"/>
          <ParaText>Mr. Speaker, the carbon tax raises the cost of everything.</ParaText>
          <ParaText>Canadians deserve relief.</ParaText>
        </Content>
      </Intervention>
    </XmlContent>
  </PublicationItem>
</Publication>
"""

# A French-delivered speech served as English text by the /en/ endpoint.
FRENCH_XML = SEARCH_XML.replace('language="EN"', 'language="FR"')

# RecordsFound greater than the item count => the 1000-item cap silently truncated the response.
TRUNCATED_XML = SEARCH_XML.replace('RecordsFound="239"', 'RecordsFound="2131"')


def _adapter(seen, xml=SEARCH_XML, respect_robots=False):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.ourcommons.ca"
        seen.append((request.url.path, parse_qs(request.url.query.decode())))
        return httpx.Response(200, text=xml, headers={"content-type": "text/xml"})
    client = PinnedHttpClient(
        "www.ourcommons.ca", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return CanadaAdapter(client, respect_robots=respect_robots)


# --- the disabled-by-default guard -----------------------------------------------

def test_search_refuses_by_default_because_robots_disallows_the_path():
    seen = []
    out = _adapter(seen, respect_robots=True).search(query="carbon tax")
    assert out["error"] == "not_available"
    assert "robots.txt" in out["message"]
    assert out["results"] == []
    assert seen == [], "no request should be made while the guard is on"


def test_get_text_refuses_by_default_too():
    seen = []
    out = _adapter(seen, respect_robots=True).get_text(doc_id="14204437")
    assert out["error"] == "not_available"
    assert seen == []


def test_the_search_path_is_in_the_documented_disallow_list():
    # Guards against someone "fixing" the guard without noticing why it exists.
    assert any(p.lower().startswith("/publicationsearch") for p in ROBOTS_DISALLOWED_PATHS)


# --- parsing (guard explicitly lifted) --------------------------------------------

def test_search_normalizes_an_intervention():
    seen = []
    out = _adapter(seen).search(query="carbon tax", date_start="2026-06-01", date_end="2026-06-18")
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "ca"
    assert row["speaker"] == "Pierre Poilievre"
    assert row["group"] == "CPC"          # caucus, not a party proper
    assert row["party"] is None
    assert row["role"] == "Hon."
    assert row["date"] == "2026-06-18"
    assert row["title"] == "Carbon Pricing"
    assert row["chamber"] == "House of Commons"
    assert row["term"] == "45"
    assert row["session_ref"] == "45-1"
    assert row["doc_id"] == "14204437"
    assert "carbon tax raises" in row["snippet"]
    assert out["total"] == 239


def test_full_text_is_embedded_no_second_call_needed():
    seen = []
    out = _adapter(seen).search(query="carbon")
    # One request only, and the spoken words are already present.
    assert len(seen) == 1
    assert "Canadians deserve relief" in out["results"][0]["snippet"]


def test_source_url_uses_event_id_not_the_record_id():
    # @Id and @EventId are different id spaces; the anchor needs EventId.
    out = _adapter([]).search(query="x")
    url = out["results"][0]["source_url"]
    assert url == (
        "https://www.ourcommons.ca/documentviewer/en/45-1/house/sitting-139/hansard#Int-322923"
    )


def test_french_delivered_speech_is_flagged_as_translation():
    # The /en/ endpoint returns English text for speeches actually delivered in French.
    out = _adapter([], xml=FRENCH_XML).search(query="x")
    row = out["results"][0]
    assert row["language_original"] == "fr"
    assert row["language_text"] == "en"
    assert row["is_translation"] is True


def test_english_delivered_speech_is_not_flagged():
    row = _adapter([]).search(query="x")["results"][0]
    assert row["is_translation"] is False


def test_parlses_is_always_sent_and_carries_the_date_range():
    # Omitting ParlSes silently limits results to ~the last 7 sitting days.
    seen = []
    _adapter(seen).search(query="x", date_start="2026-06-01", date_end="2026-06-18")
    _, qs = seen[0]
    assert qs["ParlSes"] == ["From2026-06-01To2026-06-18"]
    assert qs["PubType"] == [PUB_TYPE_HANSARD]
    assert qs["xml"] == ["1"]


def test_page_and_rpp_are_never_sent_because_they_are_ignored():
    # Sending them would imply pagination this source does not have in xml mode.
    seen = []
    _adapter(seen).search(query="x", max_results=5)
    _, qs = seen[0]
    assert "Page" not in qs and "RPP" not in qs


def test_silent_truncation_is_detected_and_explained():
    # RecordsFound=2131 with 1 returned item means the cap dropped data — the caller must be told
    # to narrow the window rather than silently receiving partial results.
    out = _adapter([], xml=TRUNCATED_XML).search(query="x")
    assert out["truncated"] is True
    assert "Narrow date_start/date_end" in out["message"]
    assert str(XML_ITEM_CAP) in out["message"]


def test_speaker_requires_a_numeric_person_id():
    seen = []
    _adapter(seen).search(speaker="25524")
    _, qs = seen[0]
    assert qs["Per"] == ["25524"]

    seen2 = []
    _adapter(seen2).search(speaker="Pierre Poilievre")   # a name cannot be used
    _, qs2 = seen2[0]
    assert "Per" not in qs2


def test_get_text_returns_the_intervention_text():
    out = _adapter([]).get_text(doc_id="14204437", query="carbon")
    assert "carbon tax" in out["text"]
    assert out["language_original"] == "en"


def test_get_text_requires_doc_id():
    assert _adapter([]).get_text(doc_id="")["error"] == "bad_argument"


def test_get_text_handles_a_missing_intervention():
    empty = '<?xml version="1.0"?><Publication RecordsFound="0"></Publication>'
    out = _adapter([], xml=empty).get_text(doc_id="999")
    assert out["text"] == "" and out["message"] == "intervention not found"


# --- pure helpers -----------------------------------------------------------------

def test_parl_ses_packs_the_date_range_as_one_literal_string():
    assert _parl_ses("2026-06-01", "2026-06-18", None) == "From2026-06-01To2026-06-18"
    assert _parl_ses("2026-06-01", None, None).startswith("From2026-06-01To")
    assert _parl_ses(None, "2026-06-18", None) == f"From{COVERAGE_FLOOR}To2026-06-18"


def test_parl_ses_falls_back_to_a_session_or_all():
    assert _parl_ses(None, None, "45-1") == "45-1"
    assert _parl_ses(None, None, "45") == "45"
    assert _parl_ses(None, None, None) == "All"


@pytest.mark.parametrize("value,expected", [
    ("25524", "25524"), ("Per=25524", "25524"), ("Pierre Poilievre", None), ("", None), (None, None),
])
def test_person_id(value, expected):
    assert _person_id(value) == expected
