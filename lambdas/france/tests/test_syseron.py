"""France bulk-XML ingest parser tests, plus the index-backed query path."""
import io
import zipfile

import pytest

from gov_debates.contracts import validate
from gov_debates.ingest.query_adapter import IndexBackedAdapter
from gov_debates.ingest.store import InMemorySpeechStore

from syseron import (
    JURISDICTION,
    JURISDICTION_LABEL,
    MIN_SPEECH_CHARS,
    iter_sitting_files,
    parse_sitting,
    _parse_fr_date,
)

SITTING_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<compteRendu>
  <metadonnees>
    <uid>CRSANR5L17S2026E1N123</uid>
    <dateSeance>20260611150000000</dateSeance>
    <legislature>17</legislature>
    <session>2025-2026</session>
    <numSeance>123</numSeance>
    <titreStruct>Debat sur la reforme des retraites</titreStruct>
  </metadonnees>
  <contenu>
    <point>
      <paragraphe id="P1">
        <orateurs><orateur><id>PA721964</id><nom>Elisabeth Borne</nom></orateur></orateurs>
        <texte>Monsieur le President, la reforme des retraites est une necessite pour notre pays.</texte>
      </paragraphe>
      <paragraphe id="P2">
        <orateurs><orateur><id>PA605036</id><nom>Jean Dupont</nom></orateur></orateurs>
        <texte>Je m'oppose fermement a ce texte qui penalise les travailleurs les plus modestes.</texte>
      </paragraphe>
      <paragraphe id="P3">
        <texte>Applaudissements.</texte>
      </paragraphe>
    </point>
  </contenu>
</compteRendu>
"""


def test_parses_speeches_with_speakers():
    rows = parse_sitting(SITTING_XML)
    assert len(rows) == 2          # the short "Applaudissements." fragment is dropped
    first = rows[0]
    assert first.jurisdiction == JURISDICTION
    assert first.speaker == "Elisabeth Borne"
    assert first.date == "2026-06-11"       # 17-char compact timestamp normalized
    assert first.term == "17"
    assert first.session_ref == "17/123"
    assert first.title == "Debat sur la reforme des retraites"
    assert "reforme des retraites" in first.full_text
    assert first.extras["acteur_uid"] == "PA721964"


def test_projection_validates_against_the_wire_contract():
    row = parse_sitting(SITTING_XML)[0].to_speech_result().to_dict()
    validate(row)
    assert row["jurisdiction"] == "fr"
    assert row["jurisdiction_label"] == JURISDICTION_LABEL


def test_party_is_not_guessed():
    # The bulk export carries no group; inventing one would risk the wrong party for the sitting date.
    rows = parse_sitting(SITTING_XML)
    assert all(r.group is None and r.party is None for r in rows)


def test_short_fragments_below_the_threshold_are_dropped():
    assert all(len(r.full_text) >= MIN_SPEECH_CHARS for r in parse_sitting(SITTING_XML))


def test_doc_id_embeds_the_date_for_cheap_shard_lookup():
    row = parse_sitting(SITTING_XML)[0]
    assert row.doc_id.startswith("fr:2026-06-11:")


def test_sitting_without_a_date_is_skipped():
    # Without a date we cannot shard or satisfy the contract's ISO date requirement.
    xml = b"<compteRendu><metadonnees><legislature>17</legislature></metadonnees>"\
          b"<contenu><paragraphe id='X'><texte>" + b"x" * 100 + b"</texte></paragraphe></contenu></compteRendu>"
    assert parse_sitting(xml) == []


def test_malformed_xml_returns_empty_not_an_exception():
    assert parse_sitting(b"<not valid xml") == []


def test_paragraph_without_an_id_still_gets_a_stable_doc_id():
    xml = SITTING_XML.replace(b'id="P1"', b"")
    rows = parse_sitting(xml)
    ids = [r.doc_id for r in rows]
    assert len(ids) == len(set(ids))       # deterministic and unique
    assert all(i.startswith("fr:2026-06-11:") for i in ids)


def test_iter_sitting_files_reads_the_bulk_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("sitting1.xml", SITTING_XML)
        archive.writestr("readme.txt", "ignore me")
    names = [name for name, _ in iter_sitting_files(buf.getvalue())]
    assert names == ["sitting1.xml"]


@pytest.mark.parametrize("raw,expected", [
    ("20260611150000000", "2026-06-11"),   # 17-char compact
    ("2026-06-11", "2026-06-11"),
    ("2026-06-11T15:00:00", "2026-06-11"),
    ("nonsense", None),
    (None, None),
])
def test_date_parsing_variants(raw, expected):
    assert _parse_fr_date(raw) == expected


# --- the index-backed query path ---------------------------------------------------

def _adapter(store):
    return IndexBackedAdapter(
        store, jurisdiction=JURISDICTION, jurisdiction_label=JURISDICTION_LABEL,
        coverage_note="note", ingest_hint="hint",
    )


def test_query_path_answers_from_the_index():
    store = InMemorySpeechStore(parse_sitting(SITTING_XML))
    out = _adapter(store).search(query="retraites", max_results=5)
    assert out["total"] >= 1
    assert out["results"][0]["jurisdiction"] == "fr"
    assert out["coverage_note"] == "note"


def test_empty_index_says_so_instead_of_returning_no_results():
    # An unpopulated index must never look like "this parliament never discussed your topic".
    out = _adapter(InMemorySpeechStore([])).search(query="retraites")
    assert out["error"] == "not_indexed"
    assert "hint" in out["message"]


def test_get_text_returns_the_full_speech():
    rows = parse_sitting(SITTING_XML)
    store = InMemorySpeechStore(rows)
    out = _adapter(store).get_text(doc_id=rows[0].doc_id, query="reforme")
    assert "reforme des retraites" in out["text"]
    assert out["language_original"] == "fr"


def test_get_text_missing_id():
    store = InMemorySpeechStore(parse_sitting(SITTING_XML))
    assert _adapter(store).get_text(doc_id="")["error"] == "bad_argument"
    assert _adapter(store).get_text(doc_id="fr:2026-06-11:nope")["message"] == (
        "speech not found in the index"
    )
