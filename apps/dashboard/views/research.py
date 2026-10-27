import streamlit as st

from apps.dashboard import api_client, transforms
from apps.dashboard.api_client import DashboardClient
from apps.dashboard.views.common import table, unwrap

RESEARCH_BADGE = (
    "🔎 **RESEARCH (AlphaScout)**: evidence-based research support, separate from trading. "
    "Scenarios are hypothetical; scores are heuristics, not probabilities. Not advice."
)
VERDICT_ICONS = {
    "rejected": "⛔",
    "insufficient_evidence": "❔",
    "no_2x_path": "⏸",
    "watchlist": "👀",
}


def render(_: DashboardClient) -> None:
    st.header("Research")
    st.caption(RESEARCH_BADGE)
    client = api_client.get_research_client()
    reports = unwrap(client.get("/reports", limit=100), "research reports")
    if not reports:
        if reports is not None:
            st.info("No research reports yet. Create one with POST /research/{symbol}.")
        return
    labels = {
        f"{r['id']} · {r['symbol']} v{r['version']} · {r['verdict']} · {r['as_of'][:10]}": r["id"]
        for r in reports
    }
    stored = unwrap(
        client.get(f"/reports/{labels[st.selectbox('Report', list(labels))]}"), "report"
    )
    if stored is None:
        return
    report = stored["report"]
    st.subheader(
        f"{VERDICT_ICONS.get(report['verdict'], '')} {report['company']} ({report['symbol']})"
    )
    st.write(f"**Verdict: {report['verdict']}**: " + "; ".join(report["verdict_reasons"]))
    cols = st.columns(4)
    screen = report["screen"]
    cols[0].metric("Reference price", transforms.money(screen["price"]))
    cols[1].metric("Market cap", transforms.money(screen["market_cap"]))
    cols[2].metric("Heuristic score", f"{report['score_total']}/100")
    cols[3].metric("Data confidence", report["data_confidence"])
    st.caption(
        f"As of {report['as_of'][:16].replace('T', ' ')} UTC · price bar "
        f"{(screen['price_bar_at'] or 'none')[:10]} · {report['score_note']}"
    )
    st.write(report["thesis"])
    st.write(report["strongest_bear_case"])

    valuation = report.get("valuation")
    if valuation:
        st.subheader(f"Scenarios ({valuation['horizon_months']} months, {valuation['method']})")
        table(
            [
                {
                    "scenario": s["assumptions"]["name"],
                    "revenue CAGR": s["assumptions"]["revenue_cagr"],
                    "EV/Sales": s["assumptions"]["ev_to_sales"],
                    "dilution/yr": s["assumptions"]["annual_dilution"],
                    "implied price": s["implied_price"],
                    "return %": s["return_pct"],
                    "reaches 2x": s["reaches_2x"],
                    "flags": "; ".join(s["plausibility_flags"]),
                }
                for s in valuation["scenarios"]
            ],
            "",
        )
        st.caption(
            f"Assumptions: {valuation['assumptions_source']}. "
            + " ".join(valuation["simplifications"])
        )

    st.subheader("Red team")
    table(report["red_team"], "No findings.")
    st.caption("Not checked: " + "; ".join(report["not_checked"]))
    st.subheader("Evidence")
    table(
        [
            {
                "item": e["claim"],
                "value": e["value"],
                "kind": e["verification"],
                "period end": e["period_end"],
                "source": e["source_url"] or e.get("source_title") or "",
            }
            for e in report["evidence"]
        ],
        "",
    )
    st.subheader("Upcoming catalysts")
    table(
        [
            {
                "event": c["event_type"],
                "date status": c["date_status"],
                "date": c["event_date"] or f"{c['window_start']} to {c['window_end']}",
                "relevance": c["relevance"],
                "source": c["source_url"] or c["source_kind"],
                "note": c.get("note") or "",
            }
            for c in report.get("upcoming_catalysts", [])
        ],
        "No upcoming catalyst with a credible date.",
    )
    st.caption(report["catalyst_note"])
    st.subheader("News (leads, not proof)")
    table(
        [
            {
                "published (UTC)": a["published_at"][:16].replace("T", " "),
                "headline": a["headline"],
                "status": a["verification"],
                "duplicate of": a["duplicate_of"] or "",
                "flags": "; ".join(a["flags"]),
                "filing": a["corroborating_filing"] or "",
            }
            for a in report.get("news", [])
        ],
        "No news analysed.",
    )
    st.caption(report.get("news_note", ""))
    st.caption(report["disclaimer"])
