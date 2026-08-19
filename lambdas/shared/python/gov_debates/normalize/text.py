"""Text normalization: HTML/markup stripping, whitespace, and snippet extraction.

Verified needs (docs/multi-gov/source-profiles):
  * UK ``ContributionTextFull`` embeds ``<span class="column-number" ...>`` markers and
    ``<em>`` search-highlight tags that pollute an index if unstripped.
  * DE/AT payloads contain U+00A0 non-breaking spaces inside citations and party names
    ("199. Sitzung", "BUENDNIS 90/DIE GRUENEN"); comparisons silently miss unless normalized.
  * Full documents are multi-MB; snippets must be bounded and, ideally, centred on the query
    term rather than always the start (ported from the original ``_excerpt``).
"""
from __future__ import annotations

import html
import re
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Unicode spaces that must collapse to a plain ASCII space before matching. DE/AT payloads
# embed U+00A0 inside citations and party names, so unnormalized comparisons silently miss.
# Escapes are explicit so the set is reviewable: no-break, narrow no-break, thin, figure,
# and zero-width spaces.
_UNICODE_SPACE_RE = re.compile("[\u00a0\u202f\u2009\u2007\u200b]")


def strip_tags(text: str | None) -> str:
    """Remove HTML/XML tags and collapse whitespace. Returns "" for falsy input."""
    if not text:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", text)).strip()


def nbsp_fix(text: str | None) -> str:
    """Replace U+00A0 and related unicode spaces with a plain ASCII space."""
    if not text:
        return ""
    return _UNICODE_SPACE_RE.sub(" ", text)


def unescape_entities(text: str | None) -> str:
    """Decode HTML/XML character entities (``&amp;`` -> ``&``, ``&#233;`` -> ``é``).

    Required for sources that embed markup: the EP serves speech text as an XML fragment where a
    political group reads ``S&amp;D``, and undecoded entities would surface in citations.
    """
    if not text:
        return ""
    return html.unescape(text)


def clean(text: str | None) -> str:
    """Full cleanup: tag strip + entity decode + nbsp fix + whitespace collapse.

    Order matters: tags are stripped FIRST, then entities decoded. Decoding first would turn an
    escaped ``&lt;hello&gt;`` (real content) into ``<hello>``, which the tag stripper would then
    delete. Stripping first leaves escaped angle brackets intact for the decode step.
    """
    return _WS_RE.sub(" ", nbsp_fix(unescape_entities(strip_tags(text)))).strip()


def snippet(text: str | None, *, limit: int = 600) -> Optional[str]:
    """Return a cleaned snippet of at most ``limit`` chars, ellipsized. None for empty."""
    cleaned = clean(text)
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + " …"


def snippet_around(text: str | None, query: str | None, *, max_chars: int = 6000) -> str:
    """Return up to ``max_chars`` of ``text`` centred on the best match of ``query``.

    Tries the full query first, then individual terms (longest first), so a multi-word query
    still locates a relevant passage deep inside a large document. Ported from the original
    DIP ``_excerpt`` and reused by every adapter's ``get_debate_text``.
    """
    cleaned = clean(text)
    if not cleaned:
        return ""
    if len(cleaned) <= max_chars:
        return cleaned
    if query:
        lowered = cleaned.lower()
        candidates = [query.strip().lower()]
        candidates += sorted(
            {w for w in query.lower().split() if len(w) > 3}, key=len, reverse=True
        )
        for term in candidates:
            idx = lowered.find(term)
            if idx != -1:
                start = max(0, idx - max_chars // 3)
                end = start + max_chars
                prefix = "… " if start > 0 else ""
                suffix = " …" if end < len(cleaned) else ""
                return prefix + cleaned[start:end].rstrip() + suffix
    return cleaned[:max_chars].rstrip() + " …"


def clamp_max_chars(value: object, *, lo: int = 500, hi: int = 20000, default: int = 6000) -> int:
    """Clamp a caller-supplied ``max_chars`` into ``[lo, hi]`` (schema subset has no bounds)."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))
