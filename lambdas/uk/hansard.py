"""United Kingdom (UK Parliament, Hansard) adapter.

Wraps the open Hansard API (hansard-api.parliament.uk) and normalizes results into the shared
SpeechResult contract. Verified live (docs/multi-gov/source-profiles/uk.json).

Source characteristics that shape this adapter:
  * Full verbatim text is EMBEDDED in the search response (`ContributionTextFull`), so
    `get_debate_text` is a cheap re-fetch rather than a second expensive hop.
  * `take` is hard-capped at 100 — 101+ returns HTTP 500 (not 400), so a naive client misreads
    it as an outage. We clamp.
  * The `queryParameters.` prefix is MANDATORY; omitting it silently returns UNFILTERED results
    rather than erroring. Every param below carries it.
  * Default sort is relevance (`Rank`), which is not stable across pages — we always send
    `orderBy=SittingDateDesc` so pagination cannot duplicate/skip rows.
  * There is NO party field and NO party filter anywhere in the Hansard API. Party requires a
    second call to members-api, and the naive answer is WRONG: `latestParty` is today's party,
    not the party at the time of the speech. We therefore fill `group` from the contemporaneous
    `AttributedTo` text when it carries an affiliation, and otherwise leave party/group null
    rather than asserting a possibly-anachronistic value.
  * Historic (pre-~1900) rows have null MemberName and inject <em> highlight tags into the text.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "uk"
JURISDICTION_LABEL = "UK Parliament"

API_HOST = "hansard-api.parliament.uk"
MEMBERS_HOST = "members-api.parliament.uk"
SITE_HOST = "hansard.parliament.uk"
BASE_URL = f"https://{API_HOST}"

# take > 100 returns HTTP 500 from the API, so this is a hard clamp, not a preference.
MAX_TAKE = 100

# Column-number markers and search-highlight tags embedded in ContributionTextFull.
_COLUMN_RE = re.compile(r'<span class="column-number"[^>]*>\s*</span>')


class HansardAdapter:
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
        chamber: Optional[str] = None,
        max_results: int = 5,
        cursor: Optional[str] = None,
        **_ignored: Any,
    ) -> dict[str, Any]:
        """Search spoken contributions. `cursor` is a stringified numeric offset (skip)."""
        limit = clamp_max_results(max_results, hi=MAX_TAKE)
        skip = _parse_offset(cursor)

        params: dict[str, Any] = {
            # The `queryParameters.` prefix is mandatory — without it filters are ignored.
            "queryParameters.take": min(limit, MAX_TAKE),
            "queryParameters.skip": skip,
            # Never rely on the default relevance order: it is unstable across pages.
            "queryParameters.orderBy": "SittingDateDesc",
        }
        if query:
            params["queryParameters.searchTerm"] = query
        if date_start:
            params["queryParameters.startDate"] = date_start
        if date_end:
            params["queryParameters.endDate"] = date_end
        house = _normalize_house(chamber)
        if house:
            params["queryParameters.house"] = house
        if speaker:
            member_id = self._resolve_member_id(speaker)
            if member_id is not None:
                params["queryParameters.memberId"] = member_id
            else:
                # Fall back to folding the name into the free-text query rather than silently
                # returning unfiltered results for the whole corpus.
                combined = f"{query} {speaker}".strip() if query else speaker
                params["queryParameters.searchTerm"] = combined

        payload = self._client.get_json(
            f"{BASE_URL}/search/contributions/Spoken.json", params=params
        )
        rows = payload.get("Results") or []
        total = payload.get("TotalResultCount")

        results = [self._parse_contribution(r) for r in rows[:limit]]
        next_skip = skip + len(rows)
        has_more = bool(total is not None and next_skip < total)

        return to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=total,
            cursor=str(next_skip) if has_more else None,
            truncated=has_more,
        )

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Re-fetch one contribution's verbatim text by its ContributionExtId.

        Hansard has no by-id contribution endpoint, so we search for the id. GUID case is
        inconsistent across eras (uppercase modern, lowercase historic), so match case-insensitively.
        """
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [],
                "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        payload = self._client.get_json(
            f"{BASE_URL}/search/contributions/Spoken.json",
            params={
                "queryParameters.searchTerm": doc_id,
                "queryParameters.take": MAX_TAKE,
                "queryParameters.skip": 0,
            },
        )
        wanted = doc_id.strip().lower()
        match = None
        for row in payload.get("Results") or []:
            if str(row.get("ContributionExtId", "")).strip().lower() == wanted:
                match = row
                break
        if match is None:
            return {
                "results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                "text": "", "message": "contribution not found",
            }

        full = _clean_hansard_text(match.get("ContributionTextFull") or "")
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        parsed = self._parse_contribution(match)
        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "title": parsed.title,
            "date": parsed.date,
            "source_url": parsed.source_url,
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": "en",
            "language_text": "en",
        }

    # ---- parsing ----------------------------------------------------------------

    def _parse_contribution(self, row: dict[str, Any]) -> SpeechResult:
        ext_id = str(row.get("ContributionExtId") or "")
        debate_ext_id = str(row.get("DebateSectionExtId") or "")
        sitting = parse_iso(row.get("SittingDate")) or ""
        house = row.get("House") or None
        # MemberName is null on historic rows; AttributedTo is the contemporaneous attribution.
        speaker = row.get("MemberName") or _speaker_from_attributed(row.get("AttributedTo"))
        group = _group_from_attributed(row.get("AttributedTo"))

        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            doc_id=ext_id or debate_ext_id or "unknown",
            source_url=_build_source_url(house, sitting, debate_ext_id, ext_id),
            title=textnorm.clean(row.get("DebateSection")) or "Debate contribution",
            date=sitting,
            snippet=textnorm.snippet(row.get("ContributionText")),
            speaker=speaker,
            group=group,
            party=None,   # never assert today's party for a historic speech
            role=None,
            chamber=house,
            term=None,    # Hansard exposes no term/session field; do not invent one
            session_ref=_session_ref(row),
            language_original="en",
            language_text="en",
            is_translation=False,
            text_status="final",
            extras=_extras(row),
        )

    def _resolve_member_id(self, name: str) -> Optional[int]:
        """Resolve a member name to a numeric MemberId via the Hansard member search."""
        try:
            payload = self._client.get_json(
                f"{BASE_URL}/search/members.json",
                params={"queryParameters.searchTerm": name, "queryParameters.take": 5},
            )
        except Exception:
            return None
        for row in payload.get("Results") or []:
            member_id = row.get("MemberId") or row.get("Id")
            if isinstance(member_id, int):
                return member_id
        return None


# --- pure helpers ----------------------------------------------------------------


def _parse_offset(cursor: Optional[str]) -> int:
    try:
        return max(0, int(cursor))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _normalize_house(chamber: Optional[str]) -> Optional[str]:
    """The API accepts exactly 'Commons' or 'Lords'; anything else must be dropped."""
    if not chamber:
        return None
    lowered = chamber.strip().lower()
    if "lord" in lowered:
        return "Lords"
    if "common" in lowered:
        return "Commons"
    return None


def _clean_hansard_text(raw: str) -> str:
    """Strip embedded column-number markers and highlight tags before use."""
    return textnorm.clean(_COLUMN_RE.sub(" ", raw or ""))


def _speaker_from_attributed(attributed: Any) -> Optional[str]:
    """Pull a name from AttributedTo, e.g. 'Sir Keir Starmer (Holborn, Labour)'."""
    if not isinstance(attributed, str) or not attributed.strip():
        return None
    name = re.split(r"\(", attributed, maxsplit=1)[0]
    return " ".join(name.split()) or None


# Party affiliations that appear inside AttributedTo parentheticals.
_UK_PARTIES = (
    "Labour", "Conservative", "Liberal Democrat", "Scottish National Party", "SNP",
    "Green", "Reform", "Plaid Cymru", "Sinn Fein", "Democratic Unionist", "DUP",
    "Independent", "Crossbench",
)


def _group_from_attributed(attributed: Any) -> Optional[str]:
    """Extract the CONTEMPORANEOUS party from AttributedTo, if it names one.

    Deliberately does not fall back to members-api `latestParty`: that is the member's party
    today, which mislabels any speech made before a defection or change of allegiance.
    """
    if not isinstance(attributed, str):
        return None
    for party in _UK_PARTIES:
        if re.search(rf"\b{re.escape(party)}\b", attributed, re.IGNORECASE):
            return party
    return None


def _session_ref(row: dict[str, Any]) -> Optional[str]:
    section = row.get("HansardSection") or row.get("Section")
    return textnorm.clean(section) or None if section else None


def _build_source_url(
    house: Optional[str], sitting_date: str, debate_ext_id: str, contribution_ext_id: str
) -> Optional[str]:
    """Build the public Hansard deep link. Constructed, not returned by the API."""
    if not (house and sitting_date and debate_ext_id):
        return None
    url = f"https://{SITE_HOST}/{house}/{sitting_date}/debates/{debate_ext_id}/"
    if contribution_ext_id:
        url += f"#contribution-{contribution_ext_id}"
    return url


def _extras(row: dict[str, Any]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for key in ("Section", "HansardSection", "OrderInDebateSection", "Rank"):
        if row.get(key) not in (None, ""):
            extras[key] = row[key]
    if row.get("AttributedTo"):
        extras["attributed_to"] = textnorm.clean(row["AttributedTo"])
    if row.get("MemberId"):
        extras["member_id"] = row["MemberId"]
    if row.get("DebateSectionExtId"):
        extras["debate_ext_id"] = row["DebateSectionExtId"]
    return extras
