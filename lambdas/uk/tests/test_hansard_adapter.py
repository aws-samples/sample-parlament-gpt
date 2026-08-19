"""UK Hansard adapter tests, built on the VERIFIED live response shapes (uk.json)."""
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from hansard import (
    MAX_TAKE,
    HansardAdapter,
    _build_source_url,
    _clean_hansard_text,
    _group_from_attributed,
    _normalize_house,
)

# Modelled on the real verified record (Richard Burgon, 2024-12-19, Commons).
CONTRIB = {
    "ContributionExtId": "6D749D0F-FD88-47D4-9E44-1B0C0F5EF9A1",
    "DebateSectionExtId": "A1B2C3D4-0000-1111-2222-333344445555",
    "DebateSection": "Flood Protection",
    "SittingDate": "2024-12-19T00:00:00",
    "House": "Commons",
    "Section": "Commons Chamber",
    "HansardSection": "vol 759 c430",
    "MemberName": "Richard Burgon",
    "MemberId": 4493,
    "AttributedTo": "Richard Burgon (Leeds East, Labour)",
    "ContributionText": " Flooding has devastated communities in my constituency...",
    "ContributionTextFull": (
        '<span class="column-number" data-column-number="430"></span>'
        "Flooding has devastated communities in my constituency. "
        "<em>Climate</em> resilience funding must rise."
    ),
}

SEARCH_PAYLOAD = {"TotalResultCount": 334, "Results": [CONTRIB]}
MEMBER_PAYLOAD = {"Results": [{"MemberId": 4493, "Name": "Richard Burgon"}]}


def _transport(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("hansard-api.parliament.uk", "members-api.parliament.uk")
        path = request.url.path
        seen.append((path, parse_qs(request.url.query.decode())))
        if path.endswith("/search/members.json"):
            return httpx.Response(200, json=MEMBER_PAYLOAD)
        if path.endswith("/search/contributions/Spoken.json"):
            return httpx.Response(200, json=SEARCH_PAYLOAD)
        return httpx.Response(404, json={"Message": "No HTTP resource was found"})
    return httpx.MockTransport(handler)


def _adapter(seen):
    client = PinnedHttpClient(
        "hansard-api.parliament.uk,members-api.parliament.uk",
        client=httpx.Client(transport=_transport(seen)),
    )
    return HansardAdapter(client)


def test_search_normalizes_a_contribution():
    seen = []
    out = _adapter(seen).search(query="flood", chamber="Commons", max_results=5)
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "uk"
    assert row["speaker"] == "Richard Burgon"
    assert row["title"] == "Flood Protection"
    assert row["date"] == "2024-12-19"          # ISO datetime -> date
    assert row["chamber"] == "Commons"
    assert row["group"] == "Labour"              # contemporaneous, from AttributedTo
    assert row["party"] is None                  # never asserted from latestParty
    assert row["doc_id"] == CONTRIB["ContributionExtId"]
    assert row["source_url"].startswith("https://hansard.parliament.uk/Commons/2024-12-19/")
    assert "#contribution-" in row["source_url"]
    assert out["total"] == 334


def test_every_param_carries_the_mandatory_queryparameters_prefix():
    # Omitting the prefix silently returns UNFILTERED results rather than erroring.
    seen = []
    _adapter(seen).search(query="flood", date_start="2024-01-01", date_end="2024-12-31",
                          chamber="Commons", max_results=5)
    _, qs = [(p, q) for p, q in seen if p.endswith("Spoken.json")][0]
    assert qs, "no query params sent"
    for key in qs:
        assert key.startswith("queryParameters."), f"param missing prefix: {key}"


def test_take_is_clamped_to_the_api_hard_limit():
    # take > 100 returns HTTP 500 from the real API, so this clamp prevents a fake "outage".
    seen = []
    _adapter(seen).search(query="x", max_results=500)
    _, qs = [(p, q) for p, q in seen if p.endswith("Spoken.json")][0]
    assert int(qs["queryParameters.take"][0]) <= MAX_TAKE


def test_ordering_is_always_explicit_for_stable_pagination():
    seen = []
    _adapter(seen).search(query="x", max_results=5)
    _, qs = [(p, q) for p, q in seen if p.endswith("Spoken.json")][0]
    assert qs["queryParameters.orderBy"] == ["SittingDateDesc"]


def test_speaker_is_resolved_to_a_member_id():
    seen = []
    _adapter(seen).search(speaker="Richard Burgon", max_results=5)
    paths = [p for p, _ in seen]
    assert any(p.endswith("/search/members.json") for p in paths)
    _, qs = [(p, q) for p, q in seen if p.endswith("Spoken.json")][0]
    assert qs["queryParameters.memberId"] == ["4493"]


def test_unresolvable_speaker_folds_into_search_term_not_unfiltered():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append((path, parse_qs(request.url.query.decode())))
        if path.endswith("/search/members.json"):
            return httpx.Response(200, json={"Results": []})   # no match
        return httpx.Response(200, json=SEARCH_PAYLOAD)

    client = PinnedHttpClient(
        "hansard-api.parliament.uk,members-api.parliament.uk",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    HansardAdapter(client).search(speaker="Nobody McNobody", max_results=5)
    _, qs = [(p, q) for p, q in seen if p.endswith("Spoken.json")][0]
    assert "queryParameters.memberId" not in qs
    assert "Nobody McNobody" in qs["queryParameters.searchTerm"][0]


def test_get_text_strips_column_markers_and_highlights():
    out = _adapter([]).get_text(doc_id=CONTRIB["ContributionExtId"], query="climate")
    assert "column-number" not in out["text"]
    assert "<em>" not in out["text"]
    assert "Climate" in out["text"]
    assert out["language_original"] == "en"


def test_get_text_matches_guid_case_insensitively():
    # GUID case is inconsistent across eras (uppercase modern, lowercase historic).
    out = _adapter([]).get_text(doc_id=CONTRIB["ContributionExtId"].lower())
    assert out["text"]


def test_get_text_requires_doc_id():
    out = _adapter([]).get_text(doc_id="")
    assert out["error"] == "bad_argument"


def test_historic_row_without_member_name_still_validates():
    historic = dict(CONTRIB, MemberName=None, AttributedTo="Mr. Gladstone", MemberId=None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"TotalResultCount": 1, "Results": [historic]})

    client = PinnedHttpClient(
        "hansard-api.parliament.uk", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    out = HansardAdapter(client).search(query="reform", max_results=1)
    row = out["results"][0]
    validate(row)
    assert row["speaker"] == "Mr. Gladstone"
    assert row["group"] is None


# --- pure helpers -----------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("Commons", "Commons"), ("commons", "Commons"),
    ("House of Lords", "Lords"), ("lords", "Lords"),
    ("Bundestag", None), (None, None), ("", None),
])
def test_normalize_house(value, expected):
    assert _normalize_house(value) == expected


@pytest.mark.parametrize("attributed,expected", [
    ("Richard Burgon (Leeds East, Labour)", "Labour"),
    ("Some Peer (Conservative)", "Conservative"),
    ("A Member (Scottish National Party)", "Scottish National Party"),
    ("Mr. Gladstone", None),
    (None, None),
])
def test_group_from_attributed(attributed, expected):
    assert _group_from_attributed(attributed) == expected


def test_clean_hansard_text_removes_markup():
    raw = '<span class="column-number" data-column-number="1"></span>Hello <em>world</em>'
    assert _clean_hansard_text(raw) == "Hello world"


def test_build_source_url_requires_all_parts():
    assert _build_source_url(None, "2024-01-01", "abc", "def") is None
    assert _build_source_url("Commons", "", "abc", "def") is None
    url = _build_source_url("Commons", "2024-01-01", "abc", "def")
    assert url == "https://hansard.parliament.uk/Commons/2024-01-01/debates/abc/#contribution-def"
