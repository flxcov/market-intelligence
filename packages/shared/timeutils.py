"""UTC time helpers.

Timezone policy: every datetime inside the system is timezone-aware and in UTC.
Naive datetimes (no offset) are rejected rather than guessed, because silently
assuming a timezone is a classic source of off-by-hours bugs in market data.
"""

from datetime import UTC, date, datetime, time


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC; reject naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "datetime must include a UTC offset, e.g. '2024-01-02T14:30:00Z' "
            "(in a URL, write '+' as '%2B' or use 'Z')"
        )
    return value.astimezone(UTC)


def parse_utc_bound(value: object, *, is_end: bool) -> datetime:
    """Parse a range boundary given as a date, datetime, or ISO 8601 string.

    A date-only value (YYYY-MM-DD) means a UTC calendar day: the start bound is
    00:00:00Z and the end bound is 23:59:59Z of that day. This is a UTC day, not
    an exchange trading session; pass explicit datetimes for precise windows.
    """
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return _day_bound(value, is_end=is_end)
    if not isinstance(value, str):
        raise ValueError("expected an ISO 8601 date or datetime string")

    text = value.strip()
    if len(text) == 10:
        try:
            return _day_bound(date.fromisoformat(text), is_end=is_end)
        except ValueError:
            raise ValueError(f"invalid date {value!r}; expected YYYY-MM-DD") from None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"invalid datetime {value!r}; expected ISO 8601 such as "
            "'2024-01-02' or '2024-01-02T14:30:00Z'"
        ) from None
    return ensure_utc(parsed)


def _day_bound(day: date, *, is_end: bool) -> datetime:
    return datetime.combine(day, time(23, 59, 59) if is_end else time.min, tzinfo=UTC)
