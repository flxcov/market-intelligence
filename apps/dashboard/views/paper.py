import streamlit as st

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import (
    PAPER_BADGE,
    drawdown_chart,
    equity_chart,
    table,
    unwrap,
)


def render(client: DashboardClient) -> None:
    st.header("PAPER account")
    st.caption(PAPER_BADGE)
    accounts = unwrap(client.accounts(), "accounts")
    if not accounts:
        if accounts is not None:
            st.info("No PAPER accounts yet. Create one with POST /paper/accounts.")
        return
    labels = {f"{a['id']} · {a['name']}": a["id"] for a in accounts}
    account_id = labels[st.selectbox("Account", list(labels))]

    account = unwrap(client.account(account_id), "the account")
    if account is None:
        return
    now = transforms.utc_now()
    if account["status"] == "halted":
        st.warning("This account is HALTED: new entries are blocked; protective stops stay active.")
    for notice in account.get("notices", []):
        st.warning(notice)

    cols = st.columns(4)
    cols[0].metric("Equity", transforms.money(account["equity"]))
    cols[1].metric("Cash", transforms.money(account["cash"]))
    cols[2].metric("Total return", transforms.pct(account["total_return_pct"]))
    cols[3].metric("Drawdown from peak", transforms.pct(account["drawdown_pct"]))
    cols = st.columns(4)
    cols[0].metric("Realized P&L", transforms.money(account["realized_pnl"]))
    cols[1].metric("Unrealized P&L", transforms.money(account["unrealized_pnl"]))
    cols[2].metric("Fees", transforms.money(account["fees"]))
    cols[3].metric("Working orders", account["working_orders"])
    option_feed = account.get("option_settings", {}).get("quote_feed", "?")
    st.caption(
        f"Valued at {account['valued_at'][:16].replace('T', ' ')} UTC: shares at the latest "
        f"COMPLETED daily close on the {account['feed']} feed (raw prices), options at the bid "
        f"of the latest stored {option_feed} quote. Processed through "
        f"{(account['processed_through'] or 'nothing yet')[:10]}."
    )

    snapshots = unwrap(client.equity(account_id), "equity history")
    if snapshots:
        frame = transforms.equity_frame(snapshots)
        equity_chart(frame, ["equity", "cash"], "Equity and cash at each processed session close")
        drawdown_chart(transforms.drawdown_series(frame))

    st.subheader("Open positions")
    table(transforms.position_rows(account, now), "No open positions.")

    st.subheader("Working orders")
    working = unwrap(client.orders(account_id, status="working"), "working orders")
    if working is not None:
        table(
            [
                {
                    "id": o["id"],
                    "symbol": o["symbol"],
                    "asset": o.get("asset_class", "equity"),
                    "side": o["side"],
                    "type": o["order_type"],
                    "tif": o["time_in_force"],
                    "qty": o["quantity"],
                    "stop": o["stop_price"] or o["protective_stop_price"] or "",
                    "limit": o["limit_price"] or "",
                    "submitted (UTC)": o["submitted_at"][:16].replace("T", " "),
                    "deployment": o["deployment_id"] or "",
                }
                for o in working
            ],
            "No working orders.",
        )

    st.subheader("Recent fills")
    fills = unwrap(client.fills(account_id), "fills")
    if fills is not None:
        table(
            [
                {
                    "session": f["bar_timestamp"][:10],
                    "symbol": f["symbol"],
                    "side": f["side"],
                    "qty": f["quantity"],
                    "x": f.get("multiplier", 1),
                    "base price": f["base_price"],
                    "fill price (after costs)": f["price"],
                    "commission": f["commission"],
                    "mode": f["mode"],
                }
                for f in fills
            ],
            "No fills yet.",
        )

    with st.expander("Books: reconciliation and risk limits"):
        reconcile = unwrap(client.reconcile(account_id), "reconciliation")
        if reconcile is not None:
            (st.success if reconcile["balanced"] else st.error)(
                "Books balance." if reconcile["balanced"] else "Books do NOT reconcile."
            )
            table(reconcile["checks"], "")
        st.json(account["risk_limits"])
        st.json(account["cost_model"])
