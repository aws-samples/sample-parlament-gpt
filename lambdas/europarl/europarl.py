"""European Parliament (EP Open Data Portal) adapter.

Wraps GET /api/v2/speeches on data.europarl.europa.eu. Verified live
(docs/multi-gov/source-profiles/eu.json). This source has more silent-failure traps than any
other supported one, and each is handled explicitly below:

  * EMPTY RESULTS ARE HTTP 204 WITH A ZERO-BYTE BODY, not 200 with an empty array. Calling
    json.loads() on that throws — so we check for 204 before parsing.
  * WITHOUT AN ACCEPT HEADER YOU SILENTLY GET RDF/XML instead of JSON. We always send
    `Accept: application/ld+json` and also pass `format=` explicitly.
  * `sitting-date` ALONE MEANS A SINGLE DAY, not "from this date onward". We always send
    `sitting-date-end` too, defaulting it to `sitting-date` semantics only when the caller
    supplied one bound.
  * OFFSET IS HARD-CAPPED BELOW 10000 (offset=10000 -> HTTP 404) and `meta.total` SATURATES at
    10000. We clamp and report truncation honestly rather than paging into a 404.
  * DEFAULT SORT IS RELEVANCE, not chronological. We always set sort-by=sitting-date:desc so
    pagination is stable.
  * ALWAYS SET activity-type=PLENARY_DEBATE_SPEECH — /speeches otherwise includes written
    statements that were never spoken aloud (out of scope for a debates product).
  * 23 OF 24 LANGUAGE VARIANTS ARE MACHINE TRANSLATION (xml:lang suffix "-mtec"). Only the
    variant matching `originalLanguage` is the member's own words. We set
    language_original / language_text / is_translation accordingly so the UI can label it.
  * SPEAKER NAME IS NOT IN THE SPEECHES RESPONSE (only an opaque person ref). We parse the name
    and political group out of the embedded XML `<from>` block, which avoids a second call.
  * `activity_label` IS THE DEBATE TITLE shared across every speech in that debate, not a
    per-speech title.
  * COVERAGE FLOOR is ~July 2021; earlier windows legitimately return 204.
  * `x-total-count` is misleading (reflects page size) — use `meta.total`.
"""
from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "eu"
JURISDICTION_LABEL = "European Parliament"

API_HOST = "data.europarl.europa.eu"
SITE_HOST = "www.europarl.europa.eu"
BASE_URL = f"https://{API_HOST}/api/v2"

# offset must be strictly < 10000; meta.total also saturates at this value.
MAX_OFFSET = 10000
# Only real spoken floor contributions.
ACTIVITY_TYPE = "PLENARY_DEBATE_SPEECH"
# Earliest sitting date with data; earlier queries legitimately return HTTP 204.
COVERAGE_FLOOR = "2021-07-01"

# ISO-639-3 (EU authority) -> ISO-639-1, for the languages actually observed.
_LANG3_TO_2 = {
    "ENG": "en", "SPA": "es", "ITA": "it", "DEU": "de", "FRA": "fr", "NLD": "nl",
    "POL": "pl", "POR": "pt", "SWE": "sv", "DAN": "da", "FIN": "fi", "ELL": "el",
    "CES": "cs", "SLK": "sk", "SLV": "sl", "HUN": "hu", "RON": "ro", "BUL": "bg",
    "HRV": "hr", "EST": "et", "LAV": "lv", "LIT": "lt", "MLT": "mt", "GLE": "ga",
}

# <person refersTo="...">Name</person> and <organization refersTo="...">Group</organization>
_PERSON_RE = re.compile(r"<person[^>]*>(?P<name>[^<]+)</person>", re.IGNORECASE)
_ORG_RE = re.compile(r"<organization[^>]*>(?P<org>[^<]+)</organization>", re.IGNORECASE)
# A machine-translated variant is tagged e.g. xml:lang="es-t-en-mtec".
_MTEC_SUFFIX = "-mtec"


class EuThrottled(RuntimeError):
    """The EP API throttled us (connection dropped / empty body, no 429).

    Raised rather than returned so the Gateway surfaces a ToolExecutionError: the model must not be
    told "no results" when the truth is "we were rate-limited". Documented limit is 500 req/5min per
    endpoint, but bursts of ~10 in a few seconds were observed to drop the connection; it self-heals
    after 40-60s, so callers should back off rather than retry immediately.
    """


class EuroparlAdapter:
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
        max_results: int = 5,
        cursor: Optional[str] = None,
        language: str = "en",
        **_ignored: Any,
    ) -> dict[str, Any]:
        limit = clamp_max_results(max_results)
        offset = _parse_offset(cursor)
        if offset >= MAX_OFFSET:
            # Paging further would 404; report the ceiling honestly instead.
            return _envelope_with_note(
                "The European Parliament API cannot page beyond 10000 results for one query. "
                "Narrow the date range to reach older speeches."
            )

        params: dict[str, Any] = {
            "activity-type": ACTIVITY_TYPE,        # exclude never-spoken written statements
            "include-output": "xml_fragment",      # full text inline; MUST be comma-joined if >1
            "language": language,                 # one language, not all 24 (~11x smaller)
            "search-language": language,
            "sort-by": "sitting-date:desc",       # default is relevance, which is unstable
            "limit": limit,
            "offset": offset,
            "format": "application/ld+json",
        }
        if query:
            params["text"] = query
        if speaker:
            person_id = _person_id(speaker)
            if person_id:
                params["person-id"] = person_id
        # `sitting-date` alone means a SINGLE day, so always supply both bounds.
        start, end = _date_window(date_start, date_end)
        if start:
            params["sitting-date"] = start
        if end:
            params["sitting-date-end"] = end
        parliamentary_term = _term(term)
        if parliamentary_term:
            params["parliamentary-term"] = parliamentary_term

        payload = self._get_json_or_none(f"{BASE_URL}/speeches", params)
        if payload is None:
            # HTTP 204: a legitimate empty result set (or a pre-coverage-floor window).
            return _envelope_with_note(
                "No European Parliament speeches matched. Note that coverage starts around "
                f"{COVERAGE_FLOOR}."
            )

        rows = payload.get("data") or []
        highlights = _highlight_index(payload)
        total = _meta_total(payload)

        results = [
            self._parse_speech(row, language=language, highlights=highlights)
            for row in rows[:limit]
        ]
        next_offset = offset + len(rows)
        has_more = bool(total is not None and next_offset < min(total, MAX_OFFSET))

        return to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=total,
            cursor=str(next_offset) if has_more else None,
            truncated=has_more or (total is not None and total >= MAX_OFFSET),
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self,
        *,
        doc_id: str,
        query: Optional[str] = None,
        max_chars: int = 6000,
        language: str = "en",
    ) -> dict[str, Any]:
        """Fetch one speech's verbatim text.

        The detail route takes the BARE activity_id (e.g. 'MTG-PL-2024-12-18-OTH-...'), NOT the
        'eli/dl/event/...' prefixed `id` field.
        """
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        payload = self._get_json_or_none(
            f"{BASE_URL}/speeches/{doc_id}",
            {"include-output": "xml_fragment", "language": language,
             "format": "application/ld+json"},
        )
        if payload is None:
            return {"results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                    "text": "", "message": "speech not found"}

        rows = payload.get("data") or []
        if not rows:
            return {"results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                    "text": "", "message": "speech not found"}
        row = rows[0]
        realization = _realization(row)
        fragment, frag_lang = _pick_fragment(realization, language)
        original = _original_language(realization)
        full = textnorm.clean(fragment)
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        is_translation = _is_translation(frag_lang, original)

        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "title": _debate_title(row, language),
            "date": parse_iso(row.get("activity_date")),
            "source_url": _source_url(realization),
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": original,
            "language_text": _lang2(frag_lang) or language,
            "is_translation": is_translation,
        }

    # ---- HTTP -------------------------------------------------------------------

    def _get_json_or_none(self, url: str, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        """GET returning parsed JSON, or None on a genuine HTTP 204 empty result set.

        A 204 must be detected BEFORE json.loads(), which would otherwise raise on the empty
        body — the single most likely way to crash a naive Lambda against this API.

        Raises :class:`EuThrottled` when the response looks like the EP's connection-level
        throttle instead. This distinction is critical and easy to get wrong: the documented limit
        is 500 requests / 5 min per endpoint, but bursting ~10 requests in a few seconds was
        observed to DROP the connection with no HTTP response at all — no 429, no Retry-After,
        empty body. Treating that as "no results" would tell the user this parliament never
        discussed their topic, which is a silent wrong answer rather than an error.
        """
        try:
            resp = self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            # A reset/timeout at this host is a throttle signal, not a data error.
            raise EuThrottled(str(exc)) from exc
        if resp.status_code == 204:
            return None                      # genuine empty result set
        if not resp.content:
            # 200-with-no-body is the throttle's other shape.
            raise EuThrottled(f"empty body with status {resp.status_code}")
        return resp.json()

    # ---- parsing ----------------------------------------------------------------

    def _parse_speech(
        self, row: dict[str, Any], *, language: str, highlights: dict[str, str]
    ) -> SpeechResult:
        realization = _realization(row)
        activity_id = str(row.get("activity_id") or row.get("id") or "")
        fragment, frag_lang = _pick_fragment(realization, language)
        original = _original_language(realization)
        speaker, group = _speaker_and_group(fragment)
        if not group:
            group = _group_from_participation(row)

        snippet = highlights.get(str(row.get("id") or "")) or fragment
        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            doc_id=activity_id or "unknown",
            source_url=_source_url(realization),
            # activity_label is the DEBATE title, shared by every speech in that debate.
            title=_debate_title(row, language),
            date=parse_iso(row.get("activity_date")) or "",
            snippet=textnorm.snippet(snippet),
            speaker=speaker,
            group=group,       # EU-level political group (S&D, Renew, …)
            party=None,        # national party needs a /meps membership join; never guessed
            role=None,
            chamber="Plenary",
            term=_term_from_identifier(realization),
            session_ref=_session_ref(realization),
            language_original=original,
            language_text=_lang2(frag_lang) or language,
            is_translation=_is_translation(frag_lang, original),
            text_status="final",
            extras=_extras(row, realization),
        )


# --- pure helpers ----------------------------------------------------------------


def _parse_offset(cursor: Optional[str]) -> int:
    try:
        return max(0, int(cursor))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _envelope_with_note(message: str) -> dict[str, Any]:
    return {
        "results": [], "total": 0, "jurisdiction": JURISDICTION,
        "truncated": False, "message": message,
    }


def _date_window(start: Optional[str], end: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return an explicit (start, end) window.

    `sitting-date` on its own is interpreted by the API as a SINGLE day, so a caller supplying
    only one bound would silently get one day of results. Mirror the supplied bound to make the
    intent explicit; if only an end is given, floor the start at the coverage floor.
    """
    if start and end:
        return start, end
    if start and not end:
        return start, start
    if end and not start:
        return COVERAGE_FLOOR, end
    return None, None


def _term(term: Optional[str]) -> Optional[int]:
    """parliamentary-term accepts only 10 or 9 per the spec."""
    try:
        value = int(str(term))
    except (TypeError, ValueError):
        return None
    return value if value in (9, 10) else None


def _person_id(speaker: str) -> Optional[str]:
    """person-id takes a NUMERIC EP person id, not a name.

    We accept a bare number or a 'person/103381' style ref. A plain name cannot be used here;
    the caller's free-text query is the fallback path.
    """
    match = re.search(r"(\d{3,})", speaker or "")
    return match.group(1) if match else None


def _meta_total(payload: dict[str, Any]) -> Optional[int]:
    """Read the grand total from meta.total (NOT the misleading x-total-count header)."""
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("total"), int):
        return meta["total"]
    return None


def _highlight_index(payload: dict[str, Any]) -> dict[str, str]:
    """Highlights live in a PARALLEL array and must be joined to data[] by `id`."""
    out: dict[str, str] = {}
    search_results = payload.get("searchResults")
    if not isinstance(search_results, dict):
        return out
    for hit in search_results.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        key = str(hit.get("id") or "")
        highlights = hit.get("highlights")
        if key and isinstance(highlights, dict):
            text = highlights.get("xml_text")
            if isinstance(text, str):
                out[key] = text
    return out


def _realization(row: dict[str, Any]) -> dict[str, Any]:
    items = row.get("recorded_in_a_realization_of")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return {}


def _delivered_language(lang_key: str) -> str:
    """The language the TEXT is actually in, for an api:xmlFragment key.

    Keys are BCP-47 with a transform extension: ``"es"`` is Spanish source text, while
    ``"es-t-en-mtec"`` means "source Spanish, TRANSFORMED TO English by machine translation" —
    so its content is ENGLISH. Reading the prefix would get this exactly backwards and hand
    callers the wrong language's text while labelling it as the one they asked for.
    """
    parts = str(lang_key).lower().split("-")
    if "t" in parts:
        idx = parts.index("t")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return parts[0]


def _pick_fragment(realization: dict[str, Any], language: str) -> tuple[str, Optional[str]]:
    """Return (xml fragment, its xml:lang key) whose TEXT is in ``language``, else any variant.

    Selection is by delivered language (see :func:`_delivered_language`), so asking for "en"
    against a Spanish speech returns the English MT variant — which the caller then labels as a
    translation — rather than silently returning Spanish text tagged as English.
    """
    fragments = realization.get("api:xmlFragment")
    if not isinstance(fragments, dict) or not fragments:
        return "", None
    want = str(language).lower()
    # Exact key match first (e.g. "es" when asking for Spanish).
    if language in fragments:
        return str(fragments[language]), language
    # Then any variant whose delivered text is in the requested language.
    for key, value in fragments.items():
        if _delivered_language(key) == want:
            return str(value), str(key)
    key, value = next(iter(fragments.items()))
    return str(value), str(key)


def _original_language(realization: dict[str, Any]) -> Optional[str]:
    """The genuinely spoken language, from originalLanguage (an EU authority URI or code)."""
    raw = realization.get("originalLanguage")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, str) or not raw:
        return None
    code = raw.rstrip("/").rsplit("/", 1)[-1].upper()
    return _LANG3_TO_2.get(code, code.lower()[:2])


def _lang2(lang_key: Optional[str]) -> Optional[str]:
    """The ISO-639-1 code of the language the text is actually in."""
    if not lang_key:
        return None
    return _delivered_language(lang_key) or None


def _is_translation(fragment_lang: Optional[str], original: Optional[str]) -> bool:
    """True when the text we hold is not the member's own words.

    Two independent signals: an "-mtec" tag marks machine translation outright, and any variant
    whose DELIVERED language differs from originalLanguage is a translation regardless of tag.
    """
    if not fragment_lang:
        return False
    if _MTEC_SUFFIX in str(fragment_lang).lower():
        return True
    delivered = _lang2(fragment_lang)
    return bool(original and delivered and delivered != original)


def _speaker_and_group(fragment: str) -> tuple[Optional[str], Optional[str]]:
    """Parse the speaker name and political group from the embedded <from> block.

    This avoids a second /meps call — the speeches payload itself carries only an opaque
    person ref with no name.
    """
    if not fragment:
        return None, None
    person = _PERSON_RE.search(fragment)
    org = _ORG_RE.search(fragment)
    name = textnorm.clean(person.group("name")) if person else None
    group = textnorm.clean(org.group("org")) if org else None
    return (name or None), (group or None)


# Group slugs used by /speeches -> their conventional display names. Identifier schemes are
# inconsistent across EP endpoints (slugs here, numeric ids elsewhere), so map explicitly.
_GROUP_SLUGS = {
    "S_D": "S&D",
    "VERTS_ALE": "Greens/EFA",
    "RENEW": "Renew",
    "EPP": "EPP",
    "ECR": "ECR",
    "ID": "ID",
    "PFE": "PfE",
    "ESN": "ESN",
    "GUE_NGL": "The Left",
    "NI": "Non-attached",
}


def _group_from_participation(row: dict[str, Any]) -> Optional[str]:
    """Fall back to had_participation.participation_in_name_of (null ~76% of the time).

    It is only set when the MEP spoke explicitly on behalf of their group, so absence is normal
    and must not be treated as "no group" — we simply leave it null rather than inventing one.
    """
    participation = row.get("had_participation")
    if isinstance(participation, list) and participation:
        participation = participation[0]
    if not isinstance(participation, dict):
        return None
    raw = participation.get("participation_in_name_of")
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, str) or not raw:
        return None
    slug = raw.rsplit("/", 1)[-1]
    return _GROUP_SLUGS.get(slug, slug)


def _debate_title(row: dict[str, Any], language: str) -> str:
    label = row.get("activity_label")
    if isinstance(label, dict):
        text = label.get(language) or next(iter(label.values()), None)
        if isinstance(text, str) and text.strip():
            return textnorm.clean(text)
    if isinstance(label, str) and label.strip():
        return textnorm.clean(label)
    return "Plenary debate"


def _term_from_identifier(realization: dict[str, Any]) -> Optional[str]:
    """Term is not its own field; derive it from the CRE identifier's second segment."""
    identifier = realization.get("identifier")
    if not isinstance(identifier, str):
        return None
    parts = identifier.split("-")
    if len(parts) >= 2 and parts[1].isdigit():
        return parts[1]
    return None


def _session_ref(realization: dict[str, Any]) -> Optional[str]:
    ref = realization.get("is_part_of")
    if isinstance(ref, list) and ref:
        ref = ref[0]
    return ref if isinstance(ref, str) and ref else None


def _source_url(realization: dict[str, Any]) -> Optional[str]:
    """Construct the public verbatim-report page; the API does not return one.

    Built from is_part_of, e.g. 'eli/dl/doc/CRE-10-2024-12-18-ITM-006' ->
    .../doceo/document/CRE-10-2024-12-18-ITM-006_EN.html
    """
    ref = _session_ref(realization)
    if not ref:
        return None
    doc = ref.rsplit("/", 1)[-1]
    if not doc.startswith("CRE"):
        return None
    return f"https://{SITE_HOST}/doceo/document/{doc}_EN.html"


def _extras(row: dict[str, Any], realization: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if row.get("activity_start_date"):
        extras["activity_start_date"] = row["activity_start_date"]
    if realization.get("identifier"):
        extras["cre_identifier"] = realization["identifier"]
    if row.get("notation_speechId"):
        extras["speech_id"] = row["notation_speechId"]
    return extras
