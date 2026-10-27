"""MarketPilot dashboard (read-only).

    streamlit run apps/dashboard/app.py

Reads everything from the MarketPilot API (DASHBOARD_API_URL, default
http://127.0.0.1:8000) using GET requests only.
"""

import streamlit as st

from apps.dashboard import api_client
from apps.dashboard.views import (
    backtests,
    deployments,
    market_data,
    overview,
    paper,
    research,
    signals,
    system,
)

PAGES = {
    "Overview": overview,
    "PAPER account": paper,
    "Deployments": deployments,
    "Signals": signals,
    "Backtests": backtests,
    "Market data": market_data,
    "Research": research,
    "System health": system,
}

st.set_page_config(page_title="MarketPilot (PAPER)", page_icon="🧪", layout="wide")
st.sidebar.title("MarketPilot AI")
st.sidebar.caption("Research & PAPER trading only. No live orders.")
page = st.sidebar.radio("Page", list(PAGES))
if st.sidebar.button("Refresh"):
    st.rerun()

client = api_client.get_client()
PAGES[page].render(client)
