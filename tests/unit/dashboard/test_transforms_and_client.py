from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient

NOW = datetime(2024, 6, 12, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("100068.31", "$100,068.31"), ("-413.92", "-$413.92"), (Decimal("0.5"), "$0.50"), (None, "—")],
)
def test_money(value, expected: str) -> None:
    assert transforms.money(value) == expected


def test_pct_and_age() -> None:
    assert (transforms.pct(12.345), transforms.pct(None), transforms.pct("1", 1)) == (
        "12.35%",
        "—",
        "1.0%",
    )
    assert transforms.age_days("2024-06-10T10:00:00Z", NOW) == 2.0
    assert transforms.age_days(None, NOW) is None


def test_equity_frame_and_drawdown() -> None:
    snapshots = [
        {"bar_at": "2024-01-02T05:00:00Z", "cash": "100", "market_value": "0", "equity": "100"},
        {"bar_at": "2024-01-03T05:00:00Z", "cash": "50", "market_value": "70", "equity": "120"},
        {"bar_at": "2024-01-04T05:00:00Z", "cash": "50", "market_value": "40", "equity": "90"},
    ]
    frame = transforms.equity_frame(snapshots)
    assert list(frame.columns) == ["equity", "cash", "market_value"]
    assert frame["equity"].tolist() == [100.0, 120.0, 90.0]
    assert transforms.drawdown_series(frame).round(4).tolist() == [0.0, 0.0, -25.0]

    curve = [{"timestamp": "2024-01-02T05:00:00Z", "equity": "1", "market_value": "0"}]
    assert list(transforms.equity_frame(curve, time_key="timestamp").columns) == [
        "equity",
        "market_value",
    ]
    assert transforms.equity_frame([]).empty
    assert transforms.drawdown_series(transforms.equity_frame([])).empty


def test_position_rows_skip_closed_positions_and_show_mark_age() -> None:
    account = {
        "positions": [
            {
                "symbol": "AAA",
                "quantity": 10,
                "average_cost": "10.0000",
                "mark_price": "11",
                "mark_bar_at": "2024-06-11T04:00:00Z",
                "market_value": "110",
                "unrealized_pnl": "10",
            },
            {
                "symbol": "OLD",
                "quantity": 0,
                "average_cost": None,
                "mark_price": None,
                "mark_bar_at": None,
                "market_value": "0",
                "unrealized_pnl": "0",
            },
        ]
    }
    (row,) = transforms.position_rows(account, NOW)
    # Bar of June 11 (04:00Z) ended June 12 04:00Z; now is June 12 10:00Z -> 0.25 days.
    assert (row["symbol"], row["type"], row["mark source"], row["mark age (days)"]) == (
        "AAA",
        "shares",
        "daily close",
        0.2,
    )
    assert row["unrealized P&L"] == "$10.00"


def test_option_positions_are_labelled_and_aged_from_the_quote_time() -> None:
    account = {
        "positions": [
            {
                "symbol": "AAA240621C00010000",
                "quantity": 2,
                "multiplier": 100,
                "average_cost": "1.2500",
                "mark_price": "1.10",
                "mark_bar_at": "2024-06-12T04:00:00Z",
                "market_value": "220",
                "unrealized_pnl": "-30",
            }
        ]
    }
    (row,) = transforms.position_rows(account, NOW)
    # The quote is 6 hours old (no extra day: a quote is a moment, not a daily bar).
    assert (row["type"], row["mark source"], row["mark age (days)"]) == (
        "option x100",
        "latest quote bid",
        0.2,
    )
    assert row["marked at"] == "2024-06-12 04:00"


def test_coverage_age_counts_from_the_end_of_the_bar() -> None:
    (row,) = transforms.coverage_rows(
        [
            {
                "symbol": "AAA",
                "timeframe": "1Day",
                "feed": "iex",
                "adjustment": "raw",
                "bars": 5,
                "first_bar": "2024-06-01T04:00:00Z",
                "last_bar": "2024-06-10T04:00:00Z",
                "last_retrieved_at": "2024-06-11T06:00:00Z",
            }
        ],
        NOW,
    )
    # Bar of June 10 ends June 11 04:00Z; now is June 12 10:00Z -> 1.25 days.
    assert row["days since last bar ended"] == 1.2
    assert row["last retrieved (UTC)"] == "2024-06-11 06:00"


def test_cycle_and_alert_rows() -> None:
    (row,) = transforms.cycle_rows(
        [
            {
                "id": 7,
                "status": "partial",
                "as_of": "x",
                "error_message": None,
                "summary": {
                    "ingestion": [{"error": "down"}, {"inserted": 1}],
                    "processing": {"events": [{}, {}]},
                    "exits": [{}],
                    "entries": [],
                    "account": {"equity": "5"},
                },
            }
        ]
    )
    assert (row["status"], row["fills/events"], row["exits"], row["ingestion errors"]) == (
        "🟠 partial",
        2,
        1,
        1,
    )
    (alert,) = transforms.alert_rows(
        {"alerts": [{"severity": "critical", "subject": "s", "title": "t", "detail": "d"}]}
    )
    assert alert["severity"] == "🔴 critical"


def test_metric_rows_handle_missing_values() -> None:
    rows = {r["metric"]: r["value"] for r in transforms.metric_rows({"trades": 0})}
    assert rows["Sharpe (√252, stated risk-free)"] == "—"
    assert rows["CAGR (≥1 year only)"] == "—"
    assert rows["Closed trades"] == "0"


# --- client ---------------------------------------------------------------------------------


def client_for(handler) -> DashboardClient:
    return DashboardClient(
        httpx.Client(base_url="http://api", transport=httpx.MockTransport(handler))
    )


def test_client_returns_data() -> None:
    result = client_for(lambda r: httpx.Response(200, json={"status": "ok"})).health()
    assert (result.ok, result.data) == (True, {"status": "ok"})


def test_client_turns_api_errors_into_messages() -> None:
    body = {"error": {"type": "database_not_configured", "message": "set DATABASE_URL"}}
    result = client_for(lambda r: httpx.Response(503, json=body)).accounts()
    assert result.error == "database_not_configured: set DATABASE_URL"


def test_client_never_raises() -> None:
    def unreachable(request):
        raise httpx.ConnectError("refused")

    assert "not reachable" in client_for(unreachable).alerts().error
    assert "non-JSON" in client_for(lambda r: httpx.Response(502, text="<html>")).alerts().error


def test_client_drops_empty_params() -> None:
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    client_for(handler).orders(3)
    assert seen[0].method == "GET"
    assert dict(seen[0].url.params) == {"limit": "200"}
