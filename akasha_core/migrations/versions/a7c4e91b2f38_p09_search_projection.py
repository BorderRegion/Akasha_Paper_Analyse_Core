"""p09 search projection: FTS + pgvector index tables

Revision ID: a7c4e91b2f38
Revises: bde0be4528ea
Create Date: 2026-09-17 18:05:00.000000

The search index is a DERIVED projection (spec doc 01 §17): evidence and
claims are immutable (the evidence immutability trigger refuses content
UPDATEs), so their searchable forms live in a separate rebuildable table.
Deleting and rebuilding the projection never touches canonical records.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c4e91b2f38"
down_revision: str | None = "bde0be4528ea"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: pgvector column width for the configured embedding model.
EMBEDDING_DIMENSIONS = 64


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "search_documents",
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("document_type", sa.String(length=16), nullable=False),
        sa.Column("paper_id", sa.String(length=32), nullable=True),
        sa.Column("paper_version_id", sa.String(length=32), nullable=True),
        sa.Column("section_id", sa.String(length=32), nullable=True),
        sa.Column("evidence_type", sa.String(length=32), nullable=True),
        sa.Column("claim_type", sa.String(length=16), nullable=True),
        sa.Column("support_state", sa.String(length=32), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("search_vector", sa.dialects.postgresql.TSVECTOR(), nullable=True),
        sa.Column("embedding_model_id", sa.String(length=64), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.paper_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["paper_version_id"], ["paper_versions.paper_version_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("document_id"),
    )
    op.create_index("ix_search_documents_type", "search_documents", ["document_type"], unique=False)
    op.create_index("ix_search_documents_paper", "search_documents", ["paper_id"], unique=False)
    op.create_index(
        "ix_search_documents_version",
        "search_documents",
        ["paper_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_search_documents_claim_state",
        "search_documents",
        ["claim_type", "support_state"],
        unique=False,
    )
    # Full-text search index (PostgreSQL FTS, spec doc 01 §17 channel 2).
    op.execute("CREATE INDEX ix_search_documents_fts ON search_documents USING gin (search_vector)")

    # pgvector column + approximate-nearest-neighbour index (channel 3).
    op.execute(f"ALTER TABLE search_documents ADD COLUMN embedding vector({EMBEDDING_DIMENSIONS})")
    op.execute(
        "CREATE INDEX ix_search_documents_embedding ON search_documents "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_search_documents_embedding", table_name="search_documents")
    op.drop_index("ix_search_documents_fts", table_name="search_documents")
    op.drop_index("ix_search_documents_claim_state", table_name="search_documents")
    op.drop_index("ix_search_documents_version", table_name="search_documents")
    op.drop_index("ix_search_documents_paper", table_name="search_documents")
    op.drop_index("ix_search_documents_type", table_name="search_documents")
    op.drop_table("search_documents")
