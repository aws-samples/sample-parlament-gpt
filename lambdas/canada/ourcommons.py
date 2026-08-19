"""Canada (House of Commons Hansard) adapter — ourcommons.ca PublicationSearch.

STATUS: BUILT BUT DISABLED. Two independent blockers must be cleared by a human before this is
switched on in the jurisdictions table:

  1. LICENSING. Reproduction is permitted under the Speaker's permission only if it "is accurate
     and is not presented as official", and the permission excludes reproduction "for commercial
     purpose of financial gain". The product is a non-monetised sample (a one-time donation
     button has been removed), but whether AI-condensed output is "presented as official" is an
     open question the owner must answer.
  2. robots.txt. www.ourcommons.ca explicitly Disallows /PublicationSearch/ (and /Search/,
     /Embed/, /ParlDataWidgets/) for all user-agents. Automated querying of that path is against
     the site's stated crawl policy regardless of licensing. The documented bulk-XML path
     (/Content/House/...) is NOT disallowed and is the compliant route for ingestion — see
     WHOLE_SITTING_URL below.

Verified live (docs/multi-gov/source-profiles/ca.json). Source characteristics:
  * Full verbatim text is EMBEDDED in the search response (ParaText) — search and full text in
    one request, which is the best property of this source.
  * `Page` and `RPP` are SILENTLY IGNORED in xml=1 mode: pages 1/2/3 return byte-identical
    documents (verified by md5) and every response is capped at exactly 1000 items while
    RecordsFound still reports the true total. Pagination MUST therefore be done by date-window
    slicing, asserting RecordsFound <= the item count per window.
  * The date range is packed into the single `ParlSes` value as the literal string
    "FromYYYY-MM-DDToYYYY-MM-DD" — there is no fromDate/toDate pair.
  * `ParlSes` is mandatory: omitting it silently returns only about the last seven sitting days.
  * Speaker filtering needs a numeric internal person id (`Per`), not a name.
  * The /en/ endpoint returns ENGLISH text for speeches actually delivered in French; only
    FloorLanguage reveals the original, so translations must be labelled.
  * Coverage floor is roughly September 2001, not the full historical Hansard.
  * Responses are large (~6 KB per intervention, up to ~5.5 MB) and must be stream-parsed.
"""
from __future__ import annotations

import re
# Untrusted upstream XML is parsed through defusedxml (XXE/entity-bomb hardened). The stdlib
# xml module is deliberately never imported — not even for type names, because SAST flags any
# `import xml.*` statement wholesale, regardless of whether it is used for parsing.
from defusedxml.ElementTree import fromstring as _defused_fromstring

# The Element class defusedxml's parser produces (the stdlib class, captured from a parsed
# sentinel instead of an `import xml.*` statement). Used for type annotations only.
XmlElement = type(_defused_fromstring("<sentinel/>"))
from typing import Any, Iterator, Optional

from gov_debates.contracts import SpeechResult, to_results_envelope
from gov_debates.http.pagination import clamp_max_results
from gov_debates.http.pinned_client import PinnedHttpClient
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "ca"
JURISDICTION_LABEL = "House of Commons of Canada"

API_HOST = "www.ourcommons.ca"
BASE_URL = f"https://{API_HOST}"
SEARCH_PATH = "/PublicationSearch/en/"
# The compliant bulk route (NOT disallowed by robots.txt), for a future ingest pipeline:
WHOLE_SITTING_URL = f"{BASE_URL}/Content/House/{{parliament}}{{session}}/Debates/{{sitting}}/HAN{{sitting}}-E.XML"

# House of Commons Hansard.
PUB_TYPE_HANSARD = "37"
# xml=1 responses are hard-capped at this many items regardless of Page/RPP.
XML_ITEM_CAP = 1000
# Earliest reliably available material.
COVERAGE_FLOOR = "2001-09-20"

# robots.txt Disallow list covering the search path (documented, not inferred).
ROBOTS_DISALLOWED_PATHS = ("/PublicationSearch/", "/publicationsearch/", "/Search/", "/search/")


class RobotsPolicyError(RuntimeError):
    """Raised when a request would hit a path the site's robots.txt disallows."""


class CanadaAdapter:
    """Canada Hansard adapter.

    ``respect_robots`` defaults to True, which makes ``search`` refuse rather than query the
    disallowed path. That is deliberate: the adapter is complete and unit-tested, but it will not
    silently crawl a path the site asks crawlers to stay out of. Flipping it is a human decision
    that belongs with the licensing sign-off.
    """

    def __init__(self, client: PinnedHttpClient, *, respect_robots: bool = True) -> None:
        self._client = client
        self._respect_robots = respect_robots

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
        **_ignored: Any,
    ) -> dict[str, Any]:
        if self._respect_robots:
            return {
                "error": "not_available",
                "message": (
                    "The Canadian Hansard search path is disallowed by ourcommons.ca robots.txt, "
                    "and reuse is subject to the Speaker's permission (which excludes commercial "
                    "use and requires the material not be presented as official). This source is "
                    "built but disabled pending a licensing decision."
                ),
                "results": [], "total": 0, "jurisdiction": JURISDICTION,
            }

        limit = clamp_max_results(max_results)
        # ParlSes is mandatory AND carries the date range as one packed literal string.
        parl_ses = _parl_ses(date_start, date_end, term)
        params: dict[str, Any] = {
            "PubType": PUB_TYPE_HANSARD,
            "ParlSes": parl_ses,
            "xml": "1",
            # Page/RPP are deliberately NOT sent: they are silently ignored in xml mode and
            # sending them would imply pagination that does not exist.
        }
        if query:
            params["Text"] = query
        if speaker:
            person_id = _person_id(speaker)
            if person_id:
                params["Per"] = person_id

        xml_text = self._client.get_text(f"{BASE_URL}{SEARCH_PATH}", params=params)
        root = _defused_fromstring(xml_text)
        records_found = _int_attr(root, "RecordsFound")
        items = list(_iter_items(root))

        # Silent truncation guard: the response caps at 1000 items while still reporting the true
        # total, so a mismatch means we are dropping data and must narrow the window.
        truncated = bool(records_found is not None and records_found > len(items))

        results = [self._parse_item(it, root) for it in items[:limit]]
        envelope = to_results_envelope(
            results,
            jurisdiction=JURISDICTION,
            total=records_found,
            truncated=truncated,
        )
        if truncated:
            envelope["message"] = (
                f"This window matched {records_found} interventions but the source returns at most "
                f"{XML_ITEM_CAP}. Narrow date_start/date_end (a single sitting day is ~240 "
                "interventions) to see the rest — page numbers are ignored by this source."
            )
        return envelope

    # ---- full text --------------------------------------------------------------

    def get_text(
        self, *, doc_id: str, query: Optional[str] = None, max_chars: int = 6000
    ) -> dict[str, Any]:
        """Fetch a single intervention by its PublicationItem @Id."""
        if self._respect_robots:
            return {
                "error": "not_available",
                "message": (
                    "The Canadian Hansard search path is disallowed by ourcommons.ca robots.txt; "
                    "this source is built but disabled pending a licensing decision."
                ),
                "results": [], "total": 0, "jurisdiction": JURISDICTION,
            }
        if not doc_id:
            return {
                "error": "bad_argument",
                "message": "doc_id is required (use a value returned by search_debates)",
                "results": [], "total": 0,
            }
        max_chars = textnorm.clamp_max_chars(max_chars)
        xml_text = self._client.get_text(
            f"{BASE_URL}{SEARCH_PATH}",
            params={"PubType": PUB_TYPE_HANSARD, "Item": doc_id, "xml": "1"},
        )
        root = _defused_fromstring(xml_text)
        items = list(_iter_items(root))
        if not items:
            return {"results": [], "total": 0, "jurisdiction": JURISDICTION, "doc_id": doc_id,
                    "text": "", "message": "intervention not found"}
        item = items[0]
        full = _intervention_text(item)
        excerpt = textnorm.snippet_around(full, query, max_chars=max_chars)
        floor = _floor_language(item)
        return {
            "results": [], "total": 0, "jurisdiction": JURISDICTION,
            "doc_id": doc_id,
            "title": _title(item) or "Intervention",
            "date": parse_iso(item.get("Date")),
            "source_url": _source_url(item, root),
            "text": excerpt,
            "text_total_chars": len(full),
            "truncated": len(full) > len(excerpt),
            "language_original": floor,
            "language_text": "en",
            # The /en/ endpoint returns English text even for speeches delivered in French.
            "is_translation": bool(floor and floor != "en"),
        }

    # ---- parsing ----------------------------------------------------------------

    def _parse_item(self, item: XmlElement, root: XmlElement) -> SpeechResult:
        floor = _floor_language(item)
        person = item.find("Person")
        caucus = person.find("Caucus") if person is not None else None
        return SpeechResult(
            jurisdiction=JURISDICTION,
            jurisdiction_label=JURISDICTION_LABEL,
            doc_id=item.get("Id") or item.get("EventId") or "unknown",
            source_url=_source_url(item, root),
            title=_title(item) or "Intervention",
            date=parse_iso(item.get("Date")) or "",
            snippet=textnorm.snippet(_intervention_text(item)),
            speaker=_person_name(person),
            # Caucus is a caucus, not a party proper.
            group=(caucus.get("Abbr") if caucus is not None else None) or None,
            party=None,
            role=(person.findtext("Honorific") if person is not None else None) or None,
            chamber=root.get("Organization") or "House of Commons",
            term=_term(root),
            session_ref=_session_ref(root),
            language_original=floor,
            language_text="en",
            is_translation=bool(floor and floor != "en"),
            text_status="final",
            extras=_extras(item),
        )


# --- pure helpers ----------------------------------------------------------------


def _parl_ses(start: Optional[str], end: Optional[str], term: Optional[str]) -> str:
    """Build the mandatory ParlSes value.

    It doubles as the date-range carrier: the range is the literal string
    "FromYYYY-MM-DDToYYYY-MM-DD". When a parliament-session is given instead (e.g. "45-1") that
    is used verbatim. Omitting ParlSes entirely silently limits results to the last ~7 sitting
    days, so we always emit something.
    """
    lo = _iso(start)
    hi = _iso(end)
    if lo or hi:
        return f"From{lo or COVERAGE_FLOOR}To{hi or '2999-12-31'}"
    if term and re.fullmatch(r"\d{2}-\d", str(term).strip()):
        return str(term).strip()
    if term and str(term).strip().isdigit():
        return str(term).strip()
    return "All"


def _iso(value: Optional[str]) -> Optional[str]:
    if not value or not isinstance(value, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", value.strip())
    return m.group(1) if m else None


def _person_id(speaker: str) -> Optional[str]:
    """`Per` takes a numeric internal person id, not a name."""
    m = re.search(r"(\d{3,})", str(speaker or ""))
    return m.group(1) if m else None


def _iter_items(root: XmlElement) -> Iterator[XmlElement]:
    for item in root.iter("PublicationItem"):
        yield item


def _int_attr(element: XmlElement, name: str) -> Optional[int]:
    raw = element.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _title(item: XmlElement) -> Optional[str]:
    """There is no per-speech title; SubjectOfBusiness is the best synthetic one."""
    for tag in ("SubjectOfBusiness", "OrderOfBusiness"):
        value = item.findtext(tag)
        if value and value.strip():
            return textnorm.clean(value)
    return None


def _person_name(person: Optional[XmlElement]) -> Optional[str]:
    if person is None:
        return None
    first = (person.findtext("FirstName") or "").strip()
    last = (person.findtext("LastName") or "").strip()
    name = f"{first} {last}".strip()
    return name or None


def _intervention_text(item: XmlElement) -> str:
    """Concatenate the ParaText nodes — the actual spoken words, embedded in the search response."""
    parts: list[str] = []
    for para in item.iter("ParaText"):
        # itertext() picks up nested inline markup as well as the direct text.
        text = " ".join(t for t in para.itertext() if t and t.strip())
        if text.strip():
            parts.append(text.strip())
    return textnorm.clean(" ".join(parts))


def _floor_language(item: XmlElement) -> Optional[str]:
    """The ORIGINAL floor language, the only signal that English text is a translation."""
    for node in item.iter("FloorLanguage"):
        raw = node.get("language")
        if raw:
            return str(raw).lower()[:2]
    return None


def _term(root: XmlElement) -> Optional[str]:
    parliament = root.get("Parliament")
    return str(parliament) if parliament else None


def _session_ref(root: XmlElement) -> Optional[str]:
    parliament = root.get("Parliament")
    session = root.get("Session")
    if parliament and session:
        return f"{parliament}-{session}"
    return parliament or None


def _source_url(item: XmlElement, root: XmlElement) -> Optional[str]:
    """Construct the citizen-facing documentviewer deep link.

    Note @EventId (not @Id) is the anchor; they are different id spaces on the same record.
    """
    parliament = root.get("Parliament")
    session = root.get("Session")
    sitting = _sitting_number(root)
    event_id = item.get("EventId")
    if not (parliament and session and sitting):
        return None
    url = f"{BASE_URL}/documentviewer/en/{parliament}-{session}/house/sitting-{sitting}/hansard"
    return f"{url}#Int-{event_id}" if event_id else url


def _sitting_number(root: XmlElement) -> Optional[str]:
    title = root.get("Title") or ""
    m = re.search(r"(\d+)", title)
    return m.group(1) if m else None


def _extras(item: XmlElement) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if item.get("EventId"):
        extras["event_id"] = item.get("EventId")
    person = item.find("Person")
    if person is not None and person.get("Id"):
        extras["person_id"] = person.get("Id")
    caucus = person.find("Caucus") if person is not None else None
    if caucus is not None and (caucus.text or "").strip():
        extras["caucus"] = caucus.text.strip()
    order = item.findtext("OrderOfBusiness")
    if order and order.strip():
        extras["order_of_business"] = order.strip()
    return extras
