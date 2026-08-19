"""Germany DIP adapter tests, built on the VERIFIED live response shapes (de.json).

These pin the four bug-fixes so a regression re-introduces the original defects loudly:
  * no `q` param is ever sent;
  * `f.person_id` is applied on /aktivitaet after resolving the name;
  * doc_id encodes fundstelle.id and get_text uses it (not the activity id);
  * title comes from vorgangsbezug, speaker from titel, party from /person join.
"""
import json
from urllib.parse import parse_qs

import httpx
import pytest

from gov_debates.contracts import validate
from gov_debates.http.pinned_client import PinnedHttpClient

from dip import (
    DipAdapter,
    _person_query,
    _protokoll_id_from_doc_id,
    _speaker_name,
)

# --- fixtures modelled on real verified payloads (de.json verificationNote) -------

PERSON_LOOKUP = {   # /person?f.person=Heil Hubertus
    "numFound": 1,
    "cursor": "END",
    "documents": [{"id": "1268", "nachname": "Heil", "vorname": "Hubertus"}],
}

PERSON_DETAIL = {   # /person/1268
    "id": "1268", "nachname": "Heil", "vorname": "Hubertus",
    "fraktion": ["SPD"], "funktion": ["MdB"], "wahlkreiszusatz": "Peine",
    "wahlperiode": [20, 21],
}

ACTIVITY_PAGE = {   # /aktivitaet?f.person_id=1268&f.dokumentart=Plenarprotokoll&f.zuordnung=BT
    "numFound": 332,
    "cursor": "AoJw-MS325QDMkFrdGl2aXRhZXQtMTcwODg0Mw==",
    "documents": [{
        "id": "1784775",
        "titel": "Hubertus Heil (Peine), MdB, SPD",
        "datum": "2026-06-11",
        "aktivitaetsart": "Rede",
        "wahlperiode": 21,
        "person_id": "1268",
        "dokumentart": "Plenarprotokoll",
        "vorgangsbezug": [{"titel": "Befragung der Bundesregierung"}],
        "fundstelle": {
            "dokumentnummer": "21/83", "seite": "10089D",
            "anfangsseite": 10086, "endseite": 10093,
            "pdf_url": "https://dserver.bundestag.de/btp/21/21083.pdf#P.10089",
            "xml_url": "https://dserver.bundestag.de/btp/21/21083.xml",
            "herausgeber": "BT", "id": "5798",
        },
    }],
}

PROTOCOL_TEXT = {   # /plenarprotokoll-text/5798
    "id": "5798", "titel": "Protokoll der 83. Sitzung", "datum": "2026-06-11",
    "fundstelle": {"pdf_url": "https://dserver.bundestag.de/btp/21/21083.pdf"},
    "text": "Plenarprotokoll 21/83\nDeutscher Bundestag\nStenografischer Bericht\n"
            + ("Klimaschutz ist zentral. " * 500),
}


def _routing_transport(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("search.dip.bundestag.de", "dserver.bundestag.de")
        assert request.headers.get("authorization", "").startswith("ApiKey ")
        path = request.url.path
        qs = parse_qs(request.url.query.decode())
        seen.append((path, qs))
        if path.endswith("/person"):
            return httpx.Response(200, json=PERSON_LOOKUP)
        if "/person/" in path:
            return httpx.Response(200, json=PERSON_DETAIL)
        if path.endswith("/aktivitaet"):
            return httpx.Response(200, json=ACTIVITY_PAGE)
        if "/plenarprotokoll-text/" in path:
            return httpx.Response(200, json=PROTOCOL_TEXT)
        return httpx.Response(404, json={"code": 404, "message": "ID not found"})
    return httpx.MockTransport(handler)


def _adapter(seen):
    client = PinnedHttpClient(
        "search.dip.bundestag.de,dserver.bundestag.de",
        client=httpx.Client(transport=_routing_transport(seen)),
    )
    return DipAdapter(client, api_key="test-key")


# --- search ----------------------------------------------------------------------

def test_search_by_speaker_resolves_id_and_normalizes():
    seen = []
    out = _adapter(seen).search(speaker="Hubertus Heil", date_start="2026-01-01",
                                date_end="2026-12-31", term="21", max_results=5)
    row = out["results"][0]
    validate(row)
    assert row["jurisdiction"] == "de"
    assert row["speaker"] == "Hubertus Heil"           # from titel, constituency stripped
    assert row["title"] == "Befragung der Bundesregierung"  # from vorgangsbezug, NOT titel
    assert row["group"] == "SPD"                        # from /person join
    assert row["date"] == "2026-06-11"
    assert row["term"] == "21"
    assert row["session_ref"] == "21/83, p. 10089D"
    assert row["doc_id"] == "aktivitaet:1784775@protokoll:5798"
    assert row["source_url"].startswith("https://dserver.bundestag.de")
    assert row["extras"]["aktivitaetsart"] == "Rede"
    assert out["total"] == 332


def test_search_never_sends_q_param():
    seen = []
    _adapter(seen).search(query="Klimaschutz Rede von Hubertus Heil", max_results=1)
    for path, qs in seen:
        assert "q" not in qs, f"q param leaked to {path}"


def test_search_applies_person_id_and_required_filters_on_aktivitaet():
    seen = []
    _adapter(seen).search(speaker="Hubertus Heil", max_results=1)
    akt = [qs for path, qs in seen if path.endswith("/aktivitaet")]
    assert akt, "no /aktivitaet call made"
    qs = akt[0]
    assert qs.get("f.person_id") == ["1268"]                 # bug-fix: applied HERE
    assert qs.get("f.dokumentart") == ["Plenarprotokoll"]    # floor only
    assert qs.get("f.zuordnung") == ["BT"]                    # Bundestag only


def test_person_lookup_uses_f_person_not_f_titel():
    seen = []
    _adapter(seen).search(speaker="Hubertus Heil", max_results=1)
    person = [qs for path, qs in seen if path.endswith("/person")]
    assert person and "f.person" in person[0] and "f.titel" not in person[0]


# --- get_debate_text --------------------------------------------------------------

def test_get_text_uses_protokoll_id_not_activity_id():
    seen = []
    out = _adapter(seen).get_text(doc_id="aktivitaet:1784775@protokoll:5798", query="Klimaschutz")
    text_calls = [path for path, _ in seen if "/plenarprotokoll-text/" in path]
    assert text_calls == ["/api/v1/plenarprotokoll-text/5798"]   # 5798, NOT 1784775
    assert "Klimaschutz" in out["text"]
    assert out["language_original"] == "de"
    assert out["truncated"] is True


def test_get_text_rejects_malformed_doc_id():
    out = _adapter([]).get_text(doc_id="1784775")   # bare activity id, the old bug shape
    assert out["error"] == "bad_argument"
    assert out["results"] == []


# --- pure helpers -----------------------------------------------------------------

@pytest.mark.parametrize("titel,expected", [
    ("Hubertus Heil (Peine), MdB, SPD", "Hubertus Heil"),
    ("Dr. Max Mustermann, AfD", "Dr. Max Mustermann"),
    ("Renate Künast (Berlin), MdB, BÜNDNIS 90/DIE GRÜNEN", "Renate Künast"),
])
def test_speaker_name_extraction(titel, expected):
    assert _speaker_name(titel) == expected


def test_person_query_reorders_to_nachname_vorname():
    assert _person_query("Hubertus Heil") == "Heil Hubertus"
    assert _person_query("Heil") == "Heil"


@pytest.mark.parametrize("doc_id,expected", [
    ("aktivitaet:1784775@protokoll:5798", "5798"),
    ("1784775", None),
    ("", None),
    (None, None),
])
def test_protokoll_id_extraction(doc_id, expected):
    assert _protokoll_id_from_doc_id(doc_id) == expected
