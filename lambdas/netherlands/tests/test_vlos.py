"""Netherlands VLOS transcript parser tests.

The headline test here is the nested-interjection one: <interrumpant> lives INSIDE <woordvoerder>,
so a naive parse credits the interjector's words to the main speaker.
"""
import pytest

from gov_debates.contracts import validate
from gov_debates.ingest.store import InMemorySpeechStore

from vlos import (
    JURISDICTION,
    parse_transcript,
    pick_report_per_meeting,
)

TRANSCRIPT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<vlosCoreDocument Source="VLOS2.0">
  <vergadering>
    <datum>2026-06-04T00:00:00+02:00</datum>
    <activiteit soort="Plenair" objectid="ACT-1">
      <onderwerp>Klimaatbeleid</onderwerp>
      <activiteithoofd>
        <activiteitdeel>
          <activiteititem>
            <woordvoerder objectid="WV-1" isvoorzitter="false">
              <spreker><achternaam>De Vries</achternaam><voornaam>Anna</voornaam></spreker>
              <tekst>Voorzitter, het klimaatbeleid van dit kabinet schiet ernstig tekort.</tekst>
              <interrumpant objectid="INT-1">
                <spreker><achternaam>Jansen</achternaam><voornaam>Piet</voornaam></spreker>
                <tekst>Dat is volstrekt onjuist, de doelen worden juist wel gehaald.</tekst>
              </interrumpant>
            </woordvoerder>
          </activiteititem>
        </activiteitdeel>
      </activiteithoofd>
    </activiteit>
  </vergadering>
</vlosCoreDocument>
"""


def test_interjection_is_not_attributed_to_the_main_speaker():
    """THE critical parse test: nested <interrumpant> text must not land on the main speaker."""
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1", "Status": "Eindpublicatie"})
    by_speaker = {r.speaker: r for r in rows}
    assert set(by_speaker) == {"De Vries", "Jansen"}

    main = by_speaker["De Vries"]
    interjection = by_speaker["Jansen"]
    assert "schiet ernstig tekort" in main.full_text
    # The interjector's words must be ABSENT from the main speaker's row.
    assert "volstrekt onjuist" not in main.full_text
    assert "volstrekt onjuist" in interjection.full_text
    assert interjection.extras["kind"] == "interjection"
    assert main.extras["kind"] == "speech"


def test_rows_validate_against_the_wire_contract():
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1", "Status": "Eindpublicatie"})
    for row in rows:
        validate(row.to_speech_result().to_dict())
        assert row.jurisdiction == JURISDICTION
        assert row.date == "2026-06-04"
        assert row.title == "Klimaatbeleid"
        assert row.chamber == "Tweede Kamer"


def test_party_and_group_are_never_guessed():
    # The transcript has no usable person id; fuzzy name matching would misattribute.
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1"})
    assert all(r.group is None and r.party is None for r in rows)


def test_interim_reports_are_marked_uncorrected():
    # Filtering to final reports only would lose recent debates entirely, so we keep them labelled.
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1", "Status": "Ongecorrigeerd"})
    assert all(r.text_status == "uncorrected" for r in rows)

    final = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1", "Status": "Eindpublicatie"})
    assert all(r.text_status == "final" for r in final)


def test_xml_local_guids_are_kept_only_as_opaque_provenance():
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1"})
    row = rows[0]
    # These GUIDs 404 against OData, so they must never be used to build links.
    assert row.extras["vlos_object_id"] == "WV-1"
    assert row.source_url.startswith("https://www.tweedekamer.nl/")


def test_malformed_xml_is_not_fatal():
    assert parse_transcript(b"<broken", report={}) == []


def test_transcript_without_a_date_is_skipped():
    xml = TRANSCRIPT_XML.replace(b"<datum>2026-06-04T00:00:00+02:00</datum>", b"")
    assert parse_transcript(xml, report={}) == []


def test_date_falls_back_to_report_metadata():
    xml = TRANSCRIPT_XML.replace(b"<datum>2026-06-04T00:00:00+02:00</datum>", b"")
    rows = parse_transcript(xml, report={"Id": "R1", "Datum": "2026-06-04T00:00:00+02:00"})
    assert rows and rows[0].date == "2026-06-04"


def test_doc_id_embeds_the_date():
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1"})
    assert all(r.doc_id.startswith("nl:2026-06-04:") for r in rows)


class TestDeduplication:
    def test_picks_one_report_per_sitting_preferring_final(self):
        # A sitting can carry ~10 non-deleted reports; without dedup every speech is ingested 10x.
        reports = [
            {"Id": "1", "Vergadering_Id": "V1", "Status": "Ongecorrigeerd", "GewijzigdOp": "2026-06-04"},
            {"Id": "2", "Vergadering_Id": "V1", "Status": "Eindpublicatie", "GewijzigdOp": "2026-06-05"},
            {"Id": "3", "Vergadering_Id": "V1", "Status": "Casco", "GewijzigdOp": "2026-06-03"},
        ]
        picked = pick_report_per_meeting(reports)
        assert len(picked) == 1
        assert picked[0]["Id"] == "2"

    def test_deleted_reports_are_excluded(self):
        reports = [
            {"Id": "1", "Vergadering_Id": "V1", "Status": "Eindpublicatie", "Verwijderd": True},
            {"Id": "2", "Vergadering_Id": "V1", "Status": "Ongecorrigeerd"},
        ]
        picked = pick_report_per_meeting(reports)
        assert len(picked) == 1 and picked[0]["Id"] == "2"

    def test_distinct_sittings_are_all_kept(self):
        reports = [
            {"Id": "1", "Vergadering_Id": "V1", "Status": "Eindpublicatie"},
            {"Id": "2", "Vergadering_Id": "V2", "Status": "Eindpublicatie"},
        ]
        assert len(pick_report_per_meeting(reports)) == 2

    def test_newest_wins_within_the_same_status(self):
        reports = [
            {"Id": "old", "Vergadering_Id": "V1", "Status": "Ongecorrigeerd", "GewijzigdOp": "2026-06-01"},
            {"Id": "new", "Vergadering_Id": "V1", "Status": "Ongecorrigeerd", "GewijzigdOp": "2026-06-09"},
        ]
        assert pick_report_per_meeting(reports)[0]["Id"] == "new"

    def test_rows_without_a_meeting_id_are_dropped(self):
        assert pick_report_per_meeting([{"Status": "Eindpublicatie"}]) == []


def test_index_roundtrip():
    rows = parse_transcript(TRANSCRIPT_XML, report={"Id": "R1"})
    store = InMemorySpeechStore(rows)
    found, total = store.query(jurisdiction="nl", terms=["klimaatbeleid"], limit=5)
    assert total >= 1 and found[0].jurisdiction == "nl"
