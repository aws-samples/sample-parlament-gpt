"""The normalized debate/speech result schema — the cross-layer contract.

This is the single source of truth for the shape every adapter emits and the frontend
renders. It is deliberately owned in ONE place because ten independently authored
adapters plus a hand-mirrored TypeScript type in the frontend is the ideal breeding
ground for silent drift: a renamed field produces no error anywhere, citations just
render blank. The TypeScript ``Source`` type is generated from this module (see
``scripts/gen_source_type.py``) so drift becomes a red build.

Every adapter MUST return :func:`to_results_envelope` output, i.e. a dict with a
top-level ``results`` list. That literal ``results`` key is the sentinel the agent's
``_extract_sources``/``_extract_steps`` rely on; do not rename it.

Field rationale (condensed from docs/multi-gov/ADR-001 §2):

* ``group`` and ``party`` are SEPARATE fields, not one. Switzerland proves they differ:
  ``ParlGroupName`` (Fraktion) vs a ``MemberCouncil`` join yielding the actual party.
  The EU has the same split (EU political group vs national party). Fill whichever the
  source gives; populate ``group`` and leave ``party`` null when only one is available.
  Never guess one from the other.
* ``term`` is a STRING — it holds Roman numerals (AT "XXVII"), hyphenated session-years
  (NL "2025-2026") and plain ints without lossy coercion.
* ``language_original``/``language_text``/``is_translation`` exist because the EU and
  Canada serve machine translation as if it were speech. The UI must be able to label a
  quote that is not the speaker's own words.
* ``text_status`` (final|uncorrected|scanned) exists because recent debates are mutable
  in several systems (NL, AU, AT, FR); caching them as final is a correctness bug.
* ``doc_id`` is an opaque compound string. Germany needs it compound because the search
  id and the full-text id live in different namespaces. Callers must never construct one.
* ``extras`` carries genuinely non-generalizable per-source fields and must never be
  rendered blindly by the frontend.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal, Optional

# The jurisdictions this system can emit. Keep in sync with the CDK JURISDICTIONS table
# (infra/lib/jurisdictions.ts) and the frontend jurisdiction labels.
Jurisdiction = Literal["de", "eu", "uk", "us", "ch", "at", "ca", "au", "fr", "nl"]

TextStatus = Literal["final", "uncorrected", "scanned"]

# Values considered valid for text_status; adapters that cannot determine it use "final"
# only when the source guarantees the record is immutable, else "uncorrected".
_TEXT_STATUS_VALUES = ("final", "uncorrected", "scanned")


@dataclass
class SpeechResult:
    """One normalized parliamentary contribution (a speech or spoken intervention).

    Required fields have no default; nullable fields default to ``None``. Construct via
    the adapter, then serialize the whole page with :func:`to_results_envelope`.
    """

    # --- identity & provenance (required) ---
    jurisdiction: str            # de|eu|uk|us|ch|at|ca|au|fr|nl
    jurisdiction_label: str      # human label, e.g. "German Bundestag"
    doc_id: str                  # opaque, adapter-defined; used for get_debate_text refetch
    source_url: Optional[str]    # citation deep link (nullable: some historic rows lack one)

    # --- content (required; snippet may be null) ---
    title: str                   # the debate/section title (NOT the speaker — see DE gotcha)
    date: str                    # ISO-8601 date YYYY-MM-DD, ALWAYS normalized
    snippet: Optional[str] = None

    # --- attribution (all nullable) ---
    speaker: Optional[str] = None
    group: Optional[str] = None  # parliamentary group / Fraktion / caucus / political group
    party: Optional[str] = None  # the actual political party — DIFFERENT from group
    role: Optional[str] = None   # e.g. "Bundesminister für Arbeit und Soziales"

    # --- context (all nullable) ---
    chamber: Optional[str] = None
    term: Optional[str] = None       # STRING, not int
    session_ref: Optional[str] = None

    # --- text fidelity (required, with safe defaults) ---
    language_original: Optional[str] = None  # what was actually SPOKEN (ISO 639-1)
    language_text: Optional[str] = None      # language of `snippet`
    is_translation: bool = False             # true => snippet is not the speaker's own words
    text_status: TextStatus = "final"        # final | uncorrected | scanned

    # --- escape hatch (never rendered blindly) ---
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class SchemaViolation(ValueError):
    """Raised by :func:`validate` when a result does not satisfy the contract."""


# Fields that must be present and non-empty on every emitted result.
_REQUIRED_NONEMPTY = ("jurisdiction", "jurisdiction_label", "doc_id", "title", "date")


def validate(result: dict[str, Any]) -> dict[str, Any]:
    """Validate one serialized result against the contract; return it unchanged.

    Raises :class:`SchemaViolation` on any breach. Adapters call this on every row so a
    mapping bug surfaces at the adapter (with a fixture test) rather than as blank
    citations three layers away.
    """
    for key in _REQUIRED_NONEMPTY:
        value = result.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SchemaViolation(f"missing/empty required field {key!r}")

    date = result["date"]
    # ISO-8601 date, exactly YYYY-MM-DD (adapters normalize before emitting).
    if len(date) != 10 or date[4] != "-" or date[7] != "-" or not _is_iso_date(date):
        raise SchemaViolation(f"date {date!r} is not normalized ISO YYYY-MM-DD")

    status = result.get("text_status", "final")
    if status not in _TEXT_STATUS_VALUES:
        raise SchemaViolation(f"text_status {status!r} not in {_TEXT_STATUS_VALUES}")

    if not isinstance(result.get("is_translation", False), bool):
        raise SchemaViolation("is_translation must be a bool")

    extras = result.get("extras", {})
    if not isinstance(extras, dict):
        raise SchemaViolation("extras must be an object")

    return result


def _is_iso_date(s: str) -> bool:
    try:
        y, m, d = s.split("-")
        int(y), int(m), int(d)
        return 1 <= int(m) <= 12 and 1 <= int(d) <= 31
    except (ValueError, TypeError):
        return False


def to_results_envelope(
    results: list[SpeechResult | dict[str, Any]],
    *,
    jurisdiction: str,
    total: Optional[int] = None,
    cursor: Optional[str] = None,
    truncated: bool = False,
    validate_each: bool = True,
) -> dict[str, Any]:
    """Build the top-level envelope every adapter returns to the Gateway.

    The ``results`` key is load-bearing: the agent detects a search result by finding a
    top-level ``results`` list (see agent ``_extract_sources``). ``total`` defaults to the
    number of returned results when the source does not report a grand total.
    """
    rows: list[dict[str, Any]] = []
    for r in results:
        row = r.to_dict() if isinstance(r, SpeechResult) else dict(r)
        if validate_each:
            validate(row)
        rows.append(row)

    envelope: dict[str, Any] = {
        "results": rows,
        "total": total if total is not None else len(rows),
        "jurisdiction": jurisdiction,
        "truncated": truncated,
    }
    if cursor is not None:
        envelope["cursor"] = cursor
    return envelope
