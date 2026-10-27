"""Pure functions that shape API payloads for display. No Streamlit imports here.

Money arrives from the API as decimal strings; it is formatted from Decimal for
tables and converted to float ONLY for charts.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

SEVERITY_ICON = {"critical": "🔴", "warning": "🟠", "info": "🔵"}
CYCLE_ICON = {
    "succeeded": "✅",
    "partial": "🟠",
    "failed": "🔴",
    "interrupted": "⚠️",
    "running": "⏳",
}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def money(value: str | Decimal | None) -> str:
    if value is None:
        return "—"
    amount = Decimal(str(value))
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def pct(value: str | float | Decimal | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}%"


def age_days(timestamp: str | None, now: datetime) -> float | None:
    moment = parse_time(timestamp)
    return None if moment is None else round((now - moment).total_seconds() / 86400, 1)


def bar_end_age_days(bar_timestamp: str | None, now: datetime) -> float | None:
    """Days since a DAILY bar completed (its timestamp + 1 day); never negative."""
    started = age_days(bar_timestamp, now)
    return None if started is None else round(max(started - 1, 0.0), 1)


def equity_frame(points: list[dict[str, Any]], *, time_key: str = "bar_at") -> pd.DataFrame:
    """Equity (and cash / market value when present) indexed by time, as floats for charts.

    PAPER snapshots use `bar_at`; backtest equity curves use `timestamp`.
    """
    if not points:
        return pd.DataFrame(columns=["equity"])
    columns = [c for c in ("equity", "cash", "market_value") if c in points[0]]
    frame = pd.DataFrame(
        {
            "timestamp": [parse_time(p[time_key]) for p in points],
            **{c: [float(p[c]) for p in points] for c in columns},
        }
    )
    return frame.set_index("timestamp")


def drawdown_series(frame: pd.DataFrame) -> pd.Series:
    """Percentage below the running peak, at each point (0 at new highs)."""
    if frame.empty:
        return pd.Series(dtype=float)
    peak = frame["equity"].cummax()
    return (frame["equity"] - peak) / peak * 100


def position_rows(account: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Open positions. Shares are marked at the latest completed daily close; options
    (100-share contracts) at the bid of the latest stored quote."""
    rows = []
    for p in account.get("positions", []):
        if not p["quantity"]:
            continue
        is_option = p.get("multiplier", 1) != 1
        mark_at = p["mark_bar_at"]
        rows.append(
            {
                "symbol": p["symbol"],
                "type": f"option x{p['multiplier']}" if is_option else "shares",
                "quantity": p["quantity"],
                "average cost": money(p["average_cost"]),
                "mark": money(p["mark_price"]),
                "mark source": "latest quote bid" if is_option else "daily close",
                "marked at": (mark_at or "—")[:16].replace("T", " "),
                "mark age (days)": (
                    _rounded(age_days(mark_at, now))
                    if is_option
                    else bar_end_age_days(mark_at, now)
                ),
                "market value": money(p["market_value"]),
                "unrealized P&L": money(p["unrealized_pnl"]),
            }
        )
    return rows


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(max(value, 0.0), 1)


def cycle_rows(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for c in cycles:
        summary = c.get("summary") or {}
        ingestion_errors = sum(1 for i in summary.get("ingestion", []) if "error" in i)
        rows.append(
            {
                "cycle": c["id"],
                "status": f"{CYCLE_ICON.get(c['status'], '')} {c['status']}",
                "as of (UTC)": c["as_of"],
                "fills/events": len(summary.get("processing", {}).get("events", [])),
                "exits": len(summary.get("exits", [])),
                "entries": len(summary.get("entries", [])),
                "ingestion errors": ingestion_errors,
                "equity": money(summary.get("account", {}).get("equity")),
                "error": c.get("error_message") or "",
            }
        )
    return rows


def coverage_rows(coverage: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Stored series with freshness; age is measured from the END of the last daily bar."""
    rows = []
    for c in coverage:
        rows.append(
            {
                "symbol": c["symbol"],
                "timeframe": c["timeframe"],
                "feed": c["feed"],
                "adjustment": c["adjustment"],
                "bars": c["bars"],
                "first bar": c["first_bar"][:10],
                "last bar": c["last_bar"][:10],
                "days since last bar ended": bar_end_age_days(c["last_bar"], now),
                "last retrieved (UTC)": c["last_retrieved_at"][:16].replace("T", " "),
            }
        )
    return rows


def alert_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "severity": f"{SEVERITY_ICON.get(a['severity'], '')} {a['severity']}",
            "subject": a["subject"],
            "alert": a["title"],
            "detail": a["detail"],
            "since": (a.get("since") or "")[:16].replace("T", " "),
        }
        for a in report.get("alerts", [])
    ]


def metric_rows(metrics: dict[str, Any]) -> list[dict[str, str]]:
    """Backtest metrics as label/value pairs, with the conventions spelled out."""

    def number(key: str, fmt: str = "{:.2f}") -> str:
        value = metrics.get(key)
        return "—" if value is None else fmt.format(float(value))

    return [
        {"metric": "Total return", "value": pct(metrics.get("total_return_pct"))},
        {"metric": "CAGR (≥1 year only)", "value": pct(metrics.get("cagr_pct"))},
        {"metric": "Max drawdown (session closes)", "value": pct(metrics.get("max_drawdown_pct"))},
        {
            "metric": "Longest drawdown (sessions)",
            "value": str(metrics.get("max_drawdown_sessions")),
        },
        {
            "metric": "Volatility (annualized, √252)",
            "value": pct(metrics.get("volatility_annual_pct")),
        },
        {"metric": "Sharpe (√252, stated risk-free)", "value": number("sharpe")},
        {"metric": "Sortino", "value": number("sortino")},
        {"metric": "Closed trades", "value": str(metrics.get("trades"))},
        {"metric": "Win rate", "value": pct(metrics.get("win_rate_pct"), 1)},
        {"metric": "Profit factor", "value": number("profit_factor")},
        {"metric": "Expectancy per trade", "value": money(metrics.get("expectancy"))},
        {"metric": "Expectancy (R multiples)", "value": number("expectancy_r")},
        {
            "metric": "Exposure (% of sessions invested)",
            "value": pct(metrics.get("exposure_pct"), 1),
        },
        {"metric": "Fees", "value": money(metrics.get("total_fees"))},
        {"metric": "Turnover (traded / avg equity)", "value": number("turnover")},
    ]


def utc_now() -> datetime:
    return datetime.now(UTC)
