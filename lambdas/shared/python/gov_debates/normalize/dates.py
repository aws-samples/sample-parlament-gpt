"""Date normalization to ISO-8601 ``YYYY-MM-DD``.

Every adapter must emit ``date`` as a normalized ISO date; the contract validator rejects
anything else. The raw upstream forms are wildly inconsistent and several are traps, all
verified live (docs/multi-gov/source-profiles):

  * DE ``datum="2026-06-11"``                       — already clean.
  * DE/FR 17-char ``"20260721150000000"``            — YYYYMMDDHHmmssSSS.
  * CH ``MeetingDate="20241218"``                    — bare YYYYMMDD string.
  * UK ``"2024-12-19T00:00:00"``                     — ISO datetime, no tz.
  * NL ``"2026-06-04T00:00:00+02:00"``               — local-midnight with offset.
  * AT ``"2023-12-14T23:00:00.000Z"``                — the 15 Dec sitting shifted to UTC;
    naive first-10-chars slicing yields the WRONG day. Convert to the local calendar day
    first (Austria is UTC+1/+2, so a 23:00Z timestamp is next-day local).
  * AU ``"8/10/2025"``                               — day-first and NOT zero-padded.

These functions return a normalized ``YYYY-MM-DD`` string, or ``None`` if the input cannot be
parsed (adapters should treat an unparseable date as a data error for that row).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional


def parse_iso(value: str | None) -> Optional[str]:
    """Parse an ISO-8601 date or datetime and return the date part ``YYYY-MM-DD``.

    Handles a trailing ``Z``, fractional seconds, and timezone offsets. When an offset (or
    ``Z``) is present the instant is converted to the offset-local calendar day BEFORE
    taking the date — otherwise a 23:00Z timestamp reports the wrong day (the AT trap). When
    no timezone is present the date part is taken as-is (UK/NL local-midnight are already the
    intended day).
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # datetime.fromisoformat handles offsets and (3.11+) a trailing "Z".
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Fall back to a plain date prefix if it looks like YYYY-MM-DD...
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return None
    if dt.tzinfo is not None:
        # Convert to the record's own local wall-clock day, not UTC. We do not know the
        # source's local zone in general, but the offset carried in the timestamp IS the
        # source's local offset, so honor it.
        dt = dt.astimezone(dt.tzinfo)
    return dt.date().isoformat()


def parse_yyyymmdd(value: str | None) -> Optional[str]:
    """Parse a bare ``YYYYMMDD`` string (CH ``MeetingDate``)."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not re.fullmatch(r"\d{8}", s):
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_compact_ts(value: str | None) -> Optional[str]:
    """Parse a ``YYYYMMDDHHmmssSSS`` (17-char) or ``YYYYMMDDHHmmss`` compact timestamp.

    Used by some DE/FR fields. Only the date portion is returned.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not re.fullmatch(r"\d{14}(\d{3})?", s):
        return None
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def parse_ddmmyyyy(value: str | None) -> Optional[str]:
    """Parse a day-first ``D/M/YYYY`` or ``DD/MM/YYYY`` date, zero-padded or not (AU)."""
    if not value or not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*", value)
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def to_utc_shifted_local_day(value: str | None, *, local_offset_hours: int) -> Optional[str]:
    """Parse a UTC (``...Z``) timestamp and return the calendar day at ``local_offset_hours``.

    For the Austrian case where the sitting date is stored as UTC (a 15 Dec sitting appears
    as ``2023-12-14T23:00:00.000Z``). Pass ``local_offset_hours=1`` (CET) so it maps back to
    the 15th. When the source varies between +1 and +2 (DST), prefer :func:`parse_iso` on a
    timestamp that actually carries the offset; this helper is for pure-``Z`` inputs.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=local_offset_hours)))
    return local.date().isoformat()
