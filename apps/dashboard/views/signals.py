import streamlit as st

from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import table, unwrap


def render(client: DashboardClient) -> None:
    st.header("Signals")
    st.caption(
        "Signals are strategy output for PAPER trading and research, not recommendations. "
        "Score components are descriptive measurements, NOT probabilities."
    )
    signals = unwrap(client.signals(), "signals")
    if not signals:
        if signals is not None:
            st.info("No signals recorded yet.")
        return
    table(
        [
            {
                "id": s["id"],
                "symbol": s["symbol"],
                "signal bar": s["signal_bar_at"][:10],
                "strategy": f"{s['strategy']} {s['strategy_version']}",
                "reference": s["reference_price"],
                "stop": s["stop_price"],
                "risk/share": s["risk_per_share"],
                "data retrieved (UTC)": (s["data_retrieved_at"] or "—")[:16].replace("T", " "),
            }
            for s in signals
        ],
        "",
    )
    labels = {f"{s['id']} · {s['symbol']} · {s['signal_bar_at'][:10]}": s for s in signals}
    signal = labels[st.selectbox("Signal detail", list(labels))]

    st.subheader("Rule checks (all must pass)")
    table(
        [
            {"rule": c["rule"], "passed": "✅" if c["passed"] else "❌", "detail": c["detail"]}
            for c in signal["rule_checks"]
        ],
        "",
    )
    left, right = st.columns(2)
    with left:
        st.subheader("Entry plan")
        st.write(
            f"Market buy at the **next session's open** (valid that session only).  \n"
            f"Cancel if the open is below **{signal['cancel_if_open_below']}** "
            f"or above **{signal['cancel_if_open_above']}**."
        )
    with right:
        st.subheader("Exit plan")
        st.write(
            f"Initial stop **{signal['stop_price']}** · trailing exit below the lowest low of "
            f"the previous **{signal['trailing_exit_lookback_bars']}** bars · time stop after "
            f"**{signal['time_stop_bars']}** bars."
        )
    st.subheader("Score components (not probabilities)")
    st.json(signal["score_components"])
    st.caption(
        f"Parameters hash `{signal['parameters_hash']}` · feed {signal['feed']}, "
        f"{signal['adjustment']}-adjusted prices."
    )
