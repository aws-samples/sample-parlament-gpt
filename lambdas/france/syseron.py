"""France (Assemblée nationale) — bulk XML ingest parser.

France has NO official queryable debates API (verified): the search endpoint returns HTML only, its
``seance_date`` and ``limit`` params are inert (a silent-wrong-results trap), and there is no
date-range query at all. The canonical, speech-level source is instead a bulk ZIP per legislature:

    https://data.assemblee-nationale.fr/static/openData/repository/{leg}/vp/syceronbrut/syseron.xml.zip

55.7 MB compressed / 324 MB unpacked / 601 per-sitting XML files, refreshed nightly (~02:05 UTC).
Per-document fetch by uid is NOT possible (404) — it is all-or-nothing, so we ingest the ZIP and
diff on subsequent runs.

Each sitting file carries ``metadonnees`` (date, legislature, session, sitting numbers) and a
``contenu`` tree of ``point``/``paragraphe``, where every ``paragraphe`` is ONE contribution with
``orateurs/orateur/nom`` + ``id`` and the verbatim ``texte``.

This module is the parser only: it turns one sitting XML into IndexedSpeech rows. The ingest Lambda
handles fetching/unzipping and writing shards.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any, Iterator, Optional

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

JURISDICTION = "fr"
JURISDICTION_LABEL = "Assemblée nationale"

DATA_HOST = "data.assemblee-nationale.fr"
SITE_HOST = "www.assemblee-nationale.fr"
BULK_URL = (
    f"https://{DATA_HOST}/static/openData/repository/{{legislature}}/vp/syceronbrut/syseron.xml.zip"
)

# Minimum characters for a paragraph to count as a speech rather than a procedural fragment.
MIN_SPEECH_CHARS = 40


def _local(tag: str) -> str:
    """Strip any XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _findtext(element: XmlElement, name: str) -> Optional[str]:
    """Namespace-agnostic descendant text lookup."""
    for node in element.iter():
        if _local(node.tag) == name and node.text and node.text.strip():
            return node.text.strip()
    return None


def _find(element: XmlElement, name: str) -> Optional[XmlElement]:
    for node in element.iter():
        if _local(node.tag) == name:
            return node
    return None


def iter_sitting_files(zip_bytes: bytes) -> Iterator[tuple[str, bytes]]:
    """Yield (filename, xml bytes) for every sitting file in the bulk ZIP."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".xml"):
                continue
            yield info.filename, archive.read(info)


def parse_sitting(xml_bytes: bytes, *, legislature: Optional[str] = None) -> list[IndexedSpeech]:
    """Parse one sitting XML into speech rows."""
    try:
        root = _defused_fromstring(xml_bytes)
    except (XmlParseError, DefusedXmlException):
        # Malformed AND hostile (entity-expansion/XXE) payloads are both skipped, not fatal:
        # one bad sitting file must not abort a whole ingest batch.
        return []

    meta = _sitting_metadata(root, legislature)
    if not meta["date"]:
        # Without a date we cannot shard or satisfy the contract's ISO date requirement.
        return []

    rows: list[IndexedSpeech] = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "paragraphe":
            continue
        speech = _parse_paragraph(paragraph, meta)
        if speech is not None:
            rows.append(speech)
    return rows


def _sitting_metadata(root: XmlElement, legislature: Optional[str]) -> dict[str, Any]:
    raw_date = (
        _findtext(root, "dateSeance")
        or _findtext(root, "dateSeanceJour")
        or _findtext(root, "date")
    )
    return {
        "date": _parse_fr_date(raw_date),
        "legislature": _findtext(root, "legislature") or legislature,
        "session": _findtext(root, "session"),
        "sitting": _findtext(root, "numSeance") or _findtext(root, "numSeanceJour"),
        "uid": _findtext(root, "uid") or _findtext(root, "refUid"),
        "title": _findtext(root, "titreStruct") or _findtext(root, "titre"),
    }


def _parse_fr_date(raw: Optional[str]) -> Optional[str]:
    """Handle both ISO dates and the 17-char compact form (YYYYMMDDHHmmssSSS)."""
    if not raw:
        return None
    text = raw.strip()
    iso = parse_iso(text)
    if iso:
        return iso
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _parse_paragraph(paragraph: XmlElement, meta: dict[str, Any]) -> Optional[IndexedSpeech]:
    body = _paragraph_text(paragraph)
    if len(body) < MIN_SPEECH_CHARS:
        return None

    speaker_node = _find(paragraph, "orateur")
    speaker = None
    speaker_id = None
    if speaker_node is not None:
        speaker = _findtext(speaker_node, "nom")
        speaker_id = _findtext(speaker_node, "id")

    para_id = paragraph.get("id") or _findtext(paragraph, "id") or ""
    doc_id = f"fr:{meta['date']}:{para_id or _stable_suffix(body)}"

    extras: dict[str, Any] = {}
    if speaker_id:
        extras["acteur_uid"] = speaker_id
    if meta.get("session"):
        extras["session"] = meta["session"]
    if meta.get("uid"):
        extras["sitting_uid"] = meta["uid"]

    return IndexedSpeech(
        jurisdiction=JURISDICTION,
        jurisdiction_label=JURISDICTION_LABEL,
        doc_id=doc_id,
        source_url=_source_url(meta),
        title=textnorm.clean(meta.get("title")) or "Séance publique",
        date=meta["date"],
        speaker=textnorm.clean(speaker) or None,
        # The bulk XML carries no group; it needs a per-date acteur-info-card lookup, so we do not
        # guess one here rather than risk attributing the wrong party for the sitting date.
        group=None,
        party=None,
        role=None,
        chamber="Assemblée nationale",
        term=str(meta["legislature"]) if meta.get("legislature") else None,
        session_ref=_session_ref(meta),
        language_original="fr",
        language_text="fr",
        is_translation=False,
        text_status="final",
        extras=extras,
        full_text=body,
    )


def _paragraph_text(paragraph: XmlElement) -> str:
    """Extract the verbatim text of one contribution."""
    for node in paragraph.iter():
        if _local(node.tag) == "texte":
            joined = " ".join(t for t in node.itertext() if t and t.strip())
            if joined.strip():
                return textnorm.clean(joined)
    return textnorm.clean(" ".join(t for t in paragraph.itertext() if t and t.strip()))


def _stable_suffix(body: str) -> str:
    """A deterministic id fragment for paragraphs that carry no id of their own."""
    import hashlib

    # Deterministic id fragment, not a security control: collisions are harmless and
    # nothing is authenticated with this digest. SHA-256 nonetheless, so no weak-hash
    # primitive appears anywhere in the codebase.
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def _session_ref(meta: dict[str, Any]) -> Optional[str]:
    parts = [meta.get("legislature"), meta.get("sitting")]
    present = [str(p) for p in parts if p]
    return "/".join(present) or None


def _source_url(meta: dict[str, Any]) -> Optional[str]:
    """Link to the public transcript page.

    The per-sitting slug only exists in the HTML index, which the bulk XML does not carry — so we
    link to the legislature's transcript listing rather than fabricate a slug that may 404.
    """
    legislature = meta.get("legislature")
    if not legislature:
        return None
    return f"https://{SITE_HOST}/dyn/{legislature}/comptes-rendus/seance"
