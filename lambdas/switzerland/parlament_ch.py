"""Switzerland (Swiss Federal Assembly) adapter — ws.parlament.ch OData v2.

Verified live (docs/multi-gov/source-profiles/ch.json). This source is powerful (full verbatim
text embedded, back to 1999) but has more silent-failure traps than any other. Each is handled:

  * OData **v2 only**: `contains()` returns HTTP 400. Free text uses
    ``substringof('needle', Field)`` — note v2's argument order is (needle, haystack), the
    REVERSE of v4's contains(haystack, needle).
  * ``Language`` MUST be pinned in every query. The entity key is composite (ID, Language), so
    omitting it yields 5x duplicate rows for every speech. Critically, Language is NOT a
    translation axis — all five values return byte-identical ``Text`` (verified by md5). It only
    localizes label fields, and Language='EN' returns FRENCH labels.
  * The spoken language of the text is ``LanguageOfText``, NOT ``Language``. Confusing the two
    mislabels every French or Italian speech as German.
  * ``Type`` must be filtered to 1. Type=2 is vote-tally text and Type=3 is agenda headings;
    both carry a null speaker and would otherwise pollute results.
  * LATENCY IS THE LAMBDA KILLER: an unbounded ``substringof(term, Text)`` measured 43–85 s
    cold. We therefore ALWAYS bound the query with a MeetingDate window (defaulting to a recent
    window when the caller gives none) and require a generous socket timeout.
  * ``MeetingDate`` is an Edm.String in YYYYMMDD form — filtered with quotes and compared
    lexicographically, not as a date.
  * Int64 keys serialize as JSON **strings** ("350903"), and IdSession is a string on Transcript
    but an int on Session. We never assume numbers.
  * There is NO title field on Transcript; the debate title requires a SubjectBusiness join.
  * ``SpeakerFullName`` is SURNAME-FIRST ("Rösti Albert"), so exact-eq on "Firstname Lastname"
    always returns zero rows.
  * ``Text`` is a <pd_text>/<p> envelope with typesetting control tokens ([GZ], [NB], [PAGE n])
    that must be scrubbed.
  * ``ParlGroupAbbreviation`` is the parliamentary GROUP (Fraktion), not the party, and is null
    for Federal Councillors — so it populates `group`, never `party`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_yyyymmdd

JURISDICTION = "ch"
JURISDICTION_LABEL = "Swiss Parliament"

API_HOST = "ws.parlament.ch"
SITE_HOST = "www.parlament.ch"
BASE_URL = f"https://{API_HOST}/odata.svc"

# The server caps any page at 1000 rows regardless of $top.
SERVER_PAGE_CAP = 1000
# Only Type=1 rows are actual spoken contributions.
SPOKEN_TYPE = 1
# Language must be pinned; DE is the canonical axis (Text is identical across all of them).
LANGUAGE_AXIS = "DE"
# Chamber codes: N = Nationalrat (lower), S = Ständerat (upper).
CHAMBERS = {"N", "S"}
# Fields we need; $select is mandatory to keep payloads sane (Text is MAX-length).
_SELECT = ",".join([
    "ID", "Language", "LanguageOfText", "Text", "SpeakerFullName", "SpeakerFunction",
    "ParlGroupAbbreviation", "ParlGroupName", "CantonAbbreviation", "CouncilName",
    "MeetingCouncilAbbreviation", "MeetingDate", "IdSession", "IdSubject", "PersonNumber", "Type",
])
# Same, minus Text, for cheap listing.
_SELECT_NO_TEXT = ",".join(f for f in _SELECT.split(",") if f != "Text")

# Typesetting control tokens embedded in Text.
_CONTROL_TOKEN_RE = re.compile(r"\[(?:GZ|NB|NAM|VS|PAGE\s+\d+)\]")


class SwissAdapter:
    def __init__(self, client: PinnedHttpClient) -> None:
        self._client = client
        # Debate titles need a SubjectBusiness join; cache per invocation so a page of speeches
        # from the same debate costs one extra request, not one per row.
        self._title_cache: dict[str, Optional[str]] = {}

    # ---- search -----------------------------------------------------------------

    def search(
        self,
        *,
        query: Optional[str] = None,
        speaker: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        chamber: Optional[str] = None,
        max_results: int = 5,
        cursor: Optional[str] = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        limit = min(clamp_max_results(max_results), SERVER_PAGE_CAP)

        # A cursor is the absolute __next URL the service handed us; follow it verbatim (never
        # hand-build a $skiptoken — its shape is an internal key tuple).
        if cursor:
            payload = self._client.get_json(cursor)
        else:
            filters = self._build_filters(
                query=query, speaker=speaker, date_start=date_start,
                date_end=date_end, chamber=chamber,
            )
            params = {
                "$filter": " and ".join(filters),
                "$select": _SELECT,
                "$orderby": "MeetingDate desc",
                "$top": limit,
                # Force the object response form so __next has somewhere to live.
                "$inlinecount": "allpages",
                "$format": "json",
            }
            payload = self._client.get_json(f"{BASE_URL}/Transcript", params=params)

        rows, total, next_url = _unwrap(payload)
        results = [self._parse_transcript(r) for r in rows[:limit]]

        return to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=total,
            cursor=next_url,
            truncated=bool(next_url),
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Fetch one speech's verbatim text by its compound doc_id ("<ID>@<Language>")."""
        transcript_id, language = _split_doc_id(doc_id)
        if not transcript_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        # The entity key is composite; both parts are required and Int64 keys take an L suffix.
        key = f"(ID={transcript_id}L,Language='{language}')"
        payload = self._client.get_json(
            f"{BASE_URL}/Transcript{key}", params={"$select": _SELECT, "$format": "json"}
        )
        row = payload.get("d") or {}
        if isinstance(row, dict) and "results" in row:
            rows = row.get("results") or []
            row = rows[0] if rows else {}
        if not row:
            return {"results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                    "text": "", "message": "transcript not found"}

        full = _clean_speech_text(row.get("Text"))
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        spoken = _spoken_language(row)
        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "title": self._debate_title(row.get("IdSubject")) or "Debate contribution",
            "date": parse_yyyymmdd(row.get("MeetingDate")),
            "source_url": _source_url(row.get("IdSubject")),
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": spoken,
            "language_text": spoken,
            "is_translation": False,   # Text is always the language actually spoken
        }

    # ---- filters ----------------------------------------------------------------

    def _build_filters(
        self,
        *,
        query: Optional[str],
        speaker: Optional[str],
        date_start: Optional[str],
        date_end: Optional[str],
        chamber: Optional[str],
    ) -> list[str]:
        # Language pin and Type filter are non-negotiable: without them we get 5x duplicates and
        # vote/agenda rows masquerading as speeches.
        filters = [
            f"Language eq '{LANGUAGE_AXIS}'",
            f"Type eq {SPOKEN_TYPE}",
        ]

        # ALWAYS bound the date range. An unbounded substringof over Text measured 43-85 s and
        # would blow any sane Lambda timeout.
        start, end = _date_window(date_start, date_end)
        filters.append(f"MeetingDate ge '{start}'")
        filters.append(f"MeetingDate le '{end}'")

        if query:
            # OData v2: substringof(needle, haystack) — argument order is the reverse of v4.
            filters.append(f"substringof('{_escape(query)}',Text)")
        if speaker:
            filters.append(_speaker_filter(speaker))
        code = _chamber_code(chamber)
        if code:
            filters.append(f"MeetingCouncilAbbreviation eq '{code}'")
        return filters

    # ---- joins ------------------------------------------------------------------

    def _debate_title(self, id_subject: Any) -> Optional[str]:
        """Resolve the debate title via SubjectBusiness (Transcript has no title field)."""
        if not id_subject:
            return None
        key = str(id_subject)
        if key in self._title_cache:
            return self._title_cache[key]
        title: Optional[str] = None
        try:
            payload = self._client.get_json(
                f"{BASE_URL}/SubjectBusiness",
                params={
                    "$filter": f"IdSubject eq {key}L",
                    "$select": "IdSubject,TitleDE,TitleFR,TitleIT",
                    "$top": 1,
                    "$format": "json",
                },
            )
            rows, _, _ = _unwrap(payload)
            if rows:
                row = rows[0]
                # TitleIT can be null; fall back across the localized variants.
                for field in ("TitleDE", "TitleFR", "TitleIT"):
                    value = row.get(field)
                    if isinstance(value, str) and value.strip():
                        title = textnorm.clean(value)
                        break
        except Exception:
            title = None
        self._title_cache[key] = title
        return title

    # ---- parsing ----------------------------------------------------------------

    def _parse_transcript(self, row: dict[str, Any]) -> SpeechResult:
        transcript_id = str(row.get("ID") or "")
        language = str(row.get("Language") or LANGUAGE_AXIS)
        spoken = _spoken_language(row)
        text = _clean_speech_text(row.get("Text"))

        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            # Composite key: ID alone is not unique across the Language axis.
            doc_id=f"{transcript_id}@{language}",
            source_url=_source_url(row.get("IdSubject")),
            title=self._debate_title(row.get("IdSubject")) or "Debate contribution",
            date=parse_yyyymmdd(row.get("MeetingDate")) or "",
            snippet=textnorm.snippet(text) if text else None,
            speaker=_speaker_name(row.get("SpeakerFullName")),
            # Fraktion, not party — and null for Federal Councillors.
            group=row.get("ParlGroupAbbreviation") or None,
            party=None,
            role=row.get("SpeakerFunction") or None,
            chamber=_chamber_label(row.get("MeetingCouncilAbbreviation")),
            term=None,   # requires a Session join; not asserted here
            session_ref=str(row.get("IdSession")) if row.get("IdSession") else None,
            language_original=spoken,
            language_text=spoken,
            is_translation=False,
            text_status="final",
            extras=_extras(row),
        )


# --- pure helpers ----------------------------------------------------------------


def _escape(value: str) -> str:
    """Escape a string literal for an OData filter (single quotes are doubled)."""
    return str(value).replace("'", "''")


# The corpus begins with the winter session of 1999 (start of the 46th legislature).
CORPUS_START = "19991206"
CORPUS_END = "29991231"


def _date_window(start: Optional[str], end: Optional[str]) -> tuple[str, str]:
    """Return an inclusive (YYYYMMDD, YYYYMMDD) window, always explicitly bounded.

    Both bounds are ALWAYS emitted into the filter. An unbounded ``substringof(term, Text)`` on
    this service measured 43-85 s cold, and a bounded range lets the server use the MeetingDate
    index. Missing bounds fall back to the corpus edges, which still produces a range predicate
    rather than none at all.
    """
    return (_compact(start) or CORPUS_START), (_compact(end) or CORPUS_END)


def _compact(iso_date: Optional[str]) -> Optional[str]:
    """Convert YYYY-MM-DD to the YYYYMMDD string form MeetingDate uses."""
    if not iso_date or not isinstance(iso_date, str):
        return None
    digits = re.sub(r"\D", "", iso_date)
    return digits[:8] if len(digits) >= 8 else None


def _speaker_filter(speaker: str) -> str:
    """Build a speaker filter tolerant of name order.

    SpeakerFullName is SURNAME-FIRST ("Rösti Albert"), so an exact eq on "Albert Rösti" returns
    nothing. substringof on each token is order-independent and still uses the index.
    """
    tokens = [t for t in re.split(r"[\s,]+", speaker.strip()) if t]
    if not tokens:
        return "true"
    clauses = [f"substringof('{_escape(t)}',SpeakerFullName)" for t in tokens[:3]]
    return "(" + " and ".join(clauses) + ")"


def _chamber_code(chamber: Optional[str]) -> Optional[str]:
    """Map a chamber hint to the 'N'/'S' council code, ignoring anything unrecognised."""
    if not chamber:
        return None
    value = chamber.strip()
    if value.upper() in CHAMBERS:
        return value.upper()
    lowered = value.lower()
    if "national" in lowered or "nationalrat" in lowered:
        return "N"
    if "stände" in lowered or "stande" in lowered or "council of states" in lowered:
        return "S"
    return None


def _chamber_label(code: Any) -> Optional[str]:
    return {"N": "Nationalrat", "S": "Ständerat"}.get(str(code or "").upper()) or None


def _spoken_language(row: dict[str, Any]) -> Optional[str]:
    """The language actually spoken, from LanguageOfText (NOT the Language key axis)."""
    raw = row.get("LanguageOfText") or None
    return str(raw).lower() if raw else None


def _clean_speech_text(raw: Any) -> str:
    """Strip the <pd_text>/<p> envelope and typesetting control tokens."""
    if not raw:
        return ""
    return textnorm.clean(_CONTROL_TOKEN_RE.sub(" ", str(raw)))


def _speaker_name(full_name: Any) -> Optional[str]:
    """Normalize "Surname Firstname" to "Firstname Surname" for display."""
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    parts = full_name.split()
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return " ".join(parts)


def _split_doc_id(doc_id: Any) -> tuple[Optional[str], str]:
    """Split "<ID>@<Language>" into its parts, defaulting the language axis."""
    if not isinstance(doc_id, str) or not doc_id.strip():
        return None, LANGUAGE_AXIS
    if "@" in doc_id:
        ident, lang = doc_id.split("@", 1)
        ident = re.sub(r"\D", "", ident)
        return (ident or None), (lang or LANGUAGE_AXIS)
    ident = re.sub(r"\D", "", doc_id)
    return (ident or None), LANGUAGE_AXIS


def _unwrap(payload: Any) -> tuple[list[dict[str, Any]], Optional[int], Optional[str]]:
    """Unwrap an OData v2 JSON response into (rows, total, next_url).

    The shape is INCONSISTENT: normally {"d": {"results": [...], "__count": "n", "__next": url}},
    but with $format=json and no $inlinecount it can degrade to {"d": [...]} — which has nowhere
    to carry __next. We handle both.
    """
    if not isinstance(payload, dict):
        return [], None, None
    d = payload.get("d")
    if isinstance(d, list):
        return [r for r in d if isinstance(r, dict)], None, None
    if not isinstance(d, dict):
        return [], None, None
    rows = [r for r in (d.get("results") or []) if isinstance(r, dict)]
    total = None
    raw_count = d.get("__count")
    if isinstance(raw_count, str) and raw_count.isdigit():
        total = int(raw_count)
    elif isinstance(raw_count, int):
        total = raw_count
    next_url = d.get("__next") if isinstance(d.get("__next"), str) else None
    return rows, total, next_url


def _source_url(id_subject: Any) -> Optional[str]:
    """Citation link to the official bulletin page for the debate."""
    if not id_subject:
        return None
    return (
        f"https://{SITE_HOST}/de/ratsbetrieb/amtliches-bulletin/"
        f"amtliches-bulletin-die-verhandlungen?SubjectId={id_subject}"
    )


def _extras(row: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for key in ("ParlGroupName", "CantonAbbreviation", "CouncilName", "PersonNumber", "IdSubject"):
        value = row.get(key)
        if value not in (None, ""):
            extras[key] = value
    return extras
