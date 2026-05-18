"""Tests for the UTC/GMT time-detection + conversion module."""

from __future__ import annotations

from datetime import date

from zeenova_bot.timezones import (
    ParsedUtcTime,
    detect_utc_time,
    format_utc_card,
)


class TestDetectUtcTime:
    def test_lone_hour_with_utc(self) -> None:
        parsed = detect_utc_time("13 UTC")
        assert parsed == ParsedUtcTime(hour=13, minute=0, matched="13 UTC")

    def test_lone_hour_with_gmt(self) -> None:
        parsed = detect_utc_time("9 GMT")
        assert parsed is not None
        assert parsed.hour == 9
        assert parsed.minute == 0

    def test_colon_minutes(self) -> None:
        parsed = detect_utc_time("13:30 UTC")
        assert parsed == ParsedUtcTime(hour=13, minute=30, matched="13:30 UTC")

    def test_midnight_24h(self) -> None:
        parsed = detect_utc_time("0:00 UTC")
        assert parsed is not None
        assert parsed.hour == 0
        assert parsed.minute == 0

    def test_almost_midnight_24h(self) -> None:
        parsed = detect_utc_time("23:59 UTC")
        assert parsed is not None
        assert parsed.hour == 23
        assert parsed.minute == 59

    def test_military_time(self) -> None:
        parsed = detect_utc_time("0900 UTC")
        assert parsed is not None
        assert parsed.hour == 9
        assert parsed.minute == 0

    def test_pm_converts_to_24h(self) -> None:
        parsed = detect_utc_time("1:30 PM UTC")
        assert parsed is not None
        assert parsed.hour == 13
        assert parsed.minute == 30

    def test_am_keeps_morning_hour(self) -> None:
        parsed = detect_utc_time("9 AM UTC")
        assert parsed is not None
        assert parsed.hour == 9

    def test_12_am_is_midnight(self) -> None:
        parsed = detect_utc_time("12 AM UTC")
        assert parsed is not None
        assert parsed.hour == 0

    def test_12_pm_is_noon(self) -> None:
        parsed = detect_utc_time("12 PM UTC")
        assert parsed is not None
        assert parsed.hour == 12

    def test_compact_pm_form(self) -> None:
        parsed = detect_utc_time("1pm UTC")
        assert parsed is not None
        assert parsed.hour == 13

    def test_picks_first_match_when_multiple(self) -> None:
        # The colon form must win over the lone-hour form when both
        # patterns could apply against the same text.
        parsed = detect_utc_time("13:30 UTC")
        assert parsed is not None
        assert parsed.minute == 30

    def test_detects_inside_a_sentence(self) -> None:
        parsed = detect_utc_time("FOMC press release scheduled at 14:00 UTC tonight")
        assert parsed is not None
        assert parsed.hour == 14
        assert parsed.minute == 0

    def test_returns_none_on_plain_number(self) -> None:
        # No UTC/GMT suffix → silent.
        assert detect_utc_time("I have 13 messages") is None

    def test_returns_none_on_empty(self) -> None:
        assert detect_utc_time("") is None
        assert detect_utc_time("   ") is None

    def test_returns_none_on_unrelated_text(self) -> None:
        assert detect_utc_time("hello world") is None
        assert detect_utc_time("btc to the moon") is None

    def test_case_insensitive_marker(self) -> None:
        assert detect_utc_time("13 utc") is not None
        assert detect_utc_time("13 gmt") is not None
        assert detect_utc_time("13 Utc") is not None


class TestFormatUtcCard:
    def test_renders_all_four_timezone_rows(self) -> None:
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        # The team picked these four (Cairo + CET + MSK + CST) and
        # explicitly asked for no flags on any row.
        assert "Cairo" in body
        assert "CET" in body
        assert "MSK" in body
        assert "CST" in body
        # Removed earlier timezones / flag glyphs must not regress.
        assert "Moscow" not in body
        assert "Riyadh" not in body
        assert "Dubai" not in body
        assert "🇪🇬" not in body
        assert "🇷🇺" not in body

    def test_header_carries_normalised_24h_time(self) -> None:
        parsed = ParsedUtcTime(hour=13, minute=30, matched="1:30 PM UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        assert "13:30 UTC" in body

    def test_footer_echoes_detected_substring(self) -> None:
        parsed = ParsedUtcTime(hour=13, minute=0, matched="1pm UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        assert "1pm UTC" in body
        assert "Detected:" in body

    def test_cairo_is_three_hours_ahead_in_summer(self) -> None:
        # 2026-05-06 → Egypt is on DST (EEST, UTC+3). The card must
        # reflect the real offset for the day rather than a hard-coded
        # +2 assumption.
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        # 13:00 UTC + 3h = 16:00 Cairo.
        cairo_line = next(line for line in body.splitlines() if "Cairo" in line)
        assert "16:00" in cairo_line

    def test_cet_is_two_hours_ahead_in_summer(self) -> None:
        # 2026-05-06 → Central Europe is on DST (CEST, UTC+2).
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        cet_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("CET")
        )
        assert "15:00" in cet_line

    def test_cet_is_one_hour_ahead_in_winter(self) -> None:
        # 2026-01-15 → off DST (CET, UTC+1).
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 1, 15))
        cet_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("CET")
        )
        assert "14:00" in cet_line

    def test_msk_is_three_hours_ahead(self) -> None:
        # Moscow has been a fixed UTC+3 since 2014 (no DST).
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        msk_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("MSK")
        )
        assert "16:00" in msk_line

    def test_cst_is_five_hours_behind_in_summer(self) -> None:
        # 2026-05-06 → US Central is on DST (CDT, UTC-5). 13:00 UTC
        # becomes 08:00 in Chicago.
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        cst_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("CST")
        )
        assert "08:00" in cst_line

    def test_cst_is_six_hours_behind_in_winter(self) -> None:
        # 2026-01-15 → off DST (CST proper, UTC-6). 13:00 UTC becomes
        # 07:00 in Chicago.
        parsed = ParsedUtcTime(hour=13, minute=0, matched="13 UTC")
        body = format_utc_card(parsed, today=date(2026, 1, 15))
        cst_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("CST")
        )
        assert "07:00" in cst_line

    def test_next_day_tag_for_late_utc_hour(self) -> None:
        # 23:00 UTC → 02:00 Cairo *next day*. The badge keeps it from
        # being misread as 2 AM on the same calendar day.
        parsed = ParsedUtcTime(hour=23, minute=0, matched="23 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        cairo_line = next(line for line in body.splitlines() if "Cairo" in line)
        assert "02:00" in cairo_line
        assert "next day" in cairo_line

    def test_prev_day_tag_for_early_utc_in_cst(self) -> None:
        # 05:00 UTC in January → 23:00 Chicago *previous day*. CST is
        # the only row in our set that can land on the previous day so
        # this exercises the negative-delta branch.
        parsed = ParsedUtcTime(hour=5, minute=0, matched="5 UTC")
        body = format_utc_card(parsed, today=date(2026, 1, 15))
        cst_line = next(
            line for line in body.splitlines() if line.lstrip().startswith("CST")
        )
        assert "23:00" in cst_line
        assert "prev day" in cst_line

    def test_no_next_day_tag_during_normal_hours(self) -> None:
        parsed = ParsedUtcTime(hour=10, minute=0, matched="10 UTC")
        body = format_utc_card(parsed, today=date(2026, 5, 6))
        assert "next day" not in body
        # CST at 10:00 UTC is the same calendar day.
        assert "prev day" not in body
