"""The ingest-side document model.

An ``IndexedSpeech`` is a normalized :class:`~gov_debates.contracts.SpeechResult` plus the fields
an index needs: the full verbatim text (not just a snippet) and a normalized search key. Ingest
jobs produce these; the query adapters for the batch-ingested jurisdictions consume them and
project them back onto the wire contract, so those jurisdictions return exactly the same shape as
the live-API ones.

Keeping the projection in one place matters: the whole point of the batch sources is that the user
cannot tell the difference, and the frontend renders them through the same generated type.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from gov_debates.contracts import SpeechResult
from gov_debates.normalize import text as textnorm


@dataclass
class IndexedSpeech:
    """One speech as stored in our own index."""

    # --- the wire contract fields (mirrors SpeechResult) ---
    jurisdiction: str
    jurisdiction_label: str
    doc_id: str
    source_url: Optional[str]
    title: str
    date: str                       # ISO YYYY-MM-DD
    speaker: Optional[str] = None
    group: Optional[str] = None
    party: Optional[str] = None
    role: Optional[str] = None
    chamber: Optional[str] = None
    term: Optional[str] = None
    session_ref: Optional[str] = None
    language_original: Optional[str] = None
    language_text: Optional[str] = None
    is_translation: bool = False
    text_status: str = "final"
    extras: dict[str, Any] = field(default_factory=dict)

    # --- index-only fields (never returned verbatim on the wire) ---
    full_text: str = ""
    #: Lowercased, accent-folded text used for matching. Derived; do not set by hand.
    search_key: str = ""

    def __post_init__(self) -> None:
        if not self.search_key:
            self.search_key = build_search_key(self.title, self.speaker, self.full_text)

    def to_speech_result(self, *, snippet_query: Optional[str] = None) -> SpeechResult:
        """Project onto the wire contract, with a snippet centred on the query when given."""
        snippet = (
            textnorm.snippet_around(self.full_text, snippet_query, max_chars=600)
            if snippet_query
            else textnorm.snippet(self.full_text)
        )
        return SpeechResult(
            jurisdiction=self.jurisdiction,
            jurisdiction_label=self.jurisdiction_label,
            doc_id=self.doc_id,
            source_url=self.source_url,
            title=self.title,
            date=self.date,
            snippet=snippet or None,
            speaker=self.speaker,
            group=self.group,
            party=self.party,
            role=self.role,
            chamber=self.chamber,
            term=self.term,
            session_ref=self.session_ref,
            language_original=self.language_original,
            language_text=self.language_text,
            is_translation=self.is_translation,  # nosemgrep: is-function-without-parentheses - dataclass field
            text_status=self.text_status,
            extras=dict(self.extras),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "IndexedSpeech":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


def fold(value: str | None) -> str:
    """Lowercase and strip accents so queries match regardless of diacritics.

    Necessary for these three sources specifically: French and Dutch transcripts are full of
    accented characters, and a user typing "energie" should still find "énergie".
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.lower()).strip()


def build_search_key(*parts: str | None) -> str:
    """Build the normalized matching key from title, speaker and body text."""
    return fold(" ".join(p for p in parts if p))


def matches(speech: IndexedSpeech, terms: Iterable[str]) -> bool:
    """True when every term appears in the speech's search key (AND semantics).

    AND rather than OR is deliberate: these indexes are built from whole sittings, so OR would
    match almost everything and make the results useless.
    """
    key = speech.search_key or build_search_key(speech.title, speech.speaker, speech.full_text)
    return all(fold(t) in key for t in terms if t and t.strip())


def tokenize(query: str | None) -> list[str]:
    """Split a query into matchable terms, honouring "quoted phrases"."""
    if not query:
        return []
    phrases = re.findall(r'"([^"]+)"', query)
    remainder = re.sub(r'"[^"]*"', " ", query)
    words = [w for w in re.split(r"\s+", remainder) if w.strip()]
    return [p.strip() for p in phrases if p.strip()] + words
