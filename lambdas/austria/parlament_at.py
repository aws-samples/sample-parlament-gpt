"""Austria (Parlament Österreich) adapter — the VTS filter API on www.parlament.gv.at.

Verified live (docs/multi-gov/source-profiles/at.json). Notable properties and traps:

  * The debate-search endpoint is **UNDOCUMENTED** (recovered from the site's own JS) and is
    **POST-only** — GET returns HTTP 405. A canary test guards against it changing shape.
  * The *documented* Open Data endpoint is a trap for this use case: it lists sittings but has no
    free-text search over speech content.
  * ``category: ["Protokolle"]`` is the CRITICAL scope filter. Omitting it silently pollutes
    results with press releases and motions (33,202 unscoped hits vs 1,210 scoped).
  * **UNKNOWN BODY KEYS ARE SILENTLY IGNORED** — a typo degrades to "return everything" rather
    than erroring. Every key we send is one that was verified to actually filter.
  * ``date_range`` has an unforgiving format: EXACTLY
    ``["YYYY-MM-DDTHH:MM:SS.mmmZ", "YYYY-MM-DDTHH:MM:SS.mmmZ"]``. Bare dates and 8 other
    variants either 500 or (worse) return unfiltered results.
  * ``datetime`` IS OFF BY ONE DAY IF READ NAIVELY: it is the Vienna sitting date converted to
    UTC, so a 15 December sitting is stamped ``2023-12-14T23:00:00.000Z``. We convert back to the
    local calendar day.
  * There is **no working speaker filter** (``name_nvg`` does not index floor speakers —
    verified count=0), and **no speaker or party field** in the response: both must be parsed out
    of the title string.
  * ``weiterfuehrender_link`` is a JSON-ENCODED STRING, not an object — it needs a second parse.
  * Coverage degrades by era behind one API: ``stteil`` (clean per-speech HTML, ~XXI GP onward),
    ``stseite`` (segmented per protocol PAGE, not per speaker), and ``PMDok`` (scanned PDF
    images, pre-1996 — unusable as text). We surface ``text_status`` accordingly.
  * All links are site-relative and must be prefixed with the host.
"""
from __future__ import annotations

import json as jsonlib
import re
from typing import Any, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import to_utc_shifted_local_day

JURISDICTION = "at"
JURISDICTION_LABEL = "Austrian Parliament"

API_HOST = "www.parlament.gv.at"
BASE_URL = f"https://{API_HOST}"
SEARCH_PATH = "/Filter/api/filterform/vts/data"
SEARCH_FBEZ = "VTS_01"

# Austria is UTC+1 (CET); the sitting date is stored as the local day converted to UTC.
LOCAL_OFFSET_HOURS = 1

# Per-speech HTML (the good case). Other item types are coarser or unusable.
ITEM_TYPE_SPEECH = "stteil"     # one speech by one speaker
ITEM_TYPE_PAGE = "stseite"      # one protocol PAGE (may span several speakers)
ITEM_TYPE_SCAN = "PMDok"        # scanned PDF image, no text layer

# Chamber values accepted by the `gremium` facet.
CHAMBERS = {"Nationalrat", "Bundesrat", "Bundesversammlung"}

# Title shape (verified): "Teil des Stenogr. Protokolls der 247. Sitzung der XXVII. GP des
# Nationalrats, 15.12.2023 / 09:05: Abgeordneter Max Mustermann (ÖVP)".
# The speaker follows the "HH:MM:" time stamp, so anchor on that rather than on any colon.
_TITLE_SPEAKER_RE = re.compile(r"\d{1,2}:\d{2}\s*:\s*(?P<rest>.+)$")
_PARTY_PAREN_RE = re.compile(r"\(([^)]+)\)\s*$")
_ROLE_PREFIX_RE = re.compile(
    r"^(Abgeordnete[r]?|Bundesrat|Bundesrätin|Bundesminister(?:in)?|Staatssekretär(?:in)?|"
    r"Präsident(?:in)?|Vizepräsident(?:in)?|Bundeskanzler(?:in)?)\s+",
    re.IGNORECASE,
)


class AustriaAdapter:
    def __init__(self, client: PinnedHttpClient) -> None:
        self._client = client

    # ---- search -----------------------------------------------------------------

    def search(
        self,
        *,
        query: Optional[str] = None,
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        term: Optional[str] = None,
        chamber: Optional[str] = None,
        max_results: int = 5,
        cursor: Optional[str] = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        limit = clamp_max_results(max_results)
        page = _parse_page(cursor)

        # There is NO working speaker filter, so a speaker request folds into the free text.
        effective_query = " ".join(p for p in (query, speaker) if p) or None
        if not effective_query:
            # This source returns nothing useful without a search term.
            return {
                "error": "bad_argument",
                "message": (
                    "The Austrian source requires a search term. Provide `query` (and optionally "
                    "`speaker`, whose name is matched as free text — there is no speaker filter)."
                ),
                "results": [], "total": 0,
            }

        body: dict[str, Any] = {
            "s.sm.query": effective_query,     # a plain string, not an array (the one exception)
            # THE critical scope filter: without it, press releases dominate the results.
            "category": ["Protokolle"],
            "searchType": ["all"],
            "searchScope": ["all"],           # search the whole document text, not just titles
        }
        window = _date_range(date_start, date_end)
        if window:
            body["date_range"] = window
        gremium = _chamber_value(chamber)
        if gremium:
            body["gremium"] = [gremium]
        gp = _term_value(term)
        if gp:
            body["gp_liste"] = [gp]

        payload = self._client.post_json(
            f"{BASE_URL}{SEARCH_PATH}",
            json=body,
            params={
                "FBEZ": SEARCH_FBEZ,
                "page": page,           # 1-based; `pageNumber` is the legacy param and 500s
                "pagesize": limit,
                "sort": "datedesc",     # omit for relevance; note `score` vanishes when sorting
            },
        )

        rows = payload.get("rows") or []
        total = payload.get("count")
        if not isinstance(total, int):
            meta = payload.get("meta") or {}
            total = meta.get("totalHits") if isinstance(meta, dict) else None

        results = [self._parse_row(r) for r in rows[:limit]]
        # `pages` is advisory (server-side result window makes it unreliable) — stop when a page
        # comes back empty rather than trusting the count.
        has_more = len(rows) >= limit
        return to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=total,
            cursor=str(page + 1) if has_more else None,
            truncated=has_more,
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Fetch a speech's full text.

        ``doc_id`` is the site-relative document link returned by search (opaque to the caller).
        Step 2 is mandatory: the search response carries only highlight fragments.
        """
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        link = doc_id if doc_id.startswith("/") else f"/{doc_id}"
        if link.lower().endswith(".pdf"):
            # Scanned-image PDFs have no machine-readable text layer.
            return {
                "results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                "text": "", "text_status": "scanned",
                "message": (
                    "This Austrian record is a scanned PDF image with no text layer; the verbatim "
                    "text cannot be extracted."
                ),
                "source_url": f"{BASE_URL}{link}",
            }

        html = self._client.get_text(f"{BASE_URL}{link}")
        full = textnorm.clean(html)
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "source_url": f"{BASE_URL}{link}",
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": "de",
            "language_text": "de",
            "is_translation": False,
        }

    # ---- parsing ----------------------------------------------------------------

    def _parse_row(self, row: dict[str, Any]) -> SpeechResult:
        title_raw = row.get("title") or ""
        speaker, group, role = _parse_title(title_raw)
        link = row.get("link") or row.get("display_link") or ""
        related = row.get("relatedLink") or ""
        item_type = row.get("item_type") or ""

        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            # The document link doubles as the id: get_debate_text needs it to fetch the text.
            doc_id=link or str(row.get("item_id_with_type") or row.get("item_id") or "unknown"),
            source_url=f"{BASE_URL}{link}" if link else None,
            title=textnorm.clean(title_raw) or "Stenographisches Protokoll",
            # datetime is the Vienna sitting day converted to UTC; naive slicing loses a day.
            date=to_utc_shifted_local_day(row.get("datetime"), local_offset_hours=LOCAL_OFFSET_HOURS) or "",
            snippet=_clean_snippet(row.get("description")),
            speaker=speaker,
            group=group,     # parliamentary club (ÖVP, SPÖ, …)
            party=None,
            role=role,
            chamber=_chamber_from_link(link or related),
            term=_term_from_link(related or link),
            session_ref=_session_ref(related),
            language_original="de",
            language_text="de",
            is_translation=False,
            # Per-page and scanned records are not clean per-speaker final text.
            text_status=_text_status(item_type),
            extras=_extras(row),
        )


# --- pure helpers ----------------------------------------------------------------


def _parse_page(cursor: Optional[str]) -> int:
    try:
        return max(1, int(cursor))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 1


def _date_range(start: Optional[str], end: Optional[str]) -> Optional[list[str]]:
    """Build the strict 2-element date_range, or None when no bounds were given.

    The format is unforgiving: only ``YYYY-MM-DDTHH:MM:SS.mmmZ`` works. Bare dates and other
    variants either 500 or silently return UNFILTERED results, which is worse.
    """
    if not start and not end:
        return None
    lo = _iso_ms(start, end_of_day=False) or "1945-01-01T00:00:00.000Z"
    hi = _iso_ms(end, end_of_day=True) or "2999-12-31T23:59:59.000Z"
    return [lo, hi]


def _iso_ms(value: Optional[str], *, end_of_day: bool) -> Optional[str]:
    """Render a YYYY-MM-DD date in the exact ISO-with-milliseconds form the API demands."""
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not m:
        return None
    day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return f"{day}T23:59:59.000Z" if end_of_day else f"{day}T00:00:00.000Z"


def _chamber_value(chamber: Optional[str]) -> Optional[str]:
    """Map a chamber hint onto the exact `gremium` facet value, ignoring anything else."""
    if not chamber:
        return None
    value = chamber.strip()
    for known in CHAMBERS:
        if value.lower() == known.lower():
            return known
    lowered = value.lower()
    if "national" in lowered:
        return "Nationalrat"
    if "bundesversammlung" in lowered:
        return "Bundesversammlung"
    if "bundesrat" in lowered:
        return "Bundesrat"
    return None


# Gesetzgebungsperiode codes are Roman numerals.
_ROMAN_RE = re.compile(r"^[IVXL]+$", re.IGNORECASE)


def _term_value(term: Optional[str]) -> Optional[str]:
    """gp_liste takes Roman-numeral term codes (e.g. "XXVII")."""
    if not term or not isinstance(term, str):
        return None
    value = term.strip().upper()
    return value if _ROMAN_RE.match(value) else None


def _parse_title(title: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (speaker, club, role) from the title string.

    There is no speaker or party field in the response, so the title is the only source. Shape:
    "Teil des Stenogr. Protokolls ... 14.12.2023 / 09:05: Abgeordneter Max Mustermann (ÖVP)".
    """
    if not title:
        return None, None, None
    cleaned = textnorm.clean(title)
    m = _TITLE_SPEAKER_RE.search(cleaned)
    if not m:
        return None, None, None
    rest = m.group("rest").strip()

    group = None
    party_match = _PARTY_PAREN_RE.search(rest)
    if party_match:
        group = party_match.group(1).strip() or None
        rest = rest[: party_match.start()].strip()

    role = None
    role_match = _ROLE_PREFIX_RE.match(rest)
    if role_match:
        role = role_match.group(1)
        rest = rest[role_match.end():].strip()

    return (rest or None), group, role


def _clean_snippet(description: Any) -> Optional[str]:
    """Strip highlight markup, entities and the literal "..." fragment joins."""
    if not description:
        return None
    text = str(description).replace("...", " … ")
    return textnorm.snippet(text)


def _chamber_from_link(link: str) -> Optional[str]:
    if not link:
        return None
    if "NRSITZ" in link:
        return "Nationalrat"
    if "BRSITZ" in link:
        return "Bundesrat"
    return None


def _term_from_link(link: str) -> Optional[str]:
    """Pull the Roman-numeral term from a link like "/XXVII/NRSITZ/247"."""
    if not link:
        return None
    for segment in link.strip("/").split("/"):
        if _ROMAN_RE.match(segment):
            return segment.upper()
    return None


def _session_ref(related: str) -> Optional[str]:
    """Render "/XXVII/NRSITZ/247" as "XXVII/247"."""
    if not related:
        return None
    parts = [p for p in related.strip("/").split("/") if p]
    if len(parts) >= 3:
        return f"{parts[0]}/{parts[2]}"
    return related or None


def _text_status(item_type: str) -> str:
    """Map the record's era/format onto the contract's text_status."""
    if item_type == ITEM_TYPE_SCAN:
        return "scanned"
    if item_type == ITEM_TYPE_PAGE:
        # Segmented per protocol page, so not a clean single-speaker record.
        return "uncorrected"
    return "final"


def _extras(row: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if row.get("item_type"):
        extras["item_type"] = row["item_type"]
    if row.get("item_id_with_type"):
        extras["item_id"] = row["item_id_with_type"]
    # weiterfuehrender_link is a JSON-ENCODED STRING, not an object — parse it a second time.
    raw = row.get("weiterfuehrender_link")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = jsonlib.loads(raw)
            if isinstance(parsed, dict) and parsed.get("url"):
                extras["further_link"] = parsed["url"]
        except (ValueError, TypeError):
            pass
    return extras
