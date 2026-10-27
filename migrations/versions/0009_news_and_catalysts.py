"""alphascout news items and catalysts

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("symbols", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_news_items"),
        sa.UniqueConstraint("provider", "provider_id", name="uq_news_items_provider_id"),
    )
    op.create_index("ix_news_items_symbol_published", "news_items", ["symbol", "published_at"])
    op.create_table(
        "research_catalysts",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("date_status", sa.String(16), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.Column("relevance", sa.String(8), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_research_catalysts"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["research_reports.id"],
            name="fk_research_catalysts_report_id_research_reports",
        ),
        sa.CheckConstraint(
            "date_status IN ('occurred', 'confirmed', 'company_guided', 'estimated', "
            "'reported', 'rumored')",
            name="ck_research_catalysts_date_status_valid",
        ),
        sa.CheckConstraint(
            "event_date IS NOT NULL OR (window_start IS NOT NULL AND window_end IS NOT NULL)",
            name="ck_research_catalysts_has_date",
        ),
    )
    op.create_index("ix_research_catalysts_report", "research_catalysts", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_research_catalysts_report", table_name="research_catalysts")
    op.drop_table("research_catalysts")
    op.drop_index("ix_news_items_symbol_published", table_name="news_items")
    op.drop_table("news_items")
