"""
Deterministic date/time helpers for the assistant.

Everything user-facing is expressed in the user's local timezone
(``settings.TIME_ZONE``, Europe/London). The model sends local dates
(``YYYY-MM-DD``) and times (``HH:MM``); this module validates them and turns
them into timezone-aware datetimes, handling GMT/BST transitions explicitly.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Callable, NamedTuple
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

Clock = Callable[[], datetime]

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_RO = ["luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică"]
MONTHS_RO = [
    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie",
]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


class DateTimeValidationError(ValueError):
    """Raised when a date/time value from the user or the model is invalid."""


class DateRange(NamedTuple):
    start: date
    end: date  # inclusive


def local_tz() -> ZoneInfo:
    return ZoneInfo(settings.TIME_ZONE)


def now_local(clock: Clock | None = None) -> datetime:
    """Current aware datetime in the user's timezone. ``clock`` allows deterministic tests."""
    current = clock() if clock else timezone.now()
    if timezone.is_naive(current):
        raise DateTimeValidationError("Clock must return a timezone-aware datetime.")
    return current.astimezone(local_tz())


def parse_local_date(value: str) -> date:
    if not isinstance(value, str) or not _DATE_RE.match(value.strip()):
        raise DateTimeValidationError(f"Invalid date {value!r}; expected YYYY-MM-DD.")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise DateTimeValidationError(f"Invalid calendar date {value!r}.") from exc


def parse_local_time(value: str) -> time:
    match = _TIME_RE.match(value.strip()) if isinstance(value, str) else None
    if not match:
        raise DateTimeValidationError(f"Invalid time {value!r}; expected HH:MM (24-hour).")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise DateTimeValidationError(f"Invalid time {value!r}; hour 0-23, minute 0-59.")
    return time(hour, minute)


def make_local_aware(day: date, at: time) -> datetime:
    """
    Combine a local date and time into an aware datetime in the user's timezone.

    - Non-existent local times (clocks go forward, e.g. 01:30 on the last Sunday
      of March) are rejected.
    - Ambiguous local times (clocks go back) resolve to the first occurrence
      (fold=0, i.e. BST).
    """
    tz = local_tz()
    candidate = datetime.combine(day, at).replace(tzinfo=tz, fold=0)
    round_trip = candidate.astimezone(dt_timezone.utc).astimezone(tz)
    if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
        raise DateTimeValidationError(
            f"{at.strftime('%H:%M')} on {day.isoformat()} does not exist in {tz.key} "
            "(clocks go forward). Please choose another time."
        )
    return candidate


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Aware [start, end) for a local calendar day (handles 23h/25h DST days)."""
    return make_local_aware(day, time(0, 0)), make_local_aware(day + timedelta(days=1), time(0, 0))


def local_range_bounds(start: date, end_inclusive: date) -> tuple[datetime, datetime]:
    if end_inclusive < start:
        raise DateTimeValidationError("End date is before start date.")
    return local_day_bounds(start)[0], local_day_bounds(end_inclusive)[1]


def to_local(value: datetime) -> datetime:
    return value.astimezone(local_tz())


def format_local(value: datetime | None, has_time: bool = True) -> str | None:
    """British-style human string, e.g. 'Wednesday 23/09/2026 10:00'."""
    if value is None:
        return None
    local = to_local(value)
    text = f"{WEEKDAYS_EN[local.weekday()]} {local.strftime('%d/%m/%Y')}"
    if has_time:
        text += f" {local.strftime('%H:%M')}"
    return text


# --- Relative day resolution -------------------------------------------------

def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower().strip())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_WEEKDAY_ALIASES = {
    **{name.lower(): i for i, name in enumerate(WEEKDAYS_EN)},
    **{_normalise(name): i for i, name in enumerate(WEEKDAYS_RO)},
    # Romanian articulated forms: "lunea", "vinerea", "sambata", ...
    "lunea": 0, "martea": 1, "miercurea": 2, "joia": 3, "vinerea": 4,
    "sambata": 5, "duminica": 6,
}


def next_weekday(today: date, weekday: int) -> date:
    """Soonest occurrence of ``weekday`` strictly after ``today`` (1-7 days ahead)."""
    delta = (weekday - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def weekday_next_week(today: date, weekday: int) -> date:
    """The given weekday within the following Monday-Sunday week."""
    next_monday = today + timedelta(days=7 - today.weekday())
    return next_monday + timedelta(days=weekday)


def next_week_range(today: date) -> DateRange:
    next_monday = today + timedelta(days=7 - today.weekday())
    return DateRange(next_monday, next_monday + timedelta(days=6))


def resolve_relative_day(expression: str, today: date) -> DateRange:
    """
    Resolve common Romanian/English relative day expressions.

    Conventions (also given to the model):
    - "vineri" / "Friday": the next Friday after today.
    - "vinerea viitoare" / "next Friday": the Friday of next week (Mon-Sun).
    - "săptămâna viitoare" / "next week": Monday-Sunday of next week.
    """
    text = _normalise(expression)
    single = {
        "azi": 0, "astazi": 0, "today": 0,
        "maine": 1, "tomorrow": 1,
        "poimaine": 2, "day after tomorrow": 2,
    }
    if text in single:
        day = today + timedelta(days=single[text])
        return DateRange(day, day)
    if text in {"saptamana viitoare", "next week"}:
        return next_week_range(today)

    words = text.split()
    if len(words) == 2 and words[0] == "next" and words[1] in _WEEKDAY_ALIASES:
        day = weekday_next_week(today, _WEEKDAY_ALIASES[words[1]])
        return DateRange(day, day)
    if len(words) == 2 and words[1] in {"viitoare", "viitor"} and words[0] in _WEEKDAY_ALIASES:
        day = weekday_next_week(today, _WEEKDAY_ALIASES[words[0]])
        return DateRange(day, day)
    if len(words) == 1 and text in _WEEKDAY_ALIASES:
        day = next_weekday(today, _WEEKDAY_ALIASES[text])
        return DateRange(day, day)
    raise DateTimeValidationError(f"Unrecognised relative day expression: {expression!r}.")


def build_calendar_context(now: datetime) -> str:
    """Text block giving the model the real current date/time and a two-week calendar."""
    local = to_local(now)
    today = local.date()
    offset = local.utcoffset() or timedelta(0)
    offset_hours = int(offset.total_seconds() // 3600)
    week = next_week_range(today)

    lines = [
        f"Current local date: {today.isoformat()} ({WEEKDAYS_EN[today.weekday()]} / "
        f"{WEEKDAYS_RO[today.weekday()]}), {today.day} {MONTHS_RO[today.month - 1]} {today.year}.",
        f"Current local time: {local.strftime('%H:%M')}.",
        f"Timezone: {local_tz().key}, currently {local.tzname()} (UTC{offset_hours:+d}).",
        f"Next week (Monday-Sunday): {week.start.isoformat()} to {week.end.isoformat()}.",
        "Upcoming days:",
    ]
    for offset_days in range(0, 15):
        day = today + timedelta(days=offset_days)
        label = {0: " (today / azi)", 1: " (tomorrow / mâine)", 2: " (poimâine)"}.get(offset_days, "")
        lines.append(
            f"- {day.isoformat()} {WEEKDAYS_EN[day.weekday()]} / {WEEKDAYS_RO[day.weekday()]}{label}"
        )
    return "\n".join(lines)
