"""ai explanations audit table

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_explanations",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        sa.Column("subject_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("draft", postgresql.JSONB(), nullable=True),
        sa.Column("issues", postgresql.JSONB(), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_ai_explanations"),
        sa.CheckConstraint(
            "status IN ('accepted', 'rejected', 'failed', 'disabled')",
            name="ck_ai_explanations_status_valid",
        ),
    )
    op.create_index(
        "ix_ai_explanations_subject", "ai_explanations", ["subject_kind", "subject_id", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_ai_explanations_subject", table_name="ai_explanations")
    op.drop_table("ai_explanations")
