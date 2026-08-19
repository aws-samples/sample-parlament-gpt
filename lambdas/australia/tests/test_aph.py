"""Australia (APH Hansard) scraper/parser tests.

Pins the verified traps: the hto title-only default, drt silently ignoring dates, the absent
total-results field, and the pre-2011 empty-full-text cliff.
"""
import pytest

from gov_debates.contracts import validate

from aph import (
    FULL_TEXT_FLOOR_YEAR,
    MAX_PAGE_SIZE,
    extract_system_ids,
    last_page,
    parse_transcript,
    search_params,
)

TRANSCRIPT = {
    "Date": "2026-02-11",
    "MainTitle": "Energy Policy",
    "Speaker": "Chris Bowen",
    "Chamber": "House of Representatives",
    "ParlNo": 48,
    "Electorate": "McMahon",
    "Status": "Final",
    "TalkText": "<span class='HPS-Normal'>Mr Speaker, the energy transition is under way.</span>",
}

SEARCH_HTML = """
<div class="search-results">
  <a href="/Parliamentary_Business/Hansard/Hansard_Display?id=chamber/hansardr/29164/0278">Item 1</a>
  <a href="/Parliamentary_Business/Hansard/Hansard_Display?id=chamber/hansards/29165/0012">Item 2</a>
  <a href="?page=18&amp;q=energy" title="Last page">Last</a>
</div>
"""


class TestSearchParams:
    def test_hto_is_always_zero_to_search_full_text(self):
        # The form ships "Hansard title only" CHECKED; leaving it would silently match titles only.
        params = search_params(query="energy", date_start=None, date_end=None, chamber=None)
        assert params["hto"] == 0

    def test_drt_is_sent_whenever_dates_are_used(self):
        # f/to are honoured ONLY when drt=1; with drt=2 a February window returned July rows.
        params = search_params(
            query="energy", date_start="2026-02-01", date_end="2026-02-28", chamber=None
        )
        assert params["drt"] == 1
        assert params["f"] == "1/2/2026"      # day-first
        assert params["to"] == "28/2/2026"

    def test_no_drt_when_no_dates(self):
        params = search_params(query="energy", date_start=None, date_end=None, chamber=None)
        assert "drt" not in params and "f" not in params and "to" not in params

    def test_page_size_is_capped(self):
        params = search_params(
            query="x", date_start=None, date_end=None, chamber=None, page_size=5000
        )
        assert params["ps"] == MAX_PAGE_SIZE

    @pytest.mark.parametrize("chamber,expected", [
        ("House of Representatives", 1), ("house", 1), ("Senate", 2), ("Bundestag", None),
        (None, None),
    ])
    def test_chamber_index(self, chamber, expected):
        params = search_params(query="x", date_start=None, date_end=None, chamber=chamber)
        assert params.get("chi") == expected

    def test_committee_hearings_are_not_pulled_in_implicitly(self):
        # chi=0 mixes in committee hearings, so it must never be sent by default.
        params = search_params(query="x", date_start=None, date_end=None, chamber=None)
        assert "chi" not in params


class TestHtmlScraping:
    def test_last_page_is_read_from_the_anchor(self):
        # There is no total-results field anywhere in the HTML.
        assert last_page(SEARCH_HTML) == 18

    def test_last_page_defaults_to_one(self):
        assert last_page("<div>no pagination</div>") == 1
        assert last_page("") == 1

    def test_system_ids_are_extracted_in_order_and_deduped(self):
        ids = extract_system_ids(SEARCH_HTML)
        assert ids == ["chamber/hansardr/29164/0278", "chamber/hansards/29165/0012"]
        assert extract_system_ids(SEARCH_HTML + SEARCH_HTML) == ids


class TestTranscriptParsing:
    def test_parses_a_modern_record(self):
        row = parse_transcript(TRANSCRIPT, "chamber/hansardr/29164/0278")
        assert row is not None
        validate(row.to_speech_result().to_dict())
        assert row.speaker == "Chris Bowen"
        assert row.date == "2026-02-11"
        assert row.chamber == "House of Representatives"
        assert row.term == "48"
        assert "energy transition" in row.full_text
        assert "HPS-Normal" not in row.full_text     # markup stripped
        assert row.extras["electorate"] == "McMahon"

    def test_party_is_not_guessed(self):
        row = parse_transcript(TRANSCRIPT, "id")
        assert row.group is None and row.party is None

    def test_pre_2011_record_is_kept_but_flagged_as_text_unavailable(self):
        # Search indexing reaches 1901 but TalkText is empty before ~2011. Claiming the speech has
        # no words would be wrong; we keep it citable and say why the text is missing.
        old = dict(TRANSCRIPT, Date="2005-03-15", TalkText=None)
        row = parse_transcript(old, "chamber/hansardr/1/1")
        assert row is not None
        assert row.full_text == ""
        assert "full_text_unavailable" in row.extras
        assert str(FULL_TEXT_FLOOR_YEAR) in row.extras["full_text_unavailable"]

    def test_proof_status_is_marked_uncorrected(self):
        row = parse_transcript(dict(TRANSCRIPT, Status="Proof"), "id")
        assert row.text_status == "uncorrected"
        assert parse_transcript(TRANSCRIPT, "id").text_status == "final"

    def test_day_first_dates_are_handled(self):
        row = parse_transcript(dict(TRANSCRIPT, Date="8/10/2025"), "id")
        assert row.date == "2025-10-08"

    def test_record_without_a_date_is_skipped(self):
        assert parse_transcript(dict(TRANSCRIPT, Date=None), "id") is None

    def test_non_dict_payload_is_skipped(self):
        assert parse_transcript(None, "id") is None
        assert parse_transcript("not a dict", "id") is None

    def test_doc_id_embeds_the_date_and_system_id(self):
        row = parse_transcript(TRANSCRIPT, "chamber/hansardr/29164/0278")
        assert row.doc_id == "au:2026-02-11:chamber/hansardr/29164/0278"

    def test_source_url_points_at_the_public_display_page(self):
        row = parse_transcript(TRANSCRIPT, "chamber/hansardr/29164/0278")
        assert row.source_url.startswith("https://www.aph.gov.au/Parliamentary_Business/Hansard/")
