import streamlit as st

from apps.dashboard import transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import PAPER_BADGE, table, unwrap


def render(client: DashboardClient) -> None:
    st.header("Forward trading deployments")
    st.caption(PAPER_BADGE)
    deployments = unwrap(client.deployments(), "deployments")
    if not deployments:
        if deployments is not None:
            st.info("No deployments yet. Create one with POST /paper/deployments.")
        return
    labels = {f"{d['id']} · {d['name']} ({d['status']})": d for d in deployments}
    deployment = labels[st.selectbox("Deployment", list(labels))]

    cols = st.columns(4)
    cols[0].metric("Status", deployment["status"])
    cols[1].metric("Account", deployment["account_id"])
    cols[2].metric("Strategy", f"{deployment['strategy']} {deployment['strategy_version']}")
    cols[3].metric("Signal prices", deployment["signal_adjustment"])
    st.caption(
        f"Symbols: {', '.join(deployment['symbols'])} · parameters hash "
        f"`{deployment['parameters_hash'][:12]}…` · last cycle "
        f"{(deployment['last_cycle_at'] or 'never')[:16].replace('T', ' ')}"
    )
    if deployment["status"] == "paused":
        st.info("Paused: exits are still managed; no new entries.")
    if deployment["status"] == "stopped":
        st.warning("Stopped (emergency shutdown): cycles only process pending fills.")

    cycles = unwrap(client.cycles(deployment["id"]), "cycles")
    if cycles is None:
        return
    st.subheader("Recent cycles")
    table(transforms.cycle_rows(cycles), "No cycles yet. Is the scheduler running?")
    for cycle in cycles[:10]:
        with st.expander(f"Cycle {cycle['id']} · {cycle['status']} · as of {cycle['as_of'][:16]}"):
            st.json(cycle["summary"])
    with st.expander("Strategy parameters"):
        st.json(deployment["parameters"])
