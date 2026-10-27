import streamlit as st

from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import unwrap


def render(client: DashboardClient) -> None:
    st.header("System health")
    st.caption(f"API: {client.base_url}")
    health = unwrap(client.health(), "health")
    if health is None:
        st.info("Start the API: `uvicorn apps.marketpilot.main:create_app --factory`")
        return
    (st.success if health["status"] == "ok" else st.warning)(f"Service status: {health['status']}")
    cols = st.columns(4)
    cols[0].metric("Trading mode", health["trading_mode"].upper())
    cols[1].metric("Live trading", "disabled" if not health["live_trading_enabled"] else "ENABLED")
    cols[2].metric("Writes via API", "enabled" if health["writes_enabled"] else "disabled")
    cols[3].metric("Schema", health["database"]["schema_revision"] or "—")
    market = health["market_data"]
    database = health["database"]
    st.write(
        f"**Market data** ({market['provider']}): "
        f"{'configured' if market['configured'] else 'NOT configured'}"
        f"{' · ' + market['detail'] if market.get('detail') else ''}"
    )
    st.write(
        f"**Database**: {'reachable' if database['reachable'] else 'NOT reachable'}"
        f"{' · ' + database['detail'] if database.get('detail') else ''}"
    )
    st.caption(
        "This dashboard is read-only: it only sends GET requests and never holds the write "
        "token. Halt, pause or shut down via the API or CLI."
    )
    with st.expander("Raw health response"):
        st.json(health)
