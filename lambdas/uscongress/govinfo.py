"""United States (Congressional Record) adapter — GovInfo API.

Verified live (docs/multi-gov/source-profiles/us.json). Key design consequences:

  * **api.congress.gov cannot do this.** Its /congressional-record endpoint accepts only
    format/offset/limit/y/m/d — no free-text search. GovInfo is the right source.
  * Search is **POST-only** to ``/search``, and ALL filters live inside a single Lucene-ish
    ``query`` string, not as separate fields.
  * ``collection:CREC`` MUST be in every query. Omitting it searches the CFR, US Code, bills and
    court opinions — silently ingesting statute text, which is out of scope.
  * **N+1 fan-out is unavoidable**: /search returns no speaker, no party, no congress, no chamber
    and no snippet. Each hit needs a granule-summary call. We therefore cap enrichment
    concurrency and default to small pages.
  * Three-hop pipeline: search -> granule summary (speaker/party) -> verbatim text. The text hop
    uses the **keyless** www.govinfo.gov content URL, so the API key is never exposed in a
    citation link.
  * A **granule is not a speech**: one granule is an agenda item with several speakers, and its
    text interleaves genuine speech with quoted statute text and procedural lines. We attribute
    per speaker and filter the obvious non-speech.
  * ``Extensions of Remarks`` (E pages) and ``Daily Digest`` (D pages) are NOT spoken words — we
    exclude them.
  * The .htm payload is **malformed HTML** (the closing tags precede the opening ones) wrapping
    fixed-width ASCII in a <pre>. Never hand it to a strict parser.
  * Pagination is an opaque forward-only ``offsetMark`` cursor; query/pageSize/sorts must stay
    identical across pages.
  * Coverage floor is 1994 (volume 140); earlier material is in a different collection.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "us"
JURISDICTION_LABEL = "US Congress"

API_HOST = "api.govinfo.gov"
CONTENT_HOST = "www.govinfo.gov"
API_BASE = f"https://{API_HOST}"
CONTENT_BASE = f"https://{CONTENT_HOST}"

# The Congressional Record collection. Mandatory in every query.
COLLECTION = "CREC"
# Coverage starts at volume 140 (1994); earlier material lives in CRECB (bound), not here.
COVERAGE_FLOOR = "1994-01-01"
# Page-prefix letters that are NOT spoken floor debate.
NON_SPEECH_PREFIXES = {"E", "D"}   # Extensions of Remarks, Daily Digest
# How many granule summaries to enrich per search page (N+1 cost control).
MAX_ENRICHMENTS = 10

# Speaker tokens in the fixed-width text, e.g. "Mr. BABIN." / "Ms. SALINAS." / "The SPEAKER."
_SPEAKER_TOKEN_RE = re.compile(r"\b((?:Mr|Ms|Mrs|Miss|Dr)\.\s+[A-Z][A-Z' -]{2,})\.")
# Procedural lines that are not speech.
_PROCEDURAL_RE = re.compile(
    r"^(The Clerk read|The SPEAKER pro tempore|There was no objection|"
    r"The bill was ordered|Pursuant to clause)", re.IGNORECASE
)
# The bracketed header block at the top of every .htm granule.
_HEADER_BLOCK_RE = re.compile(r"\[(?:Congressional Record|House|Senate|Pages?|From the)[^\]]*\]")
# Tags are replaced with newlines (not removed) so line structure survives for the filter below.
_TAG_RE = re.compile(r"<[^>]+>")

_PARTY_NAMES = {"R": "Republican", "D": "Democrat", "I": "Independent", "ID": "Independent"}


class GovInfoAdapter:
    def __init__(self, client: PinnedHttpClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

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
        query_string = _build_query(
            query=query, speaker=speaker, date_start=date_start,
            date_end=date_end, term=term,
        )

        body = {
            "query": query_string,
            "pageSize": limit,
            # Opaque forward-only cursor; "*" starts a new scroll.
            "offsetMark": cursor or "*",
            # Explicit ordering keeps pagination stable ('score' only allows DESC).
            "sorts": [{"field": "publishdate", "sortOrder": "DESC"}],
        }
        payload = self._client.post_json(
            f"{API_BASE}/search", json=body, headers={"X-Api-Key": self._api_key}
        )

        hits = payload.get("results") or []
        total = payload.get("count")
        next_mark = payload.get("offsetMark")

        wanted_chamber = _chamber_letter(chamber)
        results: list[SpeechResult] = []
        enriched = 0
        for hit in hits:
            granule_id = str(hit.get("granuleId") or "")
            prefix = _page_prefix(granule_id)
            # Skip Extensions of Remarks / Daily Digest — not spoken words.
            if prefix in NON_SPEECH_PREFIXES:
                continue
            if wanted_chamber and prefix != wanted_chamber:
                continue
            summary: dict[str, Any] = {}
            if enriched < MAX_ENRICHMENTS:
                summary = self._granule_summary(hit.get("packageId"), granule_id)
                enriched += 1
            results.extend(self._parse_hit(hit, summary))
            if len(results) >= limit:
                break

        return to_results_envelope(
            results[:limit],
            jurisdiction=JURISDICTION,
            total=total,
            # Only offer a cursor when the service gave us a new one and it moved.
            cursor=next_mark if next_mark and next_mark != (cursor or "*") else None,
            truncated=bool(total and len(results) < total),
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Fetch a granule's verbatim text via the KEYLESS content host.

        ``doc_id`` is the granuleId (e.g. "CREC-2026-07-20-pt1-PgH4667"); the package id is
        derivable from it, so no extra lookup is needed.
        """
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        package_id = _package_id_from_granule(doc_id)
        if not package_id:
            return {
                "error": "bad_argument",
                "message": "doc_id does not look like a Congressional Record granule id",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        # Keyless: never put the api_key in a URL a user could see.
        url = f"{CONTENT_BASE}/content/pkg/{package_id}/html/{doc_id}.htm"
        raw = self._client.get_text(url)
        full = _clean_granule_text(raw)
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "source_url": f"{CONTENT_BASE}/app/details/{package_id}/{doc_id}",
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": "en",
            "language_text": "en",
            "is_translation": False,
            "note": (
                "Congressional Record granules interleave spoken debate with quoted bill text "
                "and procedural lines; verify a quote is speech before attributing it."
            ),
        }

    # ---- enrichment -------------------------------------------------------------

    def _granule_summary(self, package_id: Any, granule_id: str) -> dict[str, Any]:
        """Second hop: the only place speaker, party, congress and chamber are available."""
        if not package_id or not granule_id:
            return {}
        try:
            return self._client.get_json(
                f"{API_BASE}/packages/{package_id}/granules/{granule_id}/summary",
                headers={"X-Api-Key": self._api_key},
            )
        except Exception:
            # Enrichment is best-effort: a rate limit must not fail the whole search.
            return {}

    # ---- parsing ----------------------------------------------------------------

    def _parse_hit(self, hit: dict[str, Any], summary: dict[str, Any]) -> list[SpeechResult]:
        """Turn one granule into one result PER SPEAKING MEMBER.

        A granule is an agenda item, not a speech, and can carry several speakers. Emitting one
        row per speaker keeps the contract's single `speaker` field honest; when the source
        attributes no speaker we emit a single row with speaker=None rather than guessing.
        """
        granule_id = str(hit.get("granuleId") or "")
        package_id = str(hit.get("packageId") or _package_id_from_granule(granule_id) or "")
        title = textnorm.clean(hit.get("title") or summary.get("title")) or "Congressional Record"
        date = parse_iso(hit.get("dateIssued") or summary.get("dateIssued")
                         or summary.get("granuleDate")) or ""
        chamber = _chamber_label(_page_prefix(granule_id), summary)
        congress = summary.get("congress")
        term = str(congress) if congress not in (None, "") else None
        source_url = summary.get("detailsLink") or (
            f"{CONTENT_BASE}/app/details/{package_id}/{granule_id}" if package_id else None
        )
        speakers = _speaking_members(summary)

        base = dict(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            doc_id=granule_id or package_id or "unknown",
            source_url=source_url,
            title=title,
            date=date,
            # /search provides no snippet or highlighting at all — a real gap. get_debate_text
            # is required to see any words.
            snippet=None,
            chamber=chamber,
            term=term,
            session_ref=_session_ref(package_id, summary),
            language_original="en",
            language_text="en",
            is_translation=False,
            text_status="final",
        )

        if not speakers:
            return [SpeechResult(**base, speaker=None, group=None, party=None, role=None,
                                 extras=_extras(hit, summary, None))]

        rows: list[SpeechResult] = []
        for member in speakers:
            party_code = member.get("party")
            rows.append(
                SpeechResult(
                    **base,
                    speaker=_member_name(member),
                    # US members belong to a party proper, not a separate parliamentary group.
                    group=None,
                    party=_PARTY_NAMES.get(str(party_code or "").upper(), party_code or None),
                    role=member.get("role") if member.get("role") != "SPEAKING" else None,
                    extras=_extras(hit, summary, member),
                )
            )
        return rows


# --- pure helpers ----------------------------------------------------------------


def _build_query(
    *,
    query: Optional[str],
    speaker: Optional[str],
    date_start: Optional[str],
    date_end: Optional[str],
    term: Optional[str],
) -> str:
    """Assemble the single Lucene-ish query string GovInfo expects.

    ``collection:CREC`` is always first and is non-negotiable: without it the search covers the
    CFR, US Code, bills and court opinions, silently pulling statute text into a debates product.
    Colon spacing is strict — no spaces around the ':'.
    """
    parts = [f"collection:{COLLECTION}"]
    if query:
        # Quote a multi-word query so it is treated as a phrase rather than loose terms.
        cleaned = query.replace('"', " ").strip()
        parts.append(f'"{cleaned}"' if " " in cleaned else cleaned)
    if speaker:
        parts.append(f"member:{_member_token(speaker)}")
    lo = _iso_or_none(date_start)
    hi = _iso_or_none(date_end)
    if lo or hi:
        parts.append(f"publishdate:range({lo or COVERAGE_FLOOR},{hi or ''})")
    congress = _congress(term)
    if congress:
        parts.append(f"congress:{congress}")
    return " ".join(parts)


def _member_token(speaker: str) -> str:
    """Prefer a bioGuideId when the caller supplies one; surnames are ambiguous across eras."""
    value = speaker.strip()
    if re.fullmatch(r"[A-Z]\d{6}", value.upper()):
        return value.upper()
    # A surname is what the docs show, but it collides across chambers/eras — use it as-is and
    # let the caller narrow with dates if needed.
    return re.sub(r"[^A-Za-z-]", "", value.split()[-1]) if value.split() else value


def _iso_or_none(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value.strip())
    return m.group(1) if m else None


def _congress(term: Optional[str]) -> Optional[int]:
    try:
        value = int(str(term))
    except (TypeError, ValueError):
        return None
    # Sanity-bound: the 1st Congress met in 1789; anything outside is a caller error.
    return value if 1 <= value <= 200 else None


def _page_prefix(granule_id: str) -> Optional[str]:
    """The page-prefix letter, which identifies the chamber/section.

    'CREC-2026-07-20-pt1-PgH4667' -> 'H' (House); '...-PgS4146-7' -> 'S' (Senate);
    'E' = Extensions of Remarks, 'D' = Daily Digest (neither is spoken debate).
    """
    if not granule_id:
        return None
    m = re.search(r"-Pg([A-Z])", granule_id)
    return m.group(1) if m else None


def _chamber_letter(chamber: Optional[str]) -> Optional[str]:
    if not chamber:
        return None
    lowered = chamber.strip().lower()
    if lowered.startswith("h"):
        return "H"
    if lowered.startswith("s"):
        return "S"
    return None


def _chamber_label(prefix: Optional[str], summary: dict[str, Any]) -> Optional[str]:
    if prefix == "H":
        return "House"
    if prefix == "S":
        return "Senate"
    granule_class = summary.get("granuleClass")
    if isinstance(granule_class, str) and granule_class:
        return granule_class.title()
    return None


def _package_id_from_granule(granule_id: str) -> Optional[str]:
    """'CREC-2026-07-20-pt1-PgH4667' -> 'CREC-2026-07-20'."""
    if not isinstance(granule_id, str):
        return None
    m = re.match(r"(CREC-\d{4}-\d{2}-\d{2})", granule_id.strip())
    return m.group(1) if m else None


def _speaking_members(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Members whose role is SPEAKING. governmentAuthor is the constant ['Congress'], not a speaker."""
    members = summary.get("members")
    if not isinstance(members, list):
        return []
    speaking = [m for m in members if isinstance(m, dict) and m.get("role") == "SPEAKING"]
    return speaking or [m for m in members if isinstance(m, dict)]


def _member_name(member: dict[str, Any]) -> Optional[str]:
    for key in ("memberName", "authorityFnf"):
        value = member.get(key)
        if isinstance(value, str) and value.strip():
            return textnorm.clean(value)
    names = member.get("name")
    if isinstance(names, list) and names:
        head = names[0]
        if isinstance(head, dict):
            for key in ("authority-fnf", "parsed", "authorityFnf"):
                value = head.get(key)
                if isinstance(value, str) and value.strip():
                    return textnorm.clean(value)
    return None


def _session_ref(package_id: str, summary: dict[str, Any]) -> Optional[str]:
    parts = [p for p in (package_id or None, summary.get("pagePrefix")) if p]
    return ", ".join(str(p) for p in parts) or None


def _clean_granule_text(raw: str) -> str:
    """Extract readable text from the malformed .htm granule payload.

    The byte stream puts the closing </pre></body></html> BEFORE the opening <html>, so it must
    not go through a strict parser. We strip tags, drop the bracketed header block, and remove
    obvious procedural lines that are not speech.
    """
    if not raw:
        return ""
    # Strip tags/entities but keep line structure: the procedural filter below is line-based, and
    # the shared `clean` collapses newlines into spaces, which would erase those boundaries.
    without_tags = textnorm.unescape_entities(_TAG_RE.sub("\n", raw))
    without_header = _HEADER_BLOCK_RE.sub(" ", without_tags)
    kept: list[str] = []
    for line in without_header.splitlines():
        stripped = textnorm.nbsp_fix(line).strip()
        if not stripped or _PROCEDURAL_RE.match(stripped):
            continue
        kept.append(stripped)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _extras(
    hit: dict[str, Any], summary: dict[str, Any], member: Optional[dict[str, Any]]
) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if hit.get("packageId"):
        extras["package_id"] = hit["packageId"]
    if summary.get("pagePrefix"):
        extras["page_prefix"] = summary["pagePrefix"]
    if summary.get("bookNumber"):
        extras["book_number"] = summary["bookNumber"]
    others = [_member_name(m) for m in _speaking_members(summary)]
    others = [n for n in others if n]
    if len(others) > 1:
        # A granule can carry several speakers; record the full cast on every row.
        extras["speakers"] = others
    if member and member.get("bioGuideId"):
        extras["bioguide_id"] = member["bioGuideId"]
    return extras
