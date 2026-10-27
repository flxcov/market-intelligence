"""Options endpoints over HTTP against a real database (synthetic quotes, fake provider).

Order submission uses the real clock, so quotes are stamped a minute before
"now" and the contract expires 30 days out. GTC avoids depending on the hour.
"""

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from packages.market_data.options import OptionQuote
from packages.shared.timeutils import utc_now
from tests.db.test_api_with_database import AUTH, make_client
from tests.fakes import FakeProvider

D = Decimal
NOW = utc_now()
EXPIRY = (NOW + timedelta(days=30)).strftime("%y%m%d")
CALL = f"OPTU{EXPIRY}C00100000"
PUT = f"OPTU{EXPIRY}P00095000"


def quote(symbol: str, bid: str, ask: str) -> OptionQuote:
    return OptionQuote(
        symbol=symbol,
        quoted_at=NOW - timedelta(minutes=1),
        bid=D(bid),
        ask=D(ask),
        bid_size=D(20),
        ask_size=D(20),
    )


@pytest.fixture
def client(database_url: str, migrated_engine: Engine) -> Iterator[TestClient]:
    provider = FakeProvider(option_quotes=[quote(CALL, "2.40", "2.50"), quote(PUT, "1.10", "1.20")])
    with make_client(database_url, provider) as test_client:
        yield test_client


def test_ingest_and_read_the_stored_chain(client: TestClient) -> None:
    body = {"underlying": "optu"}
    assert client.post("/market/options/chain/ingest", json=body).status_code == 401
    first = client.post("/market/options/chain/ingest", json=body, headers=AUTH).json()
    assert (first["feed"], first["inserted"], first["unchanged"]) == ("indicative", 2, 0)
    again = client.post("/market/options/chain/ingest", json=body, headers=AUTH).json()
    assert (again["inserted"], again["unchanged"]) == (0, 2)

    chain = client.get("/market/options/chain?underlying=OPTU").json()
    assert [q["symbol"] for q in chain["quotes"]] == [CALL, PUT]
    call = chain["quotes"][0]
    assert (call["bid"], call["ask"], call["mid"], call["spread_pct_of_mid"]) == (
        "2.40",
        "2.50",
        "2.45",
        "4.08",
    )
    assert call["strike"] == "100" and call["option_type"] == "call"
    assert any("indicative" in n.lower() for n in chain["notices"])

    history = client.get(f"/market/options/contracts/{CALL.lower()}/quotes").json()
    assert len(history) == 1 and history[0]["symbol"] == CALL

    assert client.get("/market/options/contracts/NOTOCC/quotes").status_code == 422
    naive = client.get("/market/options/chain?underlying=OPTU&as_of=2024-06-03T10:00:00")
    assert naive.status_code == 422


def test_option_order_over_http(client: TestClient) -> None:
    client.post("/market/options/chain/ingest", json={"underlying": "OPTU"}, headers=AUTH)
    account = client.post(
        "/paper/accounts", json={"name": "opt", "starting_cash": "100000"}, headers=AUTH
    ).json()
    assert account["option_settings"]["quote_feed"] == "indicative"
    assert account["option_settings"]["commission_per_contract"] == "0.65"
    url = f"/paper/accounts/{account['id']}/options/orders"
    body = {
        "symbol": CALL,
        "side": "buy",
        "quantity": 1,
        "limit_price": "2.50",
        "time_in_force": "gtc",
    }

    assert client.post(url, json=body).status_code == 401
    response = client.post(url, json=body, headers=AUTH)
    assert response.status_code == 200
    result = response.json()
    assert result["mode"] == "PAPER" and result["accepted"] is True
    order = result["order"]
    assert (order["asset_class"], order["multiplier"]) == ("option", 100)
    assert order["sizing"]["premium_at_risk"] == "250.65"
    assert order["sizing"]["breakeven_at_expiry"] == "102.50"

    too_big = client.post(url, json={**body, "quantity": 3}, headers=AUTH).json()
    assert too_big["accepted"] is False
    assert "per_trade_risk" in too_big["order"]["status_reason"]


@pytest.mark.parametrize(
    "body",
    [
        {"symbol": "OPTU", "side": "buy", "quantity": 1, "order_type": "market"},
        {"symbol": CALL, "side": "buy", "quantity": 1, "order_type": "stop"},
        {"symbol": CALL, "side": "buy", "order_type": "market"},  # buys need a quantity
        {"symbol": CALL, "side": "buy", "quantity": 1},  # limit (default) without a price
        {"symbol": CALL, "side": "buy", "quantity": 1, "limit_price": "2.505"},
        {"symbol": CALL, "side": "buy", "quantity": 1, "order_type": "market", "x": 1},
    ],
)
def test_invalid_option_orders_are_rejected_before_any_work(client: TestClient, body) -> None:
    account = client.post(
        "/paper/accounts", json={"name": "opt", "starting_cash": "100000"}, headers=AUTH
    ).json()
    url = f"/paper/accounts/{account['id']}/options/orders"
    assert client.post(url, json=body, headers=AUTH).status_code == 422
