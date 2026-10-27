"""market_bars and ingestion_runs tables

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_bars",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(), nullable=False),
        sa.Column("high", sa.Numeric(), nullable=False),
        sa.Column("low", sa.Numeric(), nullable=False),
        sa.Column("close", sa.Numeric(), nullable=False),
        sa.Column("volume", sa.Numeric(), nullable=False),
        sa.Column("trade_count", sa.BigInteger(), nullable=True),
        sa.Column("vwap", sa.Numeric(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_bars"),
        sa.UniqueConstraint(
            "symbol", "timeframe", "feed", "adjustment", "timestamp", name="uq_market_bars_key"
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0",
            name="ck_market_bars_prices_positive",
        ),
        sa.CheckConstraint(
            "high >= low AND high >= open AND high >= close AND low <= open AND low <= close",
            name="ck_market_bars_ohlc_consistent",
        ),
        sa.CheckConstraint("volume >= 0", name="ck_market_bars_volume_non_negative"),
        sa.CheckConstraint("revision >= 1", name="ck_market_bars_revision_positive"),
    )

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("feed", sa.String(16), nullable=True),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("range_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bars_received", sa.Integer(), nullable=True),
        sa.Column("bars_skipped_incomplete", sa.Integer(), nullable=True),
        sa.Column("inserted", sa.Integer(), nullable=True),
        sa.Column("updated", sa.Integer(), nullable=True),
        sa.Column("unchanged", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_runs"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name="ck_ingestion_runs_status_valid"
        ),
    )


def downgrade() -> None:
    op.drop_table("ingestion_runs")
    op.drop_table("market_bars")
