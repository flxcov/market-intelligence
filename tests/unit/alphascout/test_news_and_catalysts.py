"""News verification and catalyst detection on synthetic data."""

import json
from datetime import UTC, date, datetime

import httpx
import pytest

from apps.alphascout.analysis import news as N
from apps.alphascout.analysis.catalysts import (
    credibility_points,
    estimated_next_results,
    meeting_from_proxy,
    news_catalysts,
    past_events,
)
from packages.market_data.errors import ProviderResponseError
from packages.market_data.news import NewsRequest
from packages.sec.errors import SecResponseError
from packages.sec.parsing import Filing, parse_submissions
from packages.shared.text import html_to_text
from tests.fixtures import news_payloads as np
from tests.fixtures import sec_payloads as sp
from tests.unit.alphascout.test_sec import Recorder, client
from tests.unit.test_alpaca_provider import make_provider, ok

AS_OF = datetime(2024, 6, 3, 14, tzinfo=UTC)
FILINGS = parse_submissions(sp.submissions()).filings  # 8-K 2.02 on 2024-05-15


def filing(form, filed, items=""):
    return Filing(
        accession=f"x-{form}-{filed}",
        form=form,
        filing_date=date.fromisoformat(filed),
        report_date=None,
        primary_document="d.htm",
        items=items,
        url=f"https://sec/{form}/{filed}",
    )


# --- text and URLs ---------------------------------------------------------------------


def test_html_to_text_drops_markup_scripts_and_entities() -> None:
    html = (
        "<html><head><title>T</title><script>alert(1)</script></head><body><p>A&amp;B</p>"
        "<div>next</div></body></html>"
    )
    assert html_to_text(html) == "A&B next"
    assert html_to_text("plain  text") == "plain text"


def test_canonical_url_strips_tracking_and_variants() -> None:
    a = N.canonical_url("http://WWW.News.example.com/synt-q1/?utm_source=x&id=7#top")
    b = N.canonical_url("https://news.example.com/synt-q1?id=7")
    assert a == b == "https://news.example.com/synt-q1?id=7"
    assert N.canonical_url("javascript:alert(1)") is None
    assert N.canonical_url(None) is None


# --- news analysis ------------------------------------------------------------------------


def analysed():
    return {a.provider_id: a for a in N.analyse(np.ALL, FILINGS, AS_OF)}


def test_articles_after_as_of_are_invisible() -> None:
    assert "n7" not in analysed()


def test_duplicates_by_url_content_or_same_headline_within_48h() -> None:
    result = analysed()
    assert result["n2"].duplicate_of == "n1"
    same_headline = np.article(
        "n8",
        np.EARNINGS.headline,
        np.at(5, 16),
        content="An entirely different story about a lawsuit.",
    )
    other = N.analyse([np.EARNINGS, same_headline], FILINGS, AS_OF)
    # Same headline, different body, a day later: near-duplicate by headline within 48h.
    assert other[1].duplicate_of == "n1"
    later = np.article("n9", np.EARNINGS.headline, np.at(5, 25), content="Different body.")
    assert N.analyse([np.EARNINGS, later], FILINGS, AS_OF)[1].duplicate_of is None


def test_earnings_news_is_corroborated_by_the_8k() -> None:
    earnings = analysed()["n1"]
    assert earnings.category == "earnings"
    assert earnings.verification == N.Verification.CORROBORATED
    assert earnings.corroborating_filing.endswith("synt-8k.htm")


def test_old_event_re_reported_is_not_new() -> None:
    late = np.article("r1", "Synthetic Example Corp Q1 results recap", np.at(5, 30))
    [result] = N.analyse([late], FILINGS, AS_OF)
    assert result.verification == N.Verification.RE_REPORT


def test_corroboration_is_point_in_time() -> None:
    tomorrow_filing = [filing("8-K", "2024-06-04", "2.02")]
    item = np.article("p1", "SYNT quarterly results", np.at(6, 3, 9))
    assert N.analyse([item], tomorrow_filing, AS_OF)[0].verification == "uncorroborated"
    later = datetime(2024, 6, 5, tzinfo=UTC)
    assert N.analyse([item], tomorrow_filing, later)[0].verification == "corroborated_by_filing"


def test_flags_and_downgrades() -> None:
    result = analysed()
    assert result["n3"].verification == N.Verification.PROMOTIONAL
    assert "anonymous or unconfirmed sourcing" in result["n4"].flags
    assert result["n5"].verification == N.Verification.OPINION
    assert "contains instruction-like text (treated as data)" in result["n6"].flags


def test_headline_figures_missing_from_body_are_flagged() -> None:
    bait = np.article(
        "h1",
        "SYNT wins $500 million contract",
        np.at(5, 10),
        content="SYNT signed a pilot agreement worth $5 million.",
    )
    [result] = N.analyse([bait], [], AS_OF)
    assert any("$500 million" in f for f in result.flags)
    honest = np.article(
        "h2",
        "SYNT wins $5 million pilot",
        np.at(5, 10),
        content="A pilot agreement worth $5 million.",
    )
    assert not any("headline figures" in f for f in N.analyse([honest], [], AS_OF)[0].flags)


# --- catalysts ----------------------------------------------------------------------------


def test_past_events_from_8k_items() -> None:
    events = past_events([filing("8-K", "2024-05-01", "1.01,3.02,9.01")], date(2024, 6, 3))
    assert [(e.event_type, e.date_status, e.relevance) for e in events] == [
        ("material agreement", "occurred", "high"),
        ("unregistered sale of equity (dilution)", "occurred", "high"),
    ]
    assert past_events([filing("8-K", "2024-07-01", "2.02")], date(2024, 6, 3)) == []


PROXY = (
    "<html><body><p>NOTICE OF ANNUAL MEETING</p><p>The 2024 Annual Meeting of Stockholders of "
    "Synthetic Example Corp will be held on Thursday, July 18, 2024 at 9:00 a.m.</p></body></html>"
)


def test_meeting_date_from_a_definitive_proxy() -> None:
    proxy = filing("DEF 14A", "2024-05-01")
    meeting = meeting_from_proxy(html_to_text(PROXY), proxy, date(2024, 6, 3))
    assert (meeting.event_date, meeting.date_status, meeting.source_url) == (
        date(2024, 7, 18),
        "confirmed",
        proxy.url,
    )
    assert "will be held on Thursday, July 18, 2024" in meeting.excerpt
    assert meeting_from_proxy(html_to_text(PROXY), proxy, date(2024, 8, 1)) is None  # past
    assert meeting_from_proxy("no meeting here", proxy, date(2024, 6, 3)) is None


def test_results_window_is_estimated_from_cadence_never_confirmed() -> None:
    periodic = [filing("10-Q", d) for d in ("2023-05-10", "2023-08-09", "2023-11-08")] + [
        filing("10-K", "2024-02-20"),
        filing("10-Q", "2024-05-10"),
    ]
    estimate = estimated_next_results(periodic, date(2024, 6, 3))
    assert estimate.date_status == "estimated" and estimate.event_date is None
    assert (estimate.window_start, estimate.window_end) == (date(2024, 7, 30), date(2024, 8, 19))
    assert estimated_next_results(periodic[:2], date(2024, 6, 3)) is None  # too little history
    assert estimated_next_results(periodic, date(2024, 10, 1)) is None  # cadence broken


def test_news_dates_are_reported_or_rumored_never_confirmed() -> None:
    articles = N.analyse(np.ALL, FILINGS, AS_OF)
    found = news_catalysts(articles, date(2024, 6, 3))
    assert [(c.event_date, c.date_status, c.source_kind) for c in found] == [
        (date(2024, 7, 15), "rumored", "news")
    ]


def test_credibility_points_weight_status_and_relevance() -> None:
    proxy = filing("DEF 14A", "2024-05-01")
    meeting = meeting_from_proxy(html_to_text(PROXY), proxy, date(2024, 6, 3))
    periodic = [filing("10-Q", d) for d in ("2023-08-09", "2023-11-08", "2024-02-20", "2024-05-10")]
    estimate = estimated_next_results(periodic, date(2024, 6, 3))
    assert credibility_points([meeting], date(2024, 6, 3), 24)[0] == 6  # confirmed x low
    assert credibility_points([meeting, estimate], date(2024, 6, 3), 24)[0] == 8  # 0.4 x 20
    assert credibility_points([], date(2024, 6, 3), 24) == (
        0,
        "no upcoming catalyst with a credible date",
    )


# --- adapters -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_alpaca_news_adapter_parses_and_paginates() -> None:
    page1 = {
        "news": [
            {
                "id": 1,
                "headline": "H1",
                "author": "A",
                "source": "benzinga",
                "url": "https://x/1",
                "summary": "",
                "content": "<p>c</p>",
                "symbols": ["SYNT"],
                "created_at": "2024-05-15T21:00:00Z",
                "updated_at": "2024-05-15T21:00:00Z",
                "images": [],
            }
        ],
        "next_page_token": "p2",
    }
    page2 = {"news": [], "next_page_token": None}
    recorder = Recorder(ok(json.dumps(page1)), ok(json.dumps(page2)))

    provider = make_provider(recorder)
    batch = await provider.get_news(
        NewsRequest(symbol="synt", start=datetime(2024, 3, 1, tzinfo=UTC), end=AS_OF)
    )
    assert [a.provider_id for a in batch.articles] == ["1"]
    first = recorder.requests[0]
    assert first.url.path == "/v1beta1/news"
    assert dict(first.url.params) == {
        "symbols": "SYNT",
        "start": "2024-03-01T00:00:00Z",
        "end": "2024-06-03T14:00:00Z",
        "sort": "desc",
        "include_content": "true",
        "limit": "50",
    }
    assert recorder.requests[1].url.params["page_token"] == "p2"


@pytest.mark.anyio
@pytest.mark.parametrize("text", ['{"articles": []}', '{"news": {}}', '{"news": [{"id": 1}]}'])
async def test_alpaca_news_malformed(text: str) -> None:
    with pytest.raises(ProviderResponseError):
        await make_provider(Recorder(ok(text))).get_news(
            NewsRequest(symbol="SYNT", start=datetime(2024, 3, 1, tzinfo=UTC), end=AS_OF)
        )


@pytest.mark.anyio
async def test_sec_get_text_strips_html_and_only_fetches_archives() -> None:
    recorder = Recorder(httpx.Response(200, text=PROXY, headers={"content-type": "text/html"}))
    url = "https://www.sec.gov/Archives/edgar/data/1234567/000123456724000005/proxy.htm"
    document = await client(recorder).get_text(url)
    assert document.payload["text"].startswith("NOTICE OF ANNUAL MEETING")
    with pytest.raises(SecResponseError, match="Archives"):
        await client(Recorder()).get_text("https://evil.example.com/x")


def test_same_article_url_with_edited_text_is_one_story() -> None:
    first = np.article(
        "u1",
        "SYNT signs supply deal",
        np.at(5, 10),
        content="Version one.",
        url="https://news.example.com/deal?utm_campaign=a",
    )
    edited = np.article(
        "u2",
        "Synthetic Example Corp: supply agreement signed",
        np.at(5, 13),
        content="Version two, rewritten.",
        url="http://www.news.example.com/deal/",
    )
    assert N.analyse([first, edited], [], AS_OF)[1].duplicate_of == "u1"


def test_rumors_never_score() -> None:
    articles = N.analyse([np.RUMOR], [], AS_OF)
    [rumor] = news_catalysts(articles, date(2024, 6, 3))
    assert rumor.date_status == "rumored"
    assert credibility_points([rumor], date(2024, 6, 3), 24)[0] == 0
