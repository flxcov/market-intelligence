"""strategy_configs and signals tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_configs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("parameters_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_configs"),
        sa.UniqueConstraint("parameters_hash", name="uq_strategy_configs_hash"),
    )

    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("strategy_config_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(16), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signal_bar_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reference_price", sa.Numeric(), nullable=False),
        sa.Column("stop_price", sa.Numeric(), nullable=False),
        sa.Column("cancel_if_open_below", sa.Numeric(), nullable=False),
        sa.Column("cancel_if_open_above", sa.Numeric(), nullable=False),
        sa.Column("expires_after_bars", sa.Integer(), nullable=False),
        sa.Column("trailing_exit_lookback_bars", sa.Integer(), nullable=False),
        sa.Column("time_stop_bars", sa.Integer(), nullable=False),
        sa.Column("atr", sa.Numeric(), nullable=False),
        sa.Column("risk_per_share", sa.Numeric(), nullable=False),
        sa.Column("score_components", postgresql.JSONB(), nullable=False),
        sa.Column("rule_checks", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signals"),
        sa.ForeignKeyConstraint(
            ["strategy_config_id"],
            ["strategy_configs.id"],
            name="fk_signals_strategy_config_id_strategy_configs",
        ),
        sa.UniqueConstraint(
            "strategy_config_id",
            "symbol",
            "timeframe",
            "feed",
            "adjustment",
            "signal_bar_at",
            name="uq_signals_key",
        ),
        sa.CheckConstraint("side = 'long'", name="ck_signals_side_valid"),
        sa.CheckConstraint("status = 'new'", name="ck_signals_status_valid"),
        sa.CheckConstraint(
            "stop_price > 0 AND stop_price < reference_price",
            name="ck_signals_stop_below_reference",
        ),
        sa.CheckConstraint("risk_per_share > 0", name="ck_signals_risk_positive"),
    )
    op.create_index("ix_signals_symbol_signal_bar_at", "signals", ["symbol", "signal_bar_at"])


def downgrade() -> None:
    op.drop_index("ix_signals_symbol_signal_bar_at", table_name="signals")
    op.drop_table("signals")
    op.drop_table("strategy_configs")
