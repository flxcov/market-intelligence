"""options simulator: option quotes, multipliers, expiry ledger lines

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDENED = (
    ("paper_orders", "symbol", False),
    ("paper_fills", "symbol", False),
    ("paper_ledger", "symbol", True),
    ("paper_positions", "symbol", False),
)


def upgrade() -> None:
    op.create_table(
        "option_quotes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("underlying", sa.String(16), nullable=False),
        sa.Column("option_type", sa.String(4), nullable=False),
        sa.Column("strike", sa.Numeric(), nullable=False),
        sa.Column("expiration", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("quoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("bid", sa.Numeric(), nullable=False),
        sa.Column("ask", sa.Numeric(), nullable=False),
        sa.Column("bid_size", sa.Numeric(), nullable=False),
        sa.Column("ask_size", sa.Numeric(), nullable=False),
        sa.Column("last_trade_price", sa.Numeric(), nullable=True),
        sa.Column("last_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("implied_volatility", sa.Numeric(), nullable=True),
        sa.Column("greeks", postgresql.JSONB(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_option_quotes"),
        sa.UniqueConstraint(
            "symbol", "feed", "quoted_at", name="uq_option_quotes_symbol_feed_time"
        ),
        sa.CheckConstraint("option_type IN ('call', 'put')", name="ck_option_quotes_type_valid"),
        sa.CheckConstraint("strike > 0", name="ck_option_quotes_strike_positive"),
        sa.CheckConstraint(
            "bid >= 0 AND ask >= 0 AND bid_size >= 0 AND ask_size >= 0",
            name="ck_option_quotes_values_valid",
        ),
        sa.CheckConstraint("bid = 0 OR ask = 0 OR ask >= bid", name="ck_option_quotes_not_crossed"),
    )
    op.create_index(
        "ix_option_quotes_underlying", "option_quotes", ["underlying", "feed", "quoted_at"]
    )

    for table, column, nullable in _WIDENED:
        op.alter_column(
            table,
            column,
            type_=sa.String(32),
            existing_type=sa.String(16),
            existing_nullable=nullable,
        )

    op.add_column("paper_accounts", sa.Column("option_settings", postgresql.JSONB(), nullable=True))
    op.add_column(
        "paper_orders",
        sa.Column("asset_class", sa.String(16), server_default="equity", nullable=False),
    )
    for table in ("paper_orders", "paper_fills", "paper_ledger", "paper_positions"):
        op.add_column(
            table, sa.Column("multiplier", sa.Integer(), server_default="1", nullable=False)
        )
    op.create_check_constraint(
        "ck_paper_orders_asset_class_valid",
        "paper_orders",
        "(asset_class = 'equity' AND multiplier = 1) OR "
        "(asset_class = 'option' AND multiplier = 100 AND order_type IN ('market', 'limit'))",
    )
    for table in ("paper_fills", "paper_ledger", "paper_positions"):
        op.create_check_constraint(f"ck_{table}_multiplier_valid", table, "multiplier IN (1, 100)")
    op.drop_constraint("ck_paper_ledger_kind_valid", "paper_ledger", type_="check")
    op.create_check_constraint(
        "ck_paper_ledger_kind_valid",
        "paper_ledger",
        "kind IN ('deposit', 'buy', 'sell', 'fee', 'expire')",
    )


def downgrade() -> None:
    # Refuses (via the restored constraints) if option activity exists; delete it first.
    op.drop_constraint("ck_paper_ledger_kind_valid", "paper_ledger", type_="check")
    op.create_check_constraint(
        "ck_paper_ledger_kind_valid", "paper_ledger", "kind IN ('deposit', 'buy', 'sell', 'fee')"
    )
    for table in ("paper_fills", "paper_ledger", "paper_positions"):
        op.drop_constraint(f"ck_{table}_multiplier_valid", table, type_="check")
    op.drop_constraint("ck_paper_orders_asset_class_valid", "paper_orders", type_="check")
    for table in ("paper_orders", "paper_fills", "paper_ledger", "paper_positions"):
        op.drop_column(table, "multiplier")
    op.drop_column("paper_orders", "asset_class")
    op.drop_column("paper_accounts", "option_settings")
    for table, column, nullable in _WIDENED:
        op.alter_column(
            table,
            column,
            type_=sa.String(16),
            existing_type=sa.String(32),
            existing_nullable=nullable,
        )
    op.drop_index("ix_option_quotes_underlying", table_name="option_quotes")
    op.drop_table("option_quotes")
