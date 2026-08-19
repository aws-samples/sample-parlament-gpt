"""Tests for date and text normalizers, pinning the verified per-source format traps."""
import pytest

from gov_debates.normalize import dates, text


class TestDates:
    def test_clean_iso_date(self):
        assert dates.parse_iso("2026-06-11") == "2026-06-11"

    def test_iso_datetime_without_tz_keeps_day(self):
        # UK "2024-12-19T00:00:00" and NL local-midnight are already the intended day.
        assert dates.parse_iso("2024-12-19T00:00:00") == "2024-12-19"

    def test_nl_offset_midnight_keeps_local_day(self):
        assert dates.parse_iso("2026-06-04T00:00:00+02:00") == "2026-06-04"

    def test_at_utc_shift_trap_via_offset(self):
        # A 23:00Z timestamp is next-day in CET; naive slicing would give 2023-12-14 (wrong).
        assert dates.to_utc_shifted_local_day("2023-12-14T23:00:00.000Z", local_offset_hours=1) == "2023-12-15"

    def test_parse_iso_on_pure_Z_uses_utc_day(self):
        # Without a known local zone, a pure-Z instant reports its UTC day.
        assert dates.parse_iso("2023-12-14T23:00:00.000Z") == "2023-12-14"

    def test_ch_bare_yyyymmdd(self):
        assert dates.parse_yyyymmdd("20241218") == "2024-12-18"
        assert dates.parse_yyyymmdd("2024-12-18") is None  # not the bare form

    def test_compact_17char_timestamp(self):
        assert dates.parse_compact_ts("20260721150000000") == "2026-07-21"
        assert dates.parse_compact_ts("20260721150000") == "2026-07-21"

    def test_au_day_first_unpadded(self):
        assert dates.parse_ddmmyyyy("8/10/2025") == "2025-10-08"
        assert dates.parse_ddmmyyyy("08/10/2025") == "2025-10-08"
        assert dates.parse_ddmmyyyy("2025-10-08") is None

    @pytest.mark.parametrize("bad", [None, "", "garbage", "2026/06/11"])
    def test_unparseable_returns_none(self, bad):
        assert dates.parse_iso(bad) is None


class TestText:
    def test_strip_tags_and_collapse(self):
        assert text.strip_tags("<p>Hello   <em>world</em></p>") == "Hello world"

    def test_nbsp_fix_replaces_unicode_spaces(self):
        raw = "199.\u00a0Sitzung"
        assert text.nbsp_fix(raw) == "199. Sitzung"
        assert "\u00a0" not in text.nbsp_fix("BUENDNIS\u00a090/DIE\u00a0GRUENEN")
        assert text.nbsp_fix("thin\u2009space\u200bzero") == "thin space zero"

    def test_clean_combines_both(self):
        assert text.clean("<span>a b</span>  c") == "a b c"

    def test_unescape_entities(self):
        assert text.unescape_entities("S&amp;D") == "S&D"
        assert text.unescape_entities("caf&#233;") == "caf\u00e9"
        assert text.unescape_entities(None) == ""

    def test_clean_decodes_entities_after_stripping_tags(self):
        # EP speech text embeds markup AND entities: <organization>S&amp;D</organization>.
        assert text.clean("<organization>S&amp;D</organization>") == "S&D"
        # Order matters: an escaped angle bracket in real content must survive tag stripping.
        assert text.clean("compare &lt;b&gt; markup") == "compare <b> markup"

    def test_snippet_bounds_and_ellipsizes(self):
        assert text.snippet("short") == "short"
        assert text.snippet(None) is None
        long = "x " * 1000
        s = text.snippet(long, limit=50)
        assert len(s) <= 55 and s.endswith("…")

    def test_snippet_around_centres_on_query(self):
        body = ("A" * 5000) + " KLIMASCHUTZ marker " + ("B" * 5000)
        out = text.snippet_around(body, "klimaschutz", max_chars=200)
        assert "KLIMASCHUTZ" in out
        assert out.startswith("… ") and out.endswith(" …")

    def test_snippet_around_falls_back_to_start(self):
        body = "C" * 10000
        out = text.snippet_around(body, "absent", max_chars=100)
        assert out.startswith("C") and out.endswith("…")

    def test_clamp_max_chars(self):
        assert text.clamp_max_chars(999999) == 20000
        assert text.clamp_max_chars(10) == 500
        assert text.clamp_max_chars("nope") == 6000
