"""Germany (Deutscher Bundestag) DIP adapter logic.

Wraps the official DIP API (search.dip.bundestag.de) and normalizes results into the shared
SpeechResult contract. This is a rewrite of the repo's original agent tools, fixing four bugs
verified live against the API (docs/multi-gov/source-profiles/de.json) rather than porting them:

  1. There is NO free-text `q` parameter on the DIP debate endpoints — it was silently ignored,
     and MAX_DIP_SCAN_PAGES=25 was a workaround for it. We drop `q` entirely and steer via the
     real filters (f.person, f.datum.*, f.wahlperiode, f.deskriptor, f.dokumentnummer).
  2. `f.person` IS valid on /aktivitaet (verified numFound=332 for Heil). The original applied
     it everywhere EXCEPT /aktivitaet (inverted condition). We resolve a speaker name to a
     person id via /person?f.person=... and filter on /aktivitaet.
  3. Full text lives at /plenarprotokoll-text/{fundstelle.id}, NOT /{aktivitaet.id} (the latter
     404s). We encode both ids in a compound doc_id.
  4. On /aktivitaet, `titel` is the SPEAKER display string, not the debate title, and party is
     absent. We take the speaker from `titel`/`person`, the debate title from
     `vorgangsbezug[0].titel`, and party from a /person/{id} join.

Also required (verified): send f.dokumentart=Plenarprotokoll and f.zuordnung=BT, or DIP mixes in
written activities and Bundesrat protocols.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import DEFAULT_PAGE_CAP, clamp_max_results, cursor_pages
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "de"
JURISDICTION_LABEL = "German Bundestag"

API_HOST = "search.dip.bundestag.de"
DOC_HOST = "dserver.bundestag.de"
BASE_URL = f"https://{API_HOST}/api/v1"

# Compound doc_id form: "aktivitaet:<activityId>@protokoll:<fundstelleId>".
_DOC_ID_RE = re.compile(r"^aktivitaet:(?P<akt>[^@]+)@protokoll:(?P<prot>.+)$")


class DipAdapter:
    """DIP search + full-text, normalized to the shared contract."""

    def __init__(self, client: PinnedHttpClient, api_key: str) -> None:
        self._client = client
        self._auth = {"Authorization": f"ApiKey {api_key}"}
        # Small per-invocation cache so multiple activities by the same speaker resolve the
        # party with a single /person call.
        self._person_cache: dict[str, dict[str, Any]] = {}

    # ---- search -----------------------------------------------------------------

    def search(
        self,
        *,
        query: Optional[str] = None,
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        term: Optional[str] = None,
        cursor: Optional[str] = None,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Search Bundestag floor contributions (/aktivitaet, restricted to Plenarprotokoll/BT).

        `query` is used only to infer a speaker when none is given (DIP has no full-text search);
        callers should prefer explicit `speaker`, date and term filters.
        """
        limit = clamp_max_results(max_results)
        person_ids = self._resolve_person_ids(speaker or _infer_speaker(query))

        params: dict[str, Any] = {
            "format": "json",
            "f.dokumentart": "Plenarprotokoll",   # floor contributions only
            "f.zuordnung": "BT",                   # Bundestag, not Bundesrat
            "f.datum.start": date_start,
            "f.datum.end": date_end,
            "f.wahlperiode": _as_int(term),
        }
        if person_ids:
            params["f.person_id"] = person_ids     # repeatable, OR semantics

        results: list[SpeechResult] = []
        num_found: Optional[int] = None
        last_cursor = cursor

        def fetch(cur: Optional[str]) -> dict[str, Any]:
            page_params = dict(params, cursor=cur)
            return self._client.get_json(
                f"{BASE_URL}/aktivitaet", params=page_params, headers=self._auth
            )

        for page in cursor_pages(
            fetch,
            extract_cursor=lambda p: p.get("cursor"),
            start_cursor=cursor,
            page_cap=DEFAULT_PAGE_CAP,
        ):
            if num_found is None:
                num_found = page.get("numFound")
            for doc in page.get("documents", []):
                results.append(self._parse_activity(doc))
                if len(results) >= limit:
                    break
            last_cursor = page.get("cursor")
            if len(results) >= limit:
                break

        return to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=num_found,
            cursor=last_cursor,
            truncated=num_found is not None and num_found > len(results),
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Fetch the verbatim Stenografischer Bericht for the protocol behind ``doc_id``.

        ``doc_id`` is the compound id from search; the protocol id (fundstelle.id) is what
        /plenarprotokoll-text expects. Passing the activity id here is the verified 404 bug.
        """
        protokoll_id = _protokoll_id_from_doc_id(doc_id)
        if not protokoll_id:
            return {
                "error": "bad_argument",
                "message": "doc_id must be a value returned by search_debates",
                "results": [],
                "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        doc = self._client.get_json(
            f"{BASE_URL}/plenarprotokoll-text/{protokoll_id}",
            params={"format": "json"},
            headers=self._auth,
        )
        full_text = doc.get("text") or ""
        fundstelle = doc.get("fundstelle") or {}
        excerpt = textnorm.snippet_around(full_text, query, max_chars=max_chars)
        return {
            "results": [],   # a text fetch is not a search; keep the sentinel shape valid
            "total": 0,
            "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "title": textnorm.clean(doc.get("titel")) or "(untitled)",
            "date": parse_iso(doc.get("datum") or fundstelle.get("datum")),
            "source_url": fundstelle.get("pdf_url") or fundstelle.get("xml_url"),
            "text": excerpt,
            "text_total_chars": len(full_text),
            "truncated": len(full_text) > len(excerpt),
            "language_original": "de",
            "language_text": "de",
        }

    # ---- parsing helpers --------------------------------------------------------

    def _parse_activity(self, doc: dict[str, Any]) -> SpeechResult:
        fundstelle = doc.get("fundstelle") or {}
        activity_id = str(doc.get("id") or "")
        protokoll_id = str(fundstelle.get("id") or "")
        person_id = str(doc.get("person_id") or "")

        # On /aktivitaet, `titel` is the speaker display string ("Hubertus Heil (Peine), MdB, SPD").
        speaker_raw = textnorm.clean(doc.get("titel"))
        speaker = _speaker_name(speaker_raw)
        # Real debate title comes from the linked procedure, not `titel`.
        title = _first_vorgang_title(doc) or "Rede im Deutschen Bundestag"

        party = None
        role = None
        if person_id:
            person = self._person(person_id)
            party = _person_party(person, doc.get("wahlperiode"))
            role = _person_role(person)

        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            doc_id=f"aktivitaet:{activity_id}@protokoll:{protokoll_id}",
            source_url=fundstelle.get("pdf_url") or fundstelle.get("xml_url"),
            title=title,
            date=parse_iso(doc.get("datum") or fundstelle.get("datum")) or "",
            snippet=None,   # /aktivitaet carries no speech text; use get_debate_text for it
            speaker=speaker,
            group=party,    # DIP `fraktion` is a parliamentary group; there is no separate party
            party=None,
            role=role,
            chamber="Bundestag",
            term=str(doc["wahlperiode"]) if doc.get("wahlperiode") is not None else None,
            session_ref=_session_ref(fundstelle),
            language_original="de",
            language_text="de",
            is_translation=False,
            text_status="final",
            extras=_activity_extras(doc, fundstelle),
        )

    def _resolve_person_ids(self, name: Optional[str]) -> list[str]:
        """Resolve a speaker name to DIP person id(s) via /person?f.person=... (verified param)."""
        if not name:
            return []
        payload = self._client.get_json(
            f"{BASE_URL}/person",
            params={"format": "json", "f.person": _person_query(name)},
            headers=self._auth,
        )
        ids = [str(p["id"]) for p in payload.get("documents", []) if p.get("id") is not None]
        return ids[:5]   # cap: a name may match a few namesakes

    def _person(self, person_id: str) -> dict[str, Any]:
        if person_id not in self._person_cache:
            self._person_cache[person_id] = self._client.get_json(
                f"{BASE_URL}/person/{person_id}", params={"format": "json"}, headers=self._auth
            )
        return self._person_cache[person_id]


# --- module-level pure helpers (unit-testable without a client) ------------------


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _protokoll_id_from_doc_id(doc_id: str) -> Optional[str]:
    if not isinstance(doc_id, str):
        return None
    m = _DOC_ID_RE.match(doc_id.strip())
    if m and m.group("prot"):
        return m.group("prot")
    return None


def _speaker_name(titel: Optional[str]) -> Optional[str]:
    """Extract a clean personal name from a DIP activity `titel`.

    Titles look like "Hubertus Heil (Peine), MdB, SPD" or "Dr. Max Mustermann, AfD"; we drop the
    constituency parenthetical and any trailing role/party tokens.
    """
    if not titel:
        return None
    name = re.split(
        r",\s*(?:MdB|SPD|CDU|CSU|AfD|FDP|BÜNDNIS|DIE LINKE|Grüne|GRÜNE|fraktionslos)",
        titel,
        maxsplit=1,
    )[0]
    name = re.sub(r"\([^)]*\)", " ", name)   # drop constituency
    return " ".join(name.split()) or None


def _person_query(name: str) -> str:
    """DIP f.person expects "Nachname Vorname" phrase order; accept either input order."""
    parts = name.replace(",", " ").split()
    if len(parts) >= 2:
        # Heuristic: assume "Vorname ... Nachname" input, reorder to "Nachname Vorname".
        return f"{parts[-1]} {' '.join(parts[:-1])}"
    return name


def _infer_speaker(query: Optional[str]) -> Optional[str]:
    """Recover a speaker from a natural query like "reden von hubertus heil 2026"."""
    if not query:
        return None
    m = re.search(r"\b(?:rede|reden|beitr[aä]ge?)\s+von\s+(.+)", query, re.IGNORECASE)
    if not m:
        return None
    cand = re.sub(r"\b(?:19|20|21)\d{2}\b", " ", m.group(1))
    cand = re.sub(r"[^A-Za-zäöüÄÖÜß\-\s]", " ", cand)
    return " ".join(cand.split()) or None


def _first_vorgang_title(doc: dict[str, Any]) -> Optional[str]:
    vb = doc.get("vorgangsbezug")
    if isinstance(vb, list) and vb:
        head = vb[0]
        if isinstance(head, dict) and head.get("titel"):
            return textnorm.clean(head["titel"])
    return None


def _session_ref(fundstelle: dict[str, Any]) -> Optional[str]:
    num = fundstelle.get("dokumentnummer")
    page = fundstelle.get("seite")
    if num and page:
        return f"{num}, p. {page}"
    return num or None


def _person_party(person: dict[str, Any], wahlperiode: Any) -> Optional[str]:
    fraktion = person.get("fraktion")
    if isinstance(fraktion, list) and fraktion:
        return str(fraktion[0])
    if isinstance(fraktion, str) and fraktion:
        return fraktion
    return None


def _person_role(person: dict[str, Any]) -> Optional[str]:
    funktion = person.get("funktion")
    if isinstance(funktion, list) and funktion:
        return str(funktion[0])
    if isinstance(funktion, str) and funktion:
        return funktion
    return None


def _activity_extras(doc: dict[str, Any], fundstelle: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if doc.get("aktivitaetsart"):
        # Only ~40% of Plenarprotokoll activities are actual "Rede"; surface the kind.
        extras["aktivitaetsart"] = doc["aktivitaetsart"]
    if fundstelle.get("id"):
        extras["protokoll_id"] = str(fundstelle["id"])
    if fundstelle.get("seite"):
        extras["page"] = fundstelle["seite"]
    return extras
