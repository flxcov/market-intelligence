import streamlit as st

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import MARKET_BADGE, table, unwrap


def render(client: DashboardClient) -> None:
    st.header("Market data")
    st.caption(MARKET_BADGE)
    coverage = unwrap(client.coverage(), "stored data coverage")
    if coverage is not None:
        rows = transforms.coverage_rows(coverage, transforms.utc_now())
        if any(r["feed"] == "iex" for r in rows):
            st.info(
                "IEX feed: trades from the IEX exchange only, not the consolidated tape. "
                "Volumes are much lower than consolidated volume and some prices differ."
            )
        stale = [r for r in rows if (r["days since last bar ended"] or 0) > 4]
        if stale:
            st.warning(f"{len(stale)} series have no bar newer than 4 days (calendar).")
        table(rows, "No bars stored yet. Ingest with POST /market/bars/ingest.")

    st.subheader("Recent ingestion runs")
    runs = unwrap(client.ingestion_runs(), "ingestion runs")
    if runs is not None:
        table(
            [
                {
                    "id": r["id"],
                    "status": ("🔴 " if r["status"] == "failed" else "") + r["status"],
                    "symbol": r["symbol"],
                    "adjustment": r["adjustment"],
                    "range": f"{r['range_start'][:10]} → {r['range_end'][:10]}",
                    "inserted": r["inserted"],
                    "updated": r["updated"],
                    "unchanged": r["unchanged"],
                    "started (UTC)": r["started_at"][:16].replace("T", " "),
                    "error": r["error_message"] or "",
                }
                for r in runs
            ],
            "No ingestion runs yet.",
        )
