"""ORM table definitions.

Schema changes are made with Alembic migrations (see `migrations/`), never by
calling `create_all()` against a real database.

Precision: prices and volumes are PostgreSQL `NUMERIC` without a fixed scale,
which stores the provider's decimal values exactly (no rounding, no floats).
Time: all timestamps are `TIMESTAMP WITH TIME ZONE` and written in UTC.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Deterministic constraint names keep Alembic migrations reproducible.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IngestionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MarketBarRecord(Base):
    """One stored OHLCV bar. A bar is identified by its natural key, not its id."""

    __tablename__ = "market_bars"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(16))
    feed: Mapped[str] = mapped_column(String(16))
    adjustment: Mapped[str] = mapped_column(String(16))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open: Mapped[Decimal] = mapped_column(Numeric)
    high: Mapped[Decimal] = mapped_column(Numeric)
    low: Mapped[Decimal] = mapped_column(Numeric)
    close: Mapped[Decimal] = mapped_column(Numeric)
    volume: Mapped[Decimal] = mapped_column(Numeric)
    trade_count: Mapped[int | None] = mapped_column(BigInteger)
    vwap: Mapped[Decimal | None] = mapped_column(Numeric)
    provider: Mapped[str] = mapped_column(String(32))
    # When the provider returned the values currently stored.
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Starts at 1; incremented each time the provider returns different values.
    revision: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Column order matches the common query: one series, filtered by time range.
        UniqueConstraint(
            "symbol", "timeframe", "feed", "adjustment", "timestamp", name="uq_market_bars_key"
        ),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="prices_positive"),
        CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ohlc_consistent",
        ),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )


class IngestionRunRecord(Base):
    """Audit record of one attempt to fetch and store bars, including failures."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(16))
    # Null until known: a request may rely on the provider's default feed.
    feed: Mapped[str | None] = mapped_column(String(16))
    adjustment: Mapped[str] = mapped_column(String(16))
    range_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    range_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bars_received: Mapped[int | None] = mapped_column(Integer)
    bars_skipped_incomplete: Mapped[int | None] = mapped_column(Integer)
    inserted: Mapped[int | None] = mapped_column(Integer)
    updated: Mapped[int | None] = mapped_column(Integer)
    unchanged: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool | None] = mapped_column(Boolean)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("status IN ('running', 'succeeded', 'failed')", name="status_valid"),
    )


class StrategyConfigRecord(Base):
    """One exact strategy definition: name + version + parameters (immutable)."""

    __tablename__ = "strategy_configs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSONB)
    # sha256 of the canonical JSON of (name, version, parameters)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("parameters_hash", name="uq_strategy_configs_hash"),)


class SignalRecord(Base):
    """A trading signal as generated. Never rewritten; later state lives elsewhere."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy_config_id: Mapped[int] = mapped_column(ForeignKey("strategy_configs.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(16))
    feed: Mapped[str] = mapped_column(String(16))
    adjustment: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    # Timestamp of the completed bar that produced the signal.
    signal_bar_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The `as_of` the strategy was evaluated at.
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Latest retrieval time among the stored bars that were used (data freshness).
    data_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reference_price: Mapped[Decimal] = mapped_column(Numeric)
    stop_price: Mapped[Decimal] = mapped_column(Numeric)
    cancel_if_open_below: Mapped[Decimal] = mapped_column(Numeric)
    cancel_if_open_above: Mapped[Decimal] = mapped_column(Numeric)
    expires_after_bars: Mapped[int] = mapped_column(Integer)
    trailing_exit_lookback_bars: Mapped[int] = mapped_column(Integer)
    time_stop_bars: Mapped[int] = mapped_column(Integer)
    atr: Mapped[Decimal] = mapped_column(Numeric)
    risk_per_share: Mapped[Decimal] = mapped_column(Numeric)
    # Decimal values stored as strings to keep them exact.
    score_components: Mapped[dict] = mapped_column(JSONB)
    rule_checks: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # One signal per strategy config, series and bar: regenerating is idempotent.
        UniqueConstraint(
            "strategy_config_id",
            "symbol",
            "timeframe",
            "feed",
            "adjustment",
            "signal_bar_at",
            name="uq_signals_key",
        ),
        CheckConstraint("side = 'long'", name="side_valid"),
        CheckConstraint("status = 'new'", name="status_valid"),
        CheckConstraint(
            "stop_price > 0 AND stop_price < reference_price", name="stop_below_reference"
        ),
        CheckConstraint("risk_per_share > 0", name="risk_positive"),
        Index("ix_signals_symbol_signal_bar_at", "symbol", "signal_bar_at"),
    )


# --- PAPER trading -----------------------------------------------------------------
# Everything below is simulated. No table here is ever connected to a real broker.


class PaperAccountRecord(Base):
    __tablename__ = "paper_accounts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    # Market data feed used for fills and marks (raw, unadjusted prices).
    feed: Mapped[str] = mapped_column(String(16))
    starting_cash: Mapped[Decimal] = mapped_column(Numeric)
    # Materialized from the ledger; verified by reconciliation.
    cash: Mapped[Decimal] = mapped_column(Numeric)
    risk_limits: Mapped[dict] = mapped_column(JSONB)
    cost_model: Mapped[dict] = mapped_column(JSONB)
    # Options feed, commission and quote-quality limits; NULL means the defaults.
    option_settings: Mapped[dict | None] = mapped_column(JSONB)
    # Timestamp of the last daily bar session the simulation has processed.
    processed_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("name", name="uq_paper_accounts_name"),
        CheckConstraint("status IN ('active', 'halted')", name="status_valid"),
        CheckConstraint("starting_cash > 0", name="starting_cash_positive"),
        CheckConstraint("cash >= 0", name="no_negative_cash"),
    )


class PaperOrderRecord(Base):
    __tablename__ = "paper_orders"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    asset_class: Mapped[str] = mapped_column(String(16), server_default="equity")
    # Units per quantity: 1 for shares, 100 for standard option contracts.
    multiplier: Mapped[int] = mapped_column(Integer, server_default="1")
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    time_in_force: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric)
    # For buys: the stop that defines the trade's risk (placed when the buy fills).
    protective_stop_price: Mapped[Decimal | None] = mapped_column(Numeric)
    cancel_if_open_below: Mapped[Decimal | None] = mapped_column(Numeric)
    cancel_if_open_above: Mapped[Decimal | None] = mapped_column(Numeric)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    parent_order_id: Mapped[int | None] = mapped_column(ForeignKey("paper_orders.id"))
    # Set when the order was placed by a forward-trading deployment.
    deployment_id: Mapped[int | None] = mapped_column(ForeignKey("paper_deployments.id"))
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    status_reason: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Only sessions opening at or after this moment can fill the order (latency rule).
    active_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Signal orders may only fill on the first session after this bar.
    only_first_bar_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Worst-case cash a working buy may consume (reserved against new entries).
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric)
    risk_checks: Mapped[list] = mapped_column(JSONB)
    sizing: Mapped[dict | None] = mapped_column(JSONB)
    filled_bar_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "client_order_id", name="uq_paper_orders_client_id"),
        # At most one live (non-rejected) order per account and signal.
        Index(
            "uq_paper_orders_signal",
            "account_id",
            "signal_id",
            unique=True,
            postgresql_where=text("signal_id IS NOT NULL AND status <> 'rejected'"),
        ),
        Index("ix_paper_orders_account_status", "account_id", "status"),
        CheckConstraint("side IN ('buy', 'sell')", name="side_valid"),
        CheckConstraint("order_type IN ('market', 'limit', 'stop')", name="type_valid"),
        CheckConstraint("time_in_force IN ('day', 'gtc')", name="tif_valid"),
        CheckConstraint(
            "status IN ('working', 'filled', 'cancelled', 'expired', 'rejected')",
            name="status_valid",
        ),
        CheckConstraint(
            "quantity > 0 OR (status = 'rejected' AND quantity = 0)", name="quantity_positive"
        ),
        CheckConstraint(
            "(asset_class = 'equity' AND multiplier = 1) OR "
            "(asset_class = 'option' AND multiplier = 100 AND order_type IN ('market', 'limit'))",
            name="asset_class_valid",
        ),
    )


class PaperFillRecord(Base):
    __tablename__ = "paper_fills"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"))
    order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    multiplier: Mapped[int] = mapped_column(Integer, server_default="1")
    base_price: Mapped[Decimal] = mapped_column(Numeric)  # before spread/slippage
    price: Mapped[Decimal] = mapped_column(Numeric)
    commission: Mapped[Decimal] = mapped_column(Numeric)
    # The daily bar (equities) or the quote time (options) the fill was simulated on.
    bar_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(8), server_default="PAPER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("order_id", name="uq_paper_fills_order"),  # no partial fills (v1)
        CheckConstraint("mode = 'PAPER'", name="paper_only"),
        CheckConstraint("quantity > 0 AND price > 0 AND commission >= 0", name="values_valid"),
        CheckConstraint("multiplier IN (1, 100)", name="multiplier_valid"),
    )


class PaperLedgerRecord(Base):
    """Append-only cash ledger: the source of truth for PAPER accounting."""

    __tablename__ = "paper_ledger"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"))
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric)
    symbol: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric)
    multiplier: Mapped[int] = mapped_column(Integer, server_default="1")
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("paper_fills.id"))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("kind IN ('deposit', 'buy', 'sell', 'fee', 'expire')", name="kind_valid"),
        CheckConstraint("multiplier IN (1, 100)", name="multiplier_valid"),
        Index("ix_paper_ledger_account_id", "account_id", "id"),
    )


class PaperPositionRecord(Base):
    """Materialized position state, derived from the ledger; verified by reconciliation."""

    __tablename__ = "paper_positions"

    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer)
    multiplier: Mapped[int] = mapped_column(Integer, server_default="1")
    cost_basis: Mapped[Decimal] = mapped_column(Numeric)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="no_short_positions"),
        CheckConstraint("multiplier IN (1, 100)", name="multiplier_valid"),
    )


class PaperEquitySnapshotRecord(Base):
    """Account value at each processed session close (drives daily-loss and drawdown)."""

    __tablename__ = "paper_equity_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"))
    bar_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cash: Mapped[Decimal] = mapped_column(Numeric)
    market_value: Mapped[Decimal] = mapped_column(Numeric)
    equity: Mapped[Decimal] = mapped_column(Numeric)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("account_id", "bar_at", name="uq_paper_equity_snapshots_bar"),
    )


class OptionQuoteRecord(Base):
    """One option contract quote as provided (an append-only time series per contract)."""

    __tablename__ = "option_quotes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32))
    underlying: Mapped[str] = mapped_column(String(16))
    option_type: Mapped[str] = mapped_column(String(4))
    strike: Mapped[Decimal] = mapped_column(Numeric)
    expiration: Mapped[date] = mapped_column(Date)
    provider: Mapped[str] = mapped_column(String(32))
    feed: Mapped[str] = mapped_column(String(16))
    quoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bid: Mapped[Decimal] = mapped_column(Numeric)
    ask: Mapped[Decimal] = mapped_column(Numeric)
    bid_size: Mapped[Decimal] = mapped_column(Numeric)
    ask_size: Mapped[Decimal] = mapped_column(Numeric)
    last_trade_price: Mapped[Decimal | None] = mapped_column(Numeric)
    last_trade_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    implied_volatility: Mapped[Decimal | None] = mapped_column(Numeric)
    # Provider-computed greeks, stored as decimal strings; informational only.
    greeks: Mapped[dict | None] = mapped_column(JSONB)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("symbol", "feed", "quoted_at", name="uq_option_quotes_symbol_feed_time"),
        Index("ix_option_quotes_underlying", "underlying", "feed", "quoted_at"),
        CheckConstraint("option_type IN ('call', 'put')", name="type_valid"),
        CheckConstraint("strike > 0", name="strike_positive"),
        CheckConstraint(
            "bid >= 0 AND ask >= 0 AND bid_size >= 0 AND ask_size >= 0", name="values_valid"
        ),
        CheckConstraint("bid = 0 OR ask = 0 OR ask >= bid", name="not_crossed"),
    )


class AIExplanationRecord(Base):
    """Every explanation attempt, including rejected and failed ones (audit trail)."""

    __tablename__ = "ai_explanations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[int] = mapped_column(BigInteger)
    # accepted: AI text passed validation; rejected: it failed validation;
    # failed: the provider call failed; disabled: no AI configured.
    status: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    facts: Mapped[dict] = mapped_column(JSONB)
    facts_hash: Mapped[str] = mapped_column(String(64))
    draft: Mapped[dict | None] = mapped_column(JSONB)  # the model's raw answer, if any
    issues: Mapped[list] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_ai_explanations_subject", "subject_kind", "subject_id", "id"),
        CheckConstraint(
            "status IN ('accepted', 'rejected', 'failed', 'disabled')", name="status_valid"
        ),
    )


class ResearchDocumentRecord(Base):
    """Every source document fetch (and failed fetch) behind AlphaScout research.

    Identical content from the same URL is stored once; later fetches point at it."""

    __tablename__ = "research_documents"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_research_documents_url_sha", "url", "sha256"),
        CheckConstraint("status IN ('ok', 'failed')", name="status_valid"),
    )


class ResearchReportRecord(Base):
    """An AlphaScout research report. Append-only: a new run is a new version."""

    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(16))
    cik: Mapped[int] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verdict: Mapped[str] = mapped_column(String(32))
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric)
    reference_bar_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    engine_version: Mapped[str] = mapped_column(String(32))
    report: Mapped[dict] = mapped_column(JSONB)
    document_ids: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("symbol", "version", name="uq_research_reports_symbol_version"),
        CheckConstraint(
            "verdict IN ('rejected', 'insufficient_evidence', 'no_2x_path', 'watchlist')",
            name="verdict_valid",
        ),
    )


class ResearchEvidenceRecord(Base):
    """One cited fact, computation or assumption behind a report."""

    __tablename__ = "research_evidence"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("research_reports.id"))
    key: Mapped[str] = mapped_column(String(64))
    claim: Mapped[str] = mapped_column(Text)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    unit: Mapped[str | None] = mapped_column(String(16))
    verification: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(Text)
    concept: Mapped[str | None] = mapped_column(String(128))
    period_end: Mapped[date | None] = mapped_column(Date)
    accession: Mapped[str | None] = mapped_column(String(32))
    filed: Mapped[date | None] = mapped_column(Date)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_research_evidence_report", "report_id"),
        CheckConstraint(
            "verification IN ('primary_source', 'market_data', 'computed', 'assumption')",
            name="verification_valid",
        ),
    )


class NewsItemRecord(Base):
    """A news article as provided (third-party, untrusted text). One row per provider id."""

    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(16))  # the symbol it was retrieved for
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(64))
    author: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbols: Mapped[list] = mapped_column(JSONB)
    summary: Mapped[str] = mapped_column(Text)
    content_text: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("provider", "provider_id", name="uq_news_items_provider_id"),
        Index("ix_news_items_symbol_published", "symbol", "published_at"),
    )


class ResearchCatalystRecord(Base):
    """A catalyst found for a report (append-only, like the report)."""

    __tablename__ = "research_catalysts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("research_reports.id"))
    symbol: Mapped[str] = mapped_column(String(16))
    event_type: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text)
    date_status: Mapped[str] = mapped_column(String(16))
    event_date: Mapped[date | None] = mapped_column(Date)
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    relevance: Mapped[str] = mapped_column(String(8))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_kind: Mapped[str] = mapped_column(String(32))
    excerpt: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_research_catalysts_report", "report_id"),
        CheckConstraint(
            "date_status IN ('occurred', 'confirmed', 'company_guided', 'estimated', "
            "'reported', 'rumored')",
            name="date_status_valid",
        ),
        CheckConstraint(
            "event_date IS NOT NULL OR (window_start IS NOT NULL AND window_end IS NOT NULL)",
            name="has_date",
        ),
    )


class BacktestRecord(Base):
    """A completed backtest: inputs, a fingerprint of the exact data used, and results."""

    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    strategy: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32))
    parameters_hash: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    # sha256 over every bar used (warm-up and benchmark included): same fingerprint +
    # same request => same results.
    data_fingerprint: Mapped[str] = mapped_column(String(64))
    bars_used: Mapped[int] = mapped_column(Integer)
    report: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaperDeploymentRecord(Base):
    """A strategy configuration run forward, day by day, on a PAPER account."""

    __tablename__ = "paper_deployments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("paper_accounts.id"))
    name: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(64))
    strategy_version: Mapped[str] = mapped_column(String(32))
    parameters: Mapped[dict] = mapped_column(JSONB)
    parameters_hash: Mapped[str] = mapped_column(String(64))
    symbols: Mapped[list] = mapped_column(JSONB)
    signal_adjustment: Mapped[str] = mapped_column(String(16))
    # active: full cycle | paused: manage exits, no new entries | stopped: fills only
    status: Mapped[str] = mapped_column(String(16))
    last_cycle_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("name", name="uq_paper_deployments_name"),
        CheckConstraint("status IN ('active', 'paused', 'stopped')", name="status_valid"),
        CheckConstraint("signal_adjustment IN ('split', 'all')", name="adjustment_valid"),
    )


class TradingCycleRecord(Base):
    """Audit record of one forward-trading cycle (ingest, process, exits, entries)."""

    __tablename__ = "trading_cycles"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    deployment_id: Mapped[int] = mapped_column(ForeignKey("paper_deployments.id"))
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
            name="status_valid",
        ),
        Index("ix_trading_cycles_deployment", "deployment_id", "id"),
    )
