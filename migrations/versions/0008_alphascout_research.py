"""alphascout research: source documents, versioned reports, evidence

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "research_documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_research_documents"),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="ck_research_documents_status_valid"),
    )
    op.create_index("ix_research_documents_url_sha", "research_documents", ["url", "sha256"])
    op.create_table(
        "research_reports",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("cik", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("reference_price", sa.Numeric(), nullable=True),
        sa.Column("reference_bar_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("engine_version", sa.String(32), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column("document_ids", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_research_reports"),
        sa.UniqueConstraint("symbol", "version", name="uq_research_reports_symbol_version"),
        sa.CheckConstraint(
            "verdict IN ('rejected', 'insufficient_evidence', 'no_2x_path', 'watchlist')",
            name="ck_research_reports_verdict_valid",
        ),
    )
    op.create_table(
        "research_evidence",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("claim", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.String(16), nullable=True),
        sa.Column("verification", sa.String(32), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("concept", sa.String(128), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("accession", sa.String(32), nullable=True),
        sa.Column("filed", sa.Date(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_research_evidence"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["research_reports.id"],
            name="fk_research_evidence_report_id_research_reports",
        ),
        sa.CheckConstraint(
            "verification IN ('primary_source', 'market_data', 'computed', 'assumption')",
            name="ck_research_evidence_verification_valid",
        ),
    )
    op.create_index("ix_research_evidence_report", "research_evidence", ["report_id"])


def downgrade() -> None:
    op.drop_index("ix_research_evidence_report", table_name="research_evidence")
    op.drop_table("research_evidence")
    op.drop_table("research_reports")
    op.drop_index("ix_research_documents_url_sha", table_name="research_documents")
    op.drop_table("research_documents")
