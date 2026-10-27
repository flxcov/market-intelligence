"""Provider-independent integrity checks for bar data."""

from collections.abc import Iterable
from datetime import datetime

from packages.market_data.errors import DataValidationError
from packages.market_data.models import Bar


def normalize_bars(
    bars: Iterable[Bar], *, start: datetime, end: datetime, provider: str | None = None
) -> tuple[list[Bar], int]:
    """Sort bars chronologically and enforce integrity rules.

    - Bars outside [start, end] are rejected: returning them could leak data
      the caller did not ask for (a look-ahead risk in backtests).
    - Exact duplicates (same timestamp, same values) are dropped and counted.
    - Conflicting duplicates (same timestamp, different values) are rejected,
      because there is no safe way to choose between them.

    Returns (sorted_bars, duplicates_removed).
    """
    by_timestamp: dict[datetime, Bar] = {}
    duplicates = 0
    for bar in bars:
        if not start <= bar.timestamp <= end:
            raise DataValidationError(
                f"bar at {bar.timestamp.isoformat()} is outside the requested range "
                f"{start.isoformat()} - {end.isoformat()}",
                provider=provider,
            )
        existing = by_timestamp.get(bar.timestamp)
        if existing is None:
            by_timestamp[bar.timestamp] = bar
        elif existing == bar:
            duplicates += 1
        else:
            raise DataValidationError(
                f"conflicting bars for timestamp {bar.timestamp.isoformat()}",
                provider=provider,
            )
    return [by_timestamp[ts] for ts in sorted(by_timestamp)], duplicates
