import streamlit as st

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import PAPER_BADGE, table, unwrap


def render(client: DashboardClient) -> None:
    st.header("Overview")

    report = unwrap(client.alerts(), "alerts")
    if report is not None:
        counts = report["counts"]
        left, middle, right = st.columns(3)
        left.metric("🔴 Critical", counts.get("critical", 0))
        middle.metric("🟠 Warnings", counts.get("warning", 0))
        right.metric("🔵 Info", counts.get("info", 0))
        table(transforms.alert_rows(report), "No alerts. All checks are quiet.")

    st.subheader("PAPER accounts")
    st.caption(PAPER_BADGE)
    accounts = unwrap(client.accounts(), "accounts")
    if accounts is not None:
        rows = []
        for summary in accounts:
            view = client.account(summary["id"])
            data = view.data if view.ok else {}
            rows.append(
                {
                    "account": f"{summary['id']} · {summary['name']}",
                    "status": summary["status"],
                    "equity": transforms.money(data.get("equity")),
                    "cash": transforms.money(summary["cash"]),
                    "return": transforms.pct(data.get("total_return_pct")),
                    "drawdown": transforms.pct(data.get("drawdown_pct")),
                    "open positions": sum(1 for p in data.get("positions", []) if p["quantity"]),
                    "processed through": (summary["processed_through"] or "—")[:10],
                    "note": "" if view.ok else view.error,
                }
            )
        table(rows, "No PAPER accounts yet.")

    st.subheader("Deployments")
    deployments = unwrap(client.deployments(), "deployments")
    if deployments is not None:
        rows = []
        for d in deployments:
            cycles = client.cycles(d["id"])
            last = cycles.data[0] if cycles.ok and cycles.data else None
            rows.append(
                {
                    "deployment": f"{d['id']} · {d['name']}",
                    "status": d["status"],
                    "symbols": ", ".join(d["symbols"]),
                    "last cycle": (
                        f"{transforms.CYCLE_ICON.get(last['status'], '')} {last['status']}"
                        if last
                        else "never run"
                    ),
                    "last cycle at (UTC)": (
                        last["started_at"][:16].replace("T", " ") if last else "—"
                    ),
                }
            )
        table(rows, "No deployments yet.")
