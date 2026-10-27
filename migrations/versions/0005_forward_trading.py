"""forward paper trading: deployments, trading cycles, order attribution

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_deployments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.String(32), nullable=False),
        sa.Column("parameters", postgresql.JSONB(), nullable=False),
        sa.Column("parameters_hash", sa.String(64), nullable=False),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("signal_adjustment", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_cycle_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_deployments"),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["paper_accounts.id"],
            name="fk_paper_deployments_account_id_paper_accounts",
        ),
        sa.UniqueConstraint("name", name="uq_paper_deployments_name"),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'stopped')", name="ck_paper_deployments_status_valid"
        ),
        sa.CheckConstraint(
            "signal_adjustment IN ('split', 'all')", name="ck_paper_deployments_adjustment_valid"
        ),
    )
    op.create_table(
        "trading_cycles",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("summary", postgresql.JSONB(), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_trading_cycles"),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["paper_deployments.id"],
            name="fk_trading_cycles_deployment_id_paper_deployments",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')",
            name="ck_trading_cycles_status_valid",
        ),
    )
    op.create_index("ix_trading_cycles_deployment", "trading_cycles", ["deployment_id", "id"])
    op.add_column("paper_orders", sa.Column("deployment_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_paper_orders_deployment_id_paper_deployments",
        "paper_orders",
        "paper_deployments",
        ["deployment_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_paper_orders_deployment_id_paper_deployments", "paper_orders", type_="foreignkey"
    )
    op.drop_column("paper_orders", "deployment_id")
    op.drop_index("ix_trading_cycles_deployment", table_name="trading_cycles")
    op.drop_table("trading_cycles")
    op.drop_table("paper_deployments")
