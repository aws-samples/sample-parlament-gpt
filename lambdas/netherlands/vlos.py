"""Netherlands (Tweede Kamer) — VLOS transcript ingest parser.

The Dutch OData portal cannot search debates: ``$search`` is accepted with HTTP 200 and **silently
ignored** (verified — identical $count for a real term, a nonsense term, and no term at all), and
no field anywhere holds speech text. Transcripts live behind a separate endpoint as whole-meeting
XML blobs (0.9–3 MB each):

    GET /OData/v4/2.0/Verslag/{Id}/resource

So the Netherlands needs a bulk ingest into our own index. Verified structural traps handled here:

  * **Interjections are NESTED, not siblings**: ``<interrumpant>`` sits INSIDE ``<woordvoerder>``.
    A naive "iterate woordvoerder, take tekst" parse attributes the interjector's words to the
    main speaker. We extract each speaker's own text only, and emit interjections as their own rows.
  * **Massive duplication**: one sitting can have ~10 non-deleted ``Verslag`` rows. Dedup is
    mandatory, keyed on the meeting id, preferring the most authoritative status.
  * Only 1458 of 23263 reports are ``Eindpublicatie`` (final). Filtering to those loses recent
    debates entirely, so we ingest interim ones too and mark them ``uncorrected``.
  * **Every GUID inside the XML is XML-local and 404s against OData**, so they are recorded as
    opaque extras and never used to construct API links.
  * Votes (``stemmingen``) and procedural narration (``draadboekfragment``) are out of scope and
    filtered out.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# Untrusted upstream XML is parsed through defusedxml (XXE/entity-bomb hardened). The stdlib
# xml module is deliberately never imported — not even for type names, because SAST flags any
# `import xml.*` statement wholesale, regardless of whether it is used for parsing.
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError as XmlParseError
from defusedxml.ElementTree import fromstring as _defused_fromstring

# The Element class defusedxml's parser produces (the stdlib class, captured from a parsed
# sentinel instead of an `import xml.*` statement). Used for type annotations only.
XmlElement = type(_defused_fromstring("<sentinel/>"))

from gov_debates.ingest.documents import IndexedSpeech
from gov_debates.normalize import text as textnorm
from gov_debates.normalize.dates import parse_iso

JURISDICTION = "nl"
JURISDICTION_LABEL = "Tweede Kamer"

API_HOST = "gegevensmagazijn.tweedekamer.nl"
ODATA_BASE = f"https://{API_HOST}/OData/v4/2.0"
SITE_HOST = "www.tweedekamer.nl"

# Report status values, most authoritative first — used to pick one report per sitting.
STATUS_PRIORITY = ["Eindpublicatie", "Gerectificeerd", "Gecorrigeerd", "Ongecorrigeerd", "Casco"]
# Statuses that are not the final corrected text.
_NON_FINAL = {"Ongecorrigeerd", "Casco"}

MIN_SPEECH_CHARS = 40


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def pick_report_per_meeting(reports: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate ~10 reports per sitting down to the most authoritative one each.

    Without this, every speech is ingested up to ten times. Preference order is STATUS_PRIORITY,
    then the most recently modified.
    """
    best: dict[str, dict[str, Any]] = {}
    for report in reports:
        if report.get("Verwijderd"):
            continue   # deleted
        meeting = str(report.get("Vergadering_Id") or report.get("Id") or "")
        if not meeting:
            continue
        current = best.get(meeting)
        if current is None or _rank(report) < _rank(current):
            best[meeting] = report
    return list(best.values())


def _rank(report: dict[str, Any]) -> tuple[int, str]:
    status = str(report.get("Status") or "")
    try:
        priority = STATUS_PRIORITY.index(status)
    except ValueError:
        priority = len(STATUS_PRIORITY)
    # Later modification wins within the same status, hence the negated string compare via reverse.
    modified = str(report.get("GewijzigdOp") or "")
    return (priority, _invert(modified))


def _invert(value: str) -> str:
    # Sorting ascending on an inverted string yields most-recent-first.
    return "".join(chr(255 - ord(c)) if ord(c) < 255 else c for c in value)


def parse_transcript(
    xml_bytes: bytes,
    *,
    report: Optional[dict[str, Any]] = None,
) -> list[IndexedSpeech]:
    """Parse one whole-meeting VLOS transcript into per-speaker rows."""
    try:
        root = _defused_fromstring(xml_bytes)
    except (XmlParseError, DefusedXmlException):
        # Malformed AND hostile (entity-expansion/XXE) payloads are both skipped, not fatal.
        return []

    report = report or {}
    status = str(report.get("Status") or "")
    text_status = "uncorrected" if status in _NON_FINAL else "final"
    meeting_date = _meeting_date(root, report)
    if not meeting_date:
        return []

    rows: list[IndexedSpeech] = []
    for activity in root.iter():
        if _local(activity.tag) != "activiteit":
            continue
        subject = _activity_subject(activity)
        for speaker_node in activity.iter():
            if _local(speaker_node.tag) != "woordvoerder":
                continue
            rows.extend(
                _parse_speaker(
                    speaker_node,
                    date=meeting_date,
                    subject=subject,
                    text_status=text_status,
                    report=report,
                )
            )
    return rows


def _parse_speaker(
    node: XmlElement,
    *,
    date: str,
    subject: Optional[str],
    text_status: str,
    report: dict[str, Any],
) -> list[IndexedSpeech]:
    """Turn one <woordvoerder> into rows: the speaker's own text, plus any interjections.

    CRITICAL: <interrumpant> is nested inside <woordvoerder>, so the interjector's text must be
    excluded from the main speaker's row and emitted separately — otherwise one member is credited
    with another's words.
    """
    rows: list[IndexedSpeech] = []
    interrupters = [n for n in node.iter() if _local(n.tag) == "interrumpant"]
    own_text = _direct_text(node, exclude=interrupters)
    speaker = _speaker_name(node)
    is_chair = str(node.get("isvoorzitter") or "").lower() == "true"

    if len(own_text) >= MIN_SPEECH_CHARS:
        rows.append(
            _build(
                date=date, subject=subject, speaker=speaker, text=own_text,
                text_status=text_status, report=report, node=node,
                role="Voorzitter" if is_chair else None, kind="speech",
            )
        )

    for interjection in interrupters:
        text = _direct_text(interjection, exclude=[])
        if len(text) < MIN_SPEECH_CHARS:
            continue
        rows.append(
            _build(
                date=date, subject=subject, speaker=_speaker_name(interjection), text=text,
                text_status=text_status, report=report, node=interjection,
                role=None, kind="interjection",
            )
        )
    return rows


def _build(
    *,
    date: str,
    subject: Optional[str],
    speaker: Optional[str],
    text: str,
    text_status: str,
    report: dict[str, Any],
    node: XmlElement,
    role: Optional[str],
    kind: str,
) -> IndexedSpeech:
    object_id = node.get("objectid") or ""
    extras: dict[str, Any] = {"kind": kind}
    if object_id:
        # XML-local GUIDs 404 against OData, so they are opaque provenance only.
        extras["vlos_object_id"] = object_id
    if report.get("Id"):
        extras["verslag_id"] = report["Id"]
    if report.get("Status"):
        extras["verslag_status"] = report["Status"]

    return IndexedSpeech(
        jurisdiction=JURISDICTION,
        jurisdiction_label=JURISDICTION_LABEL,
        doc_id=f"nl:{date}:{object_id or _stable_suffix(text)}",
        source_url=f"https://{SITE_HOST}/kamerstukken/verslagen",
        title=textnorm.clean(subject) or "Vergadering",
        date=date,
        speaker=speaker,
        # The transcript carries no usable person id and no party; fuzzy name matching against
        # Persoon would misattribute, so both are left unset rather than guessed.
        group=None,
        party=None,
        role=role,
        chamber="Tweede Kamer",
        term=None,
        session_ref=None,
        language_original="nl",
        language_text="nl",
        is_translation=False,
        text_status=text_status,
        extras=extras,
        full_text=text,
    )


def _direct_text(node: XmlElement, *, exclude: list[XmlElement]) -> str:
    """Collect <tekst> content belonging to this node, skipping excluded subtrees.

    ``exclude`` holds nested <interrumpant> elements. Everything inside them belongs to the
    interjector, not to this speaker — attributing it here is exactly the misattribution bug this
    parser exists to avoid.
    """
    # One pass to collect every node id inside an excluded subtree.
    excluded: set[int] = set()
    for element in exclude:
        for descendant in element.iter():
            excluded.add(id(descendant))

    parts: list[str] = []
    for child in node.iter():
        if _local(child.tag) != "tekst" or id(child) in excluded:
            continue
        joined = " ".join(t for t in child.itertext() if t and t.strip())
        if joined.strip():
            parts.append(joined.strip())
    return textnorm.clean(" ".join(parts))


def _speaker_name(node: XmlElement) -> Optional[str]:
    for child in node.iter():
        if _local(child.tag) != "spreker":
            continue
        pieces = [
            _child_text(child, "verslagnaam"),
            _child_text(child, "achternaam"),
            _child_text(child, "voornaam"),
        ]
        for piece in pieces:
            if piece:
                return textnorm.clean(piece)
        joined = " ".join(t for t in child.itertext() if t and t.strip())
        if joined.strip():
            return textnorm.clean(joined)
    return None


def _child_text(node: XmlElement, name: str) -> Optional[str]:
    for child in node.iter():
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _activity_subject(activity: XmlElement) -> Optional[str]:
    for child in activity.iter():
        if _local(child.tag) in ("onderwerp", "titel") and child.text and child.text.strip():
            return child.text.strip()
    return activity.get("soort") or None


def _meeting_date(root: XmlElement, report: dict[str, Any]) -> Optional[str]:
    for child in root.iter():
        if _local(child.tag) in ("datum", "vergaderdatum") and child.text:
            parsed = parse_iso(child.text.strip())
            if parsed:
                return parsed
    for key in ("Datum", "GewijzigdOp"):
        parsed = parse_iso(str(report.get(key) or ""))
        if parsed:
            return parsed
    return None


def _stable_suffix(text: str) -> str:
    import hashlib

    # Deterministic id fragment, not a security control: collisions are harmless and
    # nothing is authenticated with this digest. SHA-256 nonetheless, so no weak-hash
    # primitive appears anywhere in the codebase.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
