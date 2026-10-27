"""Shared page helpers: consistent labels and error display."""

from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from apps.dashboard.api_client import ApiResult

PAPER_BADGE = "🧪 **PAPER**: simulated account activity. No real orders, no real money."
BACKTEST_BADGE = (
    "📜 **BACKTEST**: hypothetical results on historical data with assumed costs. "
    "Not a prediction and not evidence of future profitability."
)
MARKET_BADGE = (
    "📈 **MARKET DATA**: bars as stored from the provider. Check the feed, adjustment "
    "and how old the latest bar is before relying on them."
)


def unwrap(result: ApiResult, what: str) -> Any | None:
    """Return the payload, or show the error and return None."""
    if result.ok:
        return result.data
    st.error(f"Could not load {what}: {result.error}")
    return None


def table(rows: list[dict[str, Any]], empty: str) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.caption(empty)


def equity_chart(frame: pd.DataFrame, columns: list[str], title: str) -> None:
    """Line chart whose y-axis fits the data (not forced to zero), so moves are visible."""
    if frame.empty:
        st.caption("No equity history yet.")
        return
    long = frame[columns].reset_index().melt("timestamp", var_name="series", value_name="value")
    chart = (
        alt.Chart(long, title=title)
        .mark_line()
        .encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("value:Q", title="USD", scale=alt.Scale(zero=False)),
            color=alt.Color("series:N", title=None),
            tooltip=["timestamp:T", "series:N", alt.Tooltip("value:Q", format=",.2f")],
        )
    )
    st.altair_chart(chart, use_container_width=True)


def drawdown_chart(drawdown: pd.Series) -> None:
    if drawdown.empty:
        return
    data = drawdown.rename("drawdown").reset_index()
    chart = (
        alt.Chart(data, title="Drawdown from running peak (%)")
        .mark_area(opacity=0.5)
        .encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("drawdown:Q", title="%"),
            tooltip=["timestamp:T", alt.Tooltip("drawdown:Q", format=".2f")],
        )
    )
    st.altair_chart(chart, use_container_width=True)
