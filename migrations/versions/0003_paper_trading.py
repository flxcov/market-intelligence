"""paper trading: accounts, orders, fills, ledger, positions, equity snapshots

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        )
    ]


def upgrade() -> None:
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("feed", sa.String(16), nullable=False),
        sa.Column("starting_cash", sa.Numeric(), nullable=False),
        sa.Column("cash", sa.Numeric(), nullable=False),
        sa.Column("risk_limits", postgresql.JSONB(), nullable=False),
        sa.Column("cost_model", postgresql.JSONB(), nullable=False),
        sa.Column("processed_through", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_accounts"),
        sa.UniqueConstraint("name", name="uq_paper_accounts_name"),
        sa.CheckConstraint("status IN ('active', 'halted')", name="ck_paper_accounts_status_valid"),
        sa.CheckConstraint("starting_cash > 0", name="ck_paper_accounts_starting_cash_positive"),
        sa.CheckConstraint("cash >= 0", name="ck_paper_accounts_no_negative_cash"),
    )

    op.create_table(
        "paper_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("time_in_force", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(), nullable=True),
        sa.Column("stop_price", sa.Numeric(), nullable=True),
        sa.Column("protective_stop_price", sa.Numeric(), nullable=True),
        sa.Column("cancel_if_open_below", sa.Numeric(), nullable=True),
        sa.Column("cancel_if_open_above", sa.Numeric(), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("parent_order_id", sa.BigInteger(), nullable=True),
        sa.Column("client_order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("only_first_bar_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reserved_cost", sa.Numeric(), nullable=True),
        sa.Column("risk_checks", postgresql.JSONB(), nullable=False),
        sa.Column("sizing", postgresql.JSONB(), nullable=True),
        sa.Column("filled_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_paper_orders"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_accounts.id"], name="fk_paper_orders_account_id_paper_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["signal_id"], ["signals.id"], name="fk_paper_orders_signal_id_signals"
        ),
        sa.ForeignKeyConstraint(
            ["parent_order_id"],
            ["paper_orders.id"],
            name="fk_paper_orders_parent_order_id_paper_orders",
        ),
        sa.UniqueConstraint("account_id", "client_order_id", name="uq_paper_orders_client_id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_paper_orders_side_valid"),
        sa.CheckConstraint(
            "order_type IN ('market', 'limit', 'stop')", name="ck_paper_orders_type_valid"
        ),
        sa.CheckConstraint("time_in_force IN ('day', 'gtc')", name="ck_paper_orders_tif_valid"),
        sa.CheckConstraint(
            "status IN ('working', 'filled', 'cancelled', 'expired', 'rejected')",
            name="ck_paper_orders_status_valid",
        ),
        sa.CheckConstraint(
            "quantity > 0 OR (status = 'rejected' AND quantity = 0)",
            name="ck_paper_orders_quantity_positive",
        ),
    )
    op.create_index(
        "uq_paper_orders_signal",
        "paper_orders",
        ["account_id", "signal_id"],
        unique=True,
        postgresql_where=sa.text("signal_id IS NOT NULL AND status <> 'rejected'"),
    )
    op.create_index("ix_paper_orders_account_status", "paper_orders", ["account_id", "status"])

    op.create_table(
        "paper_fills",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("base_price", sa.Numeric(), nullable=False),
        sa.Column("price", sa.Numeric(), nullable=False),
        sa.Column("commission", sa.Numeric(), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("mode", sa.String(8), server_default="PAPER", nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_paper_fills"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_accounts.id"], name="fk_paper_fills_account_id_paper_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["paper_orders.id"], name="fk_paper_fills_order_id_paper_orders"
        ),
        sa.UniqueConstraint("order_id", name="uq_paper_fills_order"),
        sa.CheckConstraint("mode = 'PAPER'", name="ck_paper_fills_paper_only"),
        sa.CheckConstraint(
            "quantity > 0 AND price > 0 AND commission >= 0", name="ck_paper_fills_values_valid"
        ),
    )

    op.create_table(
        "paper_ledger",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Numeric(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("price", sa.Numeric(), nullable=True),
        sa.Column("fill_id", sa.BigInteger(), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_paper_ledger"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["paper_accounts.id"], name="fk_paper_ledger_account_id_paper_accounts"
        ),
        sa.ForeignKeyConstraint(
            ["fill_id"], ["paper_fills.id"], name="fk_paper_ledger_fill_id_paper_fills"
        ),
        sa.CheckConstraint(
            "kind IN ('deposit', 'buy', 'sell', 'fee')", name="ck_paper_ledger_kind_valid"
        ),
    )
    op.create_index("ix_paper_ledger_account_id", "paper_ledger", ["account_id", "id"])

    op.create_table(
        "paper_positions",
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("cost_basis", sa.Numeric(), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("account_id", "symbol", name="pk_paper_positions"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper_accounts.id"],
            name="fk_paper_positions_account_id_paper_accounts",
        ),
        sa.CheckConstraint("quantity >= 0", name="ck_paper_positions_no_short_positions"),
    )

    op.create_table(
        "paper_equity_snapshots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("bar_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cash", sa.Numeric(), nullable=False),
        sa.Column("market_value", sa.Numeric(), nullable=False),
        sa.Column("equity", sa.Numeric(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_paper_equity_snapshots"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper_accounts.id"],
            name="fk_paper_equity_snapshots_account_id_paper_accounts",
        ),
        sa.UniqueConstraint("account_id", "bar_at", name="uq_paper_equity_snapshots_bar"),
    )


def downgrade() -> None:
    op.drop_table("paper_equity_snapshots")
    op.drop_table("paper_positions")
    op.drop_index("ix_paper_ledger_account_id", table_name="paper_ledger")
    op.drop_table("paper_ledger")
    op.drop_table("paper_fills")
    op.drop_index("ix_paper_orders_account_status", table_name="paper_orders")
    op.drop_index("uq_paper_orders_signal", table_name="paper_orders")
    op.drop_table("paper_orders")
    op.drop_table("paper_accounts")
