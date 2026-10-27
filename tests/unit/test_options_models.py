"""OCC symbols, quotes and chain requests (synthetic values only)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from packages.market_data.options import (
    InvalidContractSymbolError,
    OptionChainRequest,
    OptionContract,
    OptionQuote,
    OptionType,
    eastern_midnight,
    regular_close_on,
)

D = Decimal
T = datetime(2024, 6, 3, 15, 0, tzinfo=UTC)


def quote(**overrides: object) -> OptionQuote:
    fields: dict[str, object] = {
        "symbol": "AAPL240621C00190000",
        "quoted_at": T,
        "bid": D("2.40"),
        "ask": D("2.50"),
        "bid_size": D(10),
        "ask_size": D(12),
    }
    fields.update(overrides)
    return OptionQuote(**fields)


def test_parses_occ_symbol() -> None:
    c = OptionContract.parse("aapl240621c00190500")
    assert c.symbol == "AAPL240621C00190500"
    assert c.root == "AAPL"
    assert c.expiration == date(2024, 6, 21)
    assert c.option_type is OptionType.CALL
    assert c.strike == D("190.5")
    assert c.multiplier == 100


def test_parses_put_with_fractional_strike() -> None:
    c = OptionContract.parse("SPY240621P00002375")
    assert c.option_type is OptionType.PUT
    assert c.strike == D("2.375")


@pytest.mark.parametrize(
    "symbol",
    [
        "AAPL",
        "AAPL240621X00190000",  # bad type
        "AAPL241321C00190000",  # month 13
        "AAPL240631C00190000",  # June 31st
        "AAPL240621C0019000",  # 7-digit strike
        "AAPL240621C00000000",  # zero strike
        "TOOLONGROOT240621C00190000",
    ],
)
def test_rejects_invalid_symbols(symbol: str) -> None:
    with pytest.raises(InvalidContractSymbolError):
        OptionContract.parse(symbol)


def test_intrinsic_value_and_breakeven() -> None:
    call = OptionContract.parse("AAPL240621C00190000")
    put = OptionContract.parse("AAPL240621P00190000")
    assert call.intrinsic_value(D("195.25")) == D("5.25")
    assert call.intrinsic_value(D("185")) == 0
    assert call.intrinsic_value(D("190")) == 0
    assert put.intrinsic_value(D("185.50")) == D("4.50")
    assert put.intrinsic_value(D("195")) == 0
    assert call.breakeven(D("2.50")) == D("192.50")
    assert put.breakeven(D("2.50")) == D("187.50")


def test_expiry_times_are_eastern_converted_to_utc() -> None:
    # 2024-06-21 is in daylight saving time (UTC-4); 2024-01-19 is not (UTC-5).
    assert regular_close_on(date(2024, 6, 21)) == datetime(2024, 6, 21, 20, 0, tzinfo=UTC)
    assert regular_close_on(date(2024, 1, 19)) == datetime(2024, 1, 19, 21, 0, tzinfo=UTC)
    assert eastern_midnight(date(2024, 6, 21)) == datetime(2024, 6, 21, 4, 0, tzinfo=UTC)
    c = OptionContract.parse("AAPL240621C00190000")
    assert c.expires_at == datetime(2024, 6, 21, 20, 0, tzinfo=UTC)


def test_days_to_expiry_uses_the_eastern_date() -> None:
    c = OptionContract.parse("AAPL240621C00190000")
    # 02:00Z on the 21st is still the 20th in New York.
    assert c.days_to_expiry(datetime(2024, 6, 21, 2, 0, tzinfo=UTC)) == 1
    assert c.days_to_expiry(datetime(2024, 6, 21, 14, 0, tzinfo=UTC)) == 0


def test_quote_derived_values() -> None:
    q = quote()
    assert q.two_sided
    assert q.mid == D("2.45")
    assert q.spread_pct_of_mid.quantize(D("0.01")) == D("4.08")


def test_one_sided_quote_has_no_mid() -> None:
    q = quote(bid=D(0))
    assert not q.two_sided
    assert q.mid is None
    assert q.spread_pct_of_mid is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"bid": D("2.60")},  # crossed
        {"bid": D("-0.01")},
        {"ask_size": D(-1)},
        {"quoted_at": datetime(2024, 6, 3, 15, 0)},  # naive
        {"symbol": "AAPL"},
    ],
)
def test_invalid_quotes_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        quote(**overrides)


def test_chain_request_validates_ranges() -> None:
    with pytest.raises(ValidationError, match="expiration_date_gte"):
        OptionChainRequest(
            underlying="AAPL",
            expiration_date_gte=date(2024, 7, 1),
            expiration_date_lte=date(2024, 6, 1),
        )
    with pytest.raises(ValidationError, match="strike_price_gte"):
        OptionChainRequest(underlying="AAPL", strike_price_gte=D(200), strike_price_lte=D(100))
    with pytest.raises(ValidationError):
        OptionChainRequest(underlying="AAPL", max_contracts=0)
    with pytest.raises(ValidationError):
        OptionChainRequest(underlying="AAPL", unexpected=1)
