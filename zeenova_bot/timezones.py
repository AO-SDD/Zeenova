"""Detect ``UTC`` / ``GMT`` time references in chat messages and render
local-time conversions for a small set of key timezones.

The trigger is intentionally tight: a message has to carry an explicit
``UTC`` (or ``GMT``) marker right after a recognised time format. This
keeps the bot from chiming in on plain hour numbers like
"I have 13 messages" while still catching the common ways traders
write times in announcements ("listing at 13:00 UTC", "tge 1pm UTC",
"FOMC 14 GMT").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo

# Timezones shown in the reply, in display order. Cairo (the team's
# home market) comes first, then the major regions traders coordinate
# against: Central Europe, Moscow, and US Central. Labels use the
# common IANA / market abbreviations rather than city + flag so the
# card stays compact and free of country-specific iconography.
TIMEZONES: tuple[tuple[str, str], ...] = (
    ("Cairo", "Africa/Cairo"),
    ("CET", "Europe/Berlin"),
    ("MSK", "Europe/Moscow"),
    ("CST", "America/Chicago"),
)


@dataclass(frozen=True, slots=True)
class ParsedUtcTime:
    """Normalised 24-hour representation of a detected UTC time."""

    hour: int  # 0..23
    minute: int  # 0..59
    matched: str  # original substring, e.g. "1:30 PM UTC"


# 12-hour and 24-hour formats live in separate patterns so each can
# stay anchored on its own discriminator (``am``/``pm`` vs the
# unambiguous 24-hour ranges) without an explosion of optional groups.
# All patterns are compiled case-insensitive so the ``UTC``/``GMT``
# and ``AM``/``PM`` markers can be written in any case.
_TZ = r"(?:UTC|GMT)"

# Order matters: first match wins. The colon form must come before
# the lone-hour form so "13:00 UTC" doesn't get parsed as just "13".
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1:30 PM UTC, 12:00 am UTC
    re.compile(
        rf"\b(?P<h>1[0-2]|[1-9]):(?P<m>[0-5][0-9])\s*(?P<ampm>am|pm)\s*{_TZ}\b",
        re.IGNORECASE,
    ),
    # 1 PM UTC, 12am UTC
    re.compile(
        rf"\b(?P<h>1[0-2]|[1-9])\s*(?P<ampm>am|pm)\s*{_TZ}\b",
        re.IGNORECASE,
    ),
    # 13:30 UTC, 09:00 GMT, 0:30 UTC
    re.compile(
        rf"\b(?P<h>2[0-3]|[01]?[0-9]):(?P<m>[0-5][0-9])\s*{_TZ}\b",
        re.IGNORECASE,
    ),
    # 1300 UTC (4-digit military). Constrained to 0000..2359 by the
    # alternation so "12345 UTC" never matches.
    re.compile(
        rf"\b(?P<h>[01][0-9]|2[0-3])(?P<m>[0-5][0-9])\s*{_TZ}\b",
        re.IGNORECASE,
    ),
    # 13 UTC (lone hour). Must be a standalone token — no leading digit
    # boundary picks up part of a number like "213 UTC".
    re.compile(
        rf"\b(?P<h>2[0-3]|[01]?[0-9])\s*{_TZ}\b",
        re.IGNORECASE,
    ),
)


def detect_utc_time(text: str) -> ParsedUtcTime | None:
    """Return the first UTC time reference found in ``text``, or
    ``None`` when nothing recognisable is present.

    Patterns are tried in declaration order so explicit-minute forms
    win over the lone-hour form ("13:30 UTC" parses to ``13:30``,
    never to ``13:00``).
    """
    for pattern in _PATTERNS:
        m = pattern.search(text)
        if m is None:
            continue
        groups = m.groupdict()
        try:
            hour = int(groups["h"])
        except (KeyError, TypeError, ValueError):
            continue
        minute_raw = groups.get("m")
        minute = int(minute_raw) if minute_raw is not None else 0
        ampm = (groups.get("ampm") or "").lower()
        if ampm == "am" and hour == 12:
            hour = 0
        elif ampm == "pm" and hour < 12:
            hour += 12
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            continue
        return ParsedUtcTime(hour=hour, minute=minute, matched=m.group(0))
    return None


def format_utc_card(parsed: ParsedUtcTime, today: date | None = None) -> str:
    """Build the HTML reply body for a detected UTC time.

    ``today`` lets callers pin the reference UTC date for
    deterministic tests; defaults to the current UTC date in
    production. DST transitions for the target timezones are handled
    by ``zoneinfo`` against the system tzdata.
    """
    base_date = today if today is not None else datetime.now(UTC).date()
    base = datetime.combine(
        base_date, time(parsed.hour, parsed.minute), tzinfo=UTC
    )
    lines: list[str] = [
        f"🕐 <b>{parsed.hour:02d}:{parsed.minute:02d} UTC</b>",
        "",
    ]
    for label, tz_name in TIMEZONES:
        try:
            local = base.astimezone(ZoneInfo(tz_name))
        except Exception:
            # Missing tzdata → silently skip the row rather than
            # poisoning the whole reply. Unlikely in practice (the
            # base image ships tzdata) but cheap to guard.
            continue
        delta = local.date() - base_date
        if delta == timedelta(days=1):
            day_tag = " <i>(next day)</i>"
        elif delta == timedelta(days=-1):
            day_tag = " <i>(prev day)</i>"
        else:
            day_tag = ""
        lines.append(
            f"{label}  <code>{local:%H:%M}</code>{day_tag}"
        )
    lines.append("")
    lines.append(
        f"<i>Detected:</i> <code>{escape(parsed.matched)}</code>"
    )
    return "\n".join(lines)
