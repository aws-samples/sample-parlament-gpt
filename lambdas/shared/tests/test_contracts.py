"""Tests for the normalized result contract and envelope builder."""
import pytest

from gov_debates.contracts import (
    SchemaViolation,
    SpeechResult,
    to_results_envelope,
    validate,
)


def _valid_row(**overrides):
    base = dict(
        jurisdiction="de",
        jurisdiction_label="German Bundestag",
        doc_id="aktivitaet:1784775@protokoll:5798",
        source_url="https://dserver.bundestag.de/btp/21/21083.pdf#P.10089",
        title="Befragung des Bundesministers",
        date="2026-06-11",
    )
    base.update(overrides)
    return SpeechResult(**base)


def test_speechresult_roundtrips_to_dict():
    row = _valid_row(speaker="Hubertus Heil", group="SPD").to_dict()
    assert row["speaker"] == "Hubertus Heil"
    assert row["group"] == "SPD"
    assert row["party"] is None            # nullable, not guessed from group
    assert row["is_translation"] is False
    assert row["text_status"] == "final"
    assert row["extras"] == {}


def test_group_and_party_are_independent():
    # The central schema decision: never copy group into party.
    row = _valid_row(group="Fraktion der SVP").to_dict()
    assert row["group"] == "Fraktion der SVP"
    assert row["party"] is None


def test_envelope_has_load_bearing_results_key():
    env = to_results_envelope([_valid_row()], jurisdiction="de")
    assert "results" in env and isinstance(env["results"], list)
    assert env["total"] == 1
    assert env["jurisdiction"] == "de"
    assert env["truncated"] is False


def test_envelope_total_defaults_to_len_but_honors_grand_total():
    env = to_results_envelope([_valid_row()], jurisdiction="de", total=250)
    assert env["total"] == 250 and len(env["results"]) == 1


def test_envelope_includes_cursor_only_when_given():
    assert "cursor" not in to_results_envelope([], jurisdiction="de")
    assert to_results_envelope([], jurisdiction="de", cursor="X")["cursor"] == "X"


@pytest.mark.parametrize("bad", [
    {"jurisdiction": "", "jurisdiction_label": "x", "doc_id": "d", "title": "t", "date": "2020-01-01"},
    {"jurisdiction": "de", "jurisdiction_label": "x", "doc_id": "", "title": "t", "date": "2020-01-01"},
    {"jurisdiction": "de", "jurisdiction_label": "x", "doc_id": "d", "title": "", "date": "2020-01-01"},
])
def test_validate_rejects_missing_required(bad):
    with pytest.raises(SchemaViolation):
        validate(bad)


@pytest.mark.parametrize("bad_date", ["2026-6-11", "11.06.2026", "2026-06-11T00:00:00", "not-a-date", "2026-13-01"])
def test_validate_rejects_unnormalized_date(bad_date):
    with pytest.raises(SchemaViolation):
        validate({"jurisdiction": "de", "jurisdiction_label": "x", "doc_id": "d", "title": "t", "date": bad_date})


def test_validate_rejects_bad_text_status_and_extras():
    good = {"jurisdiction": "de", "jurisdiction_label": "x", "doc_id": "d", "title": "t", "date": "2020-01-01"}
    with pytest.raises(SchemaViolation):
        validate({**good, "text_status": "provisional"})
    with pytest.raises(SchemaViolation):
        validate({**good, "extras": ["not", "a", "map"]})


def test_envelope_validates_each_row_by_default():
    bad = SpeechResult(jurisdiction="de", jurisdiction_label="x", doc_id="d", source_url=None, title="t", date="bad")
    with pytest.raises(SchemaViolation):
        to_results_envelope([bad], jurisdiction="de")
