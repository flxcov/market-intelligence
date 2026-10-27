"""Explanations against a real database with a scripted fake model (no network)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.marketpilot.config import Settings
from apps.marketpilot.main import create_app
from apps.marketpilot.services.explanations import (
    ExplanationNotFoundError,
    ExplanationService,
    SubjectKind,
)
from apps.marketpilot.services.paper_models import CreateAccountRequest, ManualOrderRequest
from apps.marketpilot.services.paper_trading import PaperTradingService
from packages.ai.model import AIUnavailableError
from packages.database.models import AIExplanationRecord
from packages.market_data.models import DataFeed
from tests.db.test_api_with_database import AUTH, TOKEN, make_client
from tests.fakes import FakeProvider
from tests.fakes_ai import FakeExplanationModel

pytestmark = pytest.mark.anyio

D = Decimal
Sessions = async_sessionmaker[AsyncSession]
NOW = datetime(2024, 6, 3, 14, tzinfo=UTC)


async def rejected_order(sessions: Sessions) -> int:
    """A sell with nothing to sell: rejected by the risk gate (no market data needed)."""
    paper = PaperTradingService(sessions, default_feed=DataFeed.IEX, now=lambda: NOW)
    account = await paper.create_account(
        CreateAccountRequest(name="explain", starting_cash=D(100_000))
    )
    result = await paper.submit_order(
        account.id, ManualOrderRequest(symbol="XYZ", side="sell", quantity=5)
    )
    assert not result.accepted
    return result.order.id


def grounded(order_id: int) -> dict:
    return {
        "summary": f"PAPER sell order {order_id} was rejected.",
        "points": [
            {
                "text": "The account holds 0 XYZ, so selling 5 would be a short sale.",
                "fact_ids": ["check.closes_existing_position", "order.quantity"],
            }
        ],
        "caveats": [],
    }


async def attempts(sessions: Sessions) -> list[AIExplanationRecord]:
    async with sessions() as session:
        return list((await session.scalars(select(AIExplanationRecord).order_by("id"))).all())


async def test_grounded_ai_answer_is_shown_and_recorded(sessions: Sessions) -> None:
    order_id = await rejected_order(sessions)
    model = FakeExplanationModel(grounded(order_id))
    explanation = await ExplanationService(sessions, model, now=lambda: NOW).explain(
        SubjectKind.ORDER, order_id
    )
    assert explanation.source == "ai" and explanation.model == "fake-model-1"
    cited = {f.id for f in explanation.points[0].facts}
    assert cited == {"check.closes_existing_position", "order.quantity"}

    # The model saw the stored risk check, as data.
    [call] = model.calls
    assert "sell 5 vs held 0 (no short selling)" in call["user"]
    assert "<facts>" in call["user"]

    [record] = await attempts(sessions)
    assert (record.status, record.model, record.request_id) == (
        "accepted",
        "fake-model-1",
        "req_fake",
    )
    assert record.draft == grounded(order_id)
    assert record.created_at == NOW


async def test_answer_with_invented_numbers_is_rejected_for_the_facts(sessions: Sessions) -> None:
    order_id = await rejected_order(sessions)
    bad = grounded(order_id)
    bad["points"][0]["text"] = "XYZ last traded at 41.17, so the order was too large."
    explanation = await ExplanationService(sessions, FakeExplanationModel(bad)).explain(
        SubjectKind.ORDER, order_id
    )
    assert explanation.source == "deterministic"
    assert "failed validation" in explanation.notices[0]
    [record] = await attempts(sessions)
    assert record.status == "rejected"
    assert any("41.17" in issue for issue in record.issues)


async def test_answer_off_schema_is_rejected(sessions: Sessions) -> None:
    order_id = await rejected_order(sessions)
    model = FakeExplanationModel({"summary": "x", "points": "not a list", "caveats": []})
    explanation = await ExplanationService(sessions, model).explain(SubjectKind.ORDER, order_id)
    assert explanation.source == "deterministic"
    assert (await attempts(sessions))[0].status == "rejected"


async def test_provider_failure_falls_back_and_is_recorded(sessions: Sessions) -> None:
    order_id = await rejected_order(sessions)
    model = FakeExplanationModel(AIUnavailableError("the AI provider timed out"))
    explanation = await ExplanationService(sessions, model).explain(SubjectKind.ORDER, order_id)
    assert explanation.source == "deterministic"
    [record] = await attempts(sessions)
    assert (record.status, record.error_type) == ("failed", "ai_unavailable")


async def test_without_a_model_explanations_are_deterministic(sessions: Sessions) -> None:
    order_id = await rejected_order(sessions)
    explanation = await ExplanationService(sessions, None).explain(SubjectKind.ORDER, order_id)
    assert explanation.source == "deterministic"
    texts = [p.text for p in explanation.points]
    assert "rule closes_existing_position: FAILED: sell 5 vs held 0 (no short selling)" in texts
    assert (await attempts(sessions))[0].status == "disabled"


async def test_missing_subject_is_a_404(sessions: Sessions) -> None:
    with pytest.raises(ExplanationNotFoundError):
        await ExplanationService(sessions, None).explain(SubjectKind.SIGNAL, 999)


def test_explanations_over_http(database_url: str, migrated_engine) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, log_format="console", database_url=database_url, api_write_token=TOKEN
    )
    model = FakeExplanationModel(grounded(1))
    app = create_app(settings, provider=FakeProvider(), explanation_model=model)
    with TestClient(app) as client:
        assert client.get("/health").json()["ai_explanations_enabled"] is True
        account = client.post(
            "/paper/accounts", json={"name": "e", "starting_cash": "1000"}, headers=AUTH
        ).json()
        client.post(
            f"/paper/accounts/{account['id']}/orders",
            json={"symbol": "XYZ", "side": "sell", "quantity": 5},
            headers=AUTH,
        )
        assert client.post("/explanations/order/1").status_code == 401
        assert client.post("/explanations/trade/1", headers=AUTH).status_code == 422
        body = client.post("/explanations/order/1", headers=AUTH).json()
        assert body["source"] == "ai"
        assert "no calculations" in body["disclaimer"]
        trail = client.get("/explanations/order/1/attempts").json()
        assert [t["status"] for t in trail] == ["accepted"]
        assert client.post("/explanations/order/99", headers=AUTH).status_code == 404

    # Without a model configured, the deterministic explanation is returned.
    with make_client(database_url, FakeProvider()) as client:
        assert client.get("/health").json()["ai_explanations_enabled"] is False
        body = client.post("/explanations/order/1", headers=AUTH).json()
        assert body["source"] == "deterministic"
