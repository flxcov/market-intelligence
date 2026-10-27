"""SYNTHETIC payloads shaped like Alpaca's option snapshots response.

Every number here is invented for tests. These are NOT real quotes and must
never be displayed, stored, or analyzed as if they were.
"""

import json
from typing import Any


def synthetic_snapshot(
    *,
    t: str = "2024-06-03T15:00:00Z",
    bp: float = 2.4,
    ap: float = 2.5,
    bs: int = 10,
    as_: int = 12,
    trade: bool = True,
    greeks: bool = True,
    quote: bool = True,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    if quote:
        snapshot["latestQuote"] = {
            "t": t,
            "bp": bp,
            "bs": bs,
            "ap": ap,
            "as": as_,
            "bx": "C",
            "ax": "C",
            "c": "A",
        }
    if trade:
        snapshot["latestTrade"] = {"t": t, "p": 2.45, "s": 1, "x": "C", "c": "I"}
    if greeks:
        snapshot["impliedVolatility"] = 0.2512
        snapshot["greeks"] = {
            "delta": 0.52,
            "gamma": 0.04,
            "theta": -0.05,
            "vega": 0.11,
            "rho": 0.02,
        }
    return snapshot


def snapshots_page(snapshots: dict[str, Any], next_page_token: str | None = None) -> str:
    return json.dumps({"snapshots": snapshots, "next_page_token": next_page_token})
