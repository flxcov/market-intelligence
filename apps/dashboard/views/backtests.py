import streamlit as st

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import (
    BACKTEST_BADGE,
    drawdown_chart,
    equity_chart,
    table,
    unwrap,
)


def render(client: DashboardClient) -> None:
    st.header("Backtests")
    st.caption(BACKTEST_BADGE)
    summaries = unwrap(client.backtests(), "backtests")
    if not summaries:
        if summaries is not None:
            st.info("No backtests yet. Run one with POST /backtests.")
        return
    labels = {
        f"{b['id']} · {', '.join(b['symbols'])} · {b['start'][:10]} → {b['end'][:10]}": b["id"]
        for b in summaries
    }
    report = unwrap(client.backtest(labels[st.selectbox("Backtest", list(labels))]), "backtest")
    if report is None:
        return

    for warning in report["warnings"]:
        st.warning(warning)

    metrics = report["metrics"]
    cols = st.columns(4)
    cols[0].metric("Total return", transforms.pct(metrics["total_return_pct"]))
    cols[1].metric("Max drawdown", transforms.pct(metrics["max_drawdown_pct"]))
    cols[2].metric("Closed trades", metrics["trades"])
    benchmark = report.get("benchmark")
    cols[3].metric(
        f"vs {benchmark['symbol']} buy & hold" if benchmark else "Benchmark",
        transforms.pct(benchmark["strategy_excess_return_pct"]) if benchmark else "n/a",
    )

    frame = transforms.equity_frame(report["equity_curve"], time_key="timestamp")
    equity_chart(frame, ["equity"], "Equity at each session close")
    drawdown_chart(transforms.drawdown_series(frame))

    left, right = st.columns(2)
    with left:
        st.subheader("Metrics")
        table(transforms.metric_rows(metrics), "")
    with right:
        if report.get("in_sample") and report.get("out_of_sample"):
            st.subheader("In-sample vs out-of-sample")
            table(
                [
                    {"metric": i["metric"], "in-sample": i["value"], "out-of-sample": o["value"]}
                    for i, o in zip(
                        transforms.metric_rows(report["in_sample"]),
                        transforms.metric_rows(report["out_of_sample"]),
                        strict=True,
                    )
                ],
                "",
            )
        if benchmark:
            st.subheader(f"Benchmark: {benchmark['symbol']}")
            st.write(
                f"Buy & hold return {transforms.pct(benchmark['total_return_pct'])}, max "
                f"drawdown {transforms.pct(benchmark['max_drawdown_pct'])}. {benchmark['note']}"
            )

    st.subheader("Trades")
    table(report["trades"], "No closed trades.")
    if report["open_trades"]:
        st.caption("Positions still open at the end (marked, not closed):")
        table(report["open_trades"], "")
    with st.expander("Reproducibility"):
        st.write(
            f"Strategy {report['strategy']} {report['strategy_version']} · parameters hash "
            f"`{report['parameters_hash']}` · data fingerprint `{report['data_fingerprint']}` "
            f"({report['bars_used']} bars)."
        )
        st.json(report["request"])
        st.json(report["signal_stats"])
    st.caption(report["disclaimer"])
