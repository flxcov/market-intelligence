"""SYNTHETIC payloads shaped like Alpaca Market Data v2 bar responses.

Every number here is invented for tests. These are NOT real market data and
must never be displayed, stored, or analyzed as if they were.
"""

import json
from typing import Any


def synthetic_bar(
    t: str = "2024-01-02T05:00:00Z",
    o: float = 100.0,
    h: float = 101.5,
    low: float = 99.25,
    c: float = 100.75,
    v: int = 1000,
    n: int | None = 10,
    vw: float | None = 100.4,
) -> dict[str, Any]:
    bar: dict[str, Any] = {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v}
    if n is not None:
        bar["n"] = n
    if vw is not None:
        bar["vw"] = vw
    return bar


def bars_page(symbol: str, bars: list[dict[str, Any]], next_page_token: str | None = None) -> str:
    """Return response text, as the provider would send it."""
    return json.dumps({"bars": {symbol: bars}, "next_page_token": next_page_token})


SYNTHETIC_DAILY_BARS = [
    synthetic_bar(t="2024-01-02T05:00:00Z", o=100.0, h=101.5, low=99.25, c=100.75),
    synthetic_bar(t="2024-01-03T05:00:00Z", o=100.75, h=102.1, low=100.5, c=101.9),
    synthetic_bar(t="2024-01-04T05:00:00Z", o=101.9, h=101.95, low=99.8, c=100.1),
]
