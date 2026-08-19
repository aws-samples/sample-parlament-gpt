"""Australia (APH Hansard) — ingest parser/scraper.

Australia has no debates API. Verified constraints that shape this module:

  * The search half is **HTML scraping** of an ASP.NET WebForms page; there is no total-results
    field, so the page count must be read from the "Last page" anchor.
  * ``hto`` DEFAULT TRAP: the form ships "Hansard title only" CHECKED, so a query silently matches
    titles only. We always send ``hto=0`` to search full text.
  * ``drt`` SILENTLY IGNORES DATES: ``f``/``to`` are honoured ONLY when ``drt=1``. With ``drt=2``
    a February window returned July rows. We always send ``drt=1`` alongside any date bound.
  * Full text comes from an **undocumented** endpoint (``/api/hansard/transcript?id=...``, found in
    the site's own JS), so a canary test guards its shape.
  * **Full text is modern-only**: ``TalkText`` was null/empty for every record before ~2011, while
    search indexing reaches back to 1901. We record that honestly as an empty text with a note
    rather than pretending the speech has no words.
  * ``parlinfo.aph.gov.au`` is WAF-blocked (403 with an Azure challenge), so the historic fallback
    is unavailable from a Lambda and is not attempted.
  * Licence is **CC BY-NC-ND 3.0 AU** — the NonCommercial and NoDerivatives terms are why this
    jurisdiction ships disabled pending a human decision.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from gov_debates.ingest.documents import IndexedSpeech
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_ddmmyyyy, parse_iso

JURISDICTION = "au"
JURISDICTION_LABEL = "Parliament of Australia"

API_HOST = "www.aph.gov.au"
BASE_URL = f"https://{API_HOST}"
SEARCH_PATH = "/Parliamentary_Business/Hansard/Search"
TRANSCRIPT_PATH = "/api/hansard/transcript"

# Below this year the transcript endpoint returns no text (metadata only).
FULL_TEXT_FLOOR_YEAR = 2011
# Rows per search request (the maximum the page accepts).
MAX_PAGE_SIZE = 100

# The only place a total is expressed: the "Last page" anchor.
_LAST_PAGE_RE = re.compile(r'page=(\d+)[^"]*"\s+title="Last page"', re.IGNORECASE)
# Result rows carry a SystemId used by the transcript endpoint.
_SYSTEM_ID_RE = re.compile(r'id=([a-zA-Z0-9/_\-.]+)"', re.IGNORECASE)


def search_params(
    *,
    query: Optional[str],
    date_start: Optional[str],
    date_end: Optional[str],
    chamber: Optional[str],
    page: int = 1,
    page_size: int = MAX_PAGE_SIZE,
) -> dict[str, Any]:
    """Build the search query string, defusing the two silent-filter traps.

    ``hto=0`` searches full text rather than titles only (the form's default is titles only), and
    ``drt=1`` is required for ``f``/``to`` to be honoured at all.
    """
    params: dict[str, Any] = {
        "q": query or "",
        "hto": 0,        # search full text, NOT titles only
        "page": page,
        "ps": min(page_size, MAX_PAGE_SIZE),
    }
    if date_start or date_end:
        params["drt"] = 1   # without this, f/to are silently ignored
        if date_start:
            params["f"] = _au_date(date_start)
        if date_end:
            params["to"] = _au_date(date_end)
    chi = _chamber_index(chamber)
    if chi is not None:
        params["chi"] = chi
    return params


def _au_date(iso: str) -> str:
    """APH expects day-first dates."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso).strip())
    if not m:
        return str(iso)
    return f"{int(m.group(3))}/{int(m.group(2))}/{m.group(1)}"


def _chamber_index(chamber: Optional[str]) -> Optional[int]:
    """chi selects the chamber. 0 mixes in committee hearings, so we never send it implicitly."""
    if not chamber:
        return None
    lowered = chamber.strip().lower()
    if "rep" in lowered or "house" in lowered:
        return 1
    if "senate" in lowered:
        return 2
    return None


def last_page(html: str) -> int:
    """Read the page count from the 'Last page' anchor (no total-results field exists)."""
    if not html:
        return 1
    match = _LAST_PAGE_RE.search(html)
    if match:
        try:
            return max(1, int(match.group(1)))
        except ValueError:
            return 1
    return 1


def extract_system_ids(html: str) -> list[str]:
    """Pull the SystemIds out of a search results page, preserving order and de-duplicating."""
    seen: list[str] = []
    for match in _SYSTEM_ID_RE.finditer(html or ""):
        value = match.group(1)
        # Real ids look like "chamber/hansardr/29164/0278"; skip obvious non-ids.
        if "/" in value and value not in seen:
            seen.append(value)
    return seen


def parse_transcript(payload: dict[str, Any], system_id: str) -> Optional[IndexedSpeech]:
    """Turn a transcript API response into a row.

    Records before ~2011 come back with metadata but an EMPTY TalkText. We keep them (they are
    citable) but store no text and flag it, rather than implying the speech had no words.
    """
    if not isinstance(payload, dict):
        return None

    date = parse_iso(str(payload.get("Date") or "")) or parse_ddmmyyyy(str(payload.get("Date") or ""))
    if not date:
        return None

    talk_text = payload.get("TalkText") or ""
    text = textnorm.clean(talk_text)
    year = int(date[:4])
    text_unavailable = not text and year < FULL_TEXT_FLOOR_YEAR

    speaker = payload.get("Speaker") or None
    chamber = payload.get("Chamber") or None
    parl_no = payload.get("ParlNo")

    extras: dict[str, Any] = {"system_id": system_id}
    if payload.get("Electorate"):
        extras["electorate"] = payload["Electorate"]
    if text_unavailable:
        extras["full_text_unavailable"] = (
            "The APH transcript endpoint returns no text for records before "
            f"{FULL_TEXT_FLOOR_YEAR}; only the official PDF carries the words."
        )

    return IndexedSpeech(
        jurisdiction=JURISDICTION,
        jurisdiction_label=JURISDICTION_LABEL,
        doc_id=f"au:{date}:{system_id}",
        source_url=f"{BASE_URL}/Parliamentary_Business/Hansard/Hansard_Display?bid={system_id}",
        title=textnorm.clean(payload.get("MainTitle")) or "Hansard",
        date=date,
        speaker=textnorm.clean(speaker) or None,
        # The transcript payload carries no party field; HPS markup sometimes names an electorate
        # but not an affiliation, so party/group stay unset rather than guessed.
        group=None,
        party=None,
        role=None,
        chamber=textnorm.clean(chamber) or None,
        term=str(parl_no) if parl_no not in (None, "") else None,
        session_ref=None,
        language_original="en",
        language_text="en",
        is_translation=False,
        # Proof (uncorrected) vs Final, where the source says.
        text_status="uncorrected" if str(payload.get("Status") or "").lower() == "proof" else "final",
        extras=extras,
        full_text=text,
    )
