"""ui surface: personal state, notes, review decisions, imports, operations

Revision ID: e41f7c9a2b60
Revises: d32a109bf123
Create Date: 2026-09-19 02:10:00.000000

Adds the workbench's own persistence (frontend spec docs/06 「新增持久数据」).
Nothing here changes core scientific tables: personal state, notes, review
decisions, saved searches, import batches and operation requests are UI-owned,
single-user (owner_key) records; ui_evidence_locators is a REBUILDABLE
projection referencing immutable evidence, never a mutation of it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e41f7c9a2b60"
down_revision: str | None = "d32a109bf123"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ui_preferences",
        sa.Column("owner_key", sa.String(length=64), primary_key=True),
        sa.Column(
            "theme", sa.String(length=16), nullable=False, server_default=sa.text("'SYSTEM'")
        ),
        sa.Column(
            "density", sa.String(length=16), nullable=False, server_default=sa.text("'COMFORTABLE'")
        ),
        sa.Column("reduce_motion", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "single_key_shortcuts", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("reader_font_px", sa.Integer(), nullable=False, server_default=sa.text("17")),
        sa.Column("focus_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ui_personal_paper_state",
        sa.Column("owner_key", sa.String(length=64), primary_key=True),
        sa.Column("paper_id", sa.String(length=32), primary_key=True),
        sa.Column("saved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "read_state", sa.String(length=16), nullable=False, server_default=sa.text("'UNREAD'")
        ),
        sa.Column("reading_anchor", postgresql.JSONB(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.paper_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "ui_notes",
        sa.Column("note_id", sa.String(length=32), primary_key=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False, index=True),
        sa.Column("paper_id", sa.String(length=32), nullable=False),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("claim_id", sa.String(length=32), nullable=True),
        sa.Column("evidence_id", sa.String(length=32), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.paper_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["paper_version_id"], ["paper_versions.paper_version_id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_ui_notes_paper", "ui_notes", ["paper_id", "paper_version_id"])

    op.create_table(
        "ui_review_decisions",
        sa.Column("decision_id", sa.String(length=32), primary_key=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False, index=True),
        sa.Column("claim_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=True, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.claim_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "ui_saved_searches",
        sa.Column("saved_search_id", sa.String(length=32), primary_key=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False, index=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("query", postgresql.JSONB(), nullable=False),
        sa.Column(
            "schema_version",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'1.0.0'"),
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ui_import_batches",
        sa.Column("batch_id", sa.String(length=32), primary_key=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False, index=True),
        sa.Column("collection_ids", postgresql.JSONB(), nullable=True),
        sa.Column(
            "requested_tier",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'T2_FULL'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "ui_import_items",
        sa.Column("item_id", sa.String(length=32), primary_key=True),
        sa.Column("batch_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default=sa.text("'PENDING'")
        ),
        sa.Column("paper_id", sa.String(length=32), nullable=True),
        sa.Column("paper_version_id", sa.String(length=32), nullable=True),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["ui_import_batches.batch_id"], ondelete="CASCADE"),
    )

    op.create_table(
        "ui_operation_requests",
        sa.Column("operation_id", sa.String(length=32), primary_key=True),
        sa.Column("owner_key", sa.String(length=64), nullable=False, index=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False, server_default=sa.text("'ACCEPTED'")
        ),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("results", postgresql.JSONB(), nullable=True),
        sa.Column("job_ids", postgresql.JSONB(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=96), nullable=True, unique=True),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Rebuildable projection: references immutable evidence by ID + hash.
    op.create_table(
        "ui_evidence_locators",
        sa.Column("evidence_id", sa.String(length=32), primary_key=True),
        sa.Column("paper_version_id", sa.String(length=32), nullable=False, index=True),
        sa.Column("document_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("page_label", sa.String(length=64), nullable=True),
        sa.Column(
            "coordinate_space",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'DISPLAY_NORMALIZED_V1'"),
        ),
        sa.Column("rect_norm", postgresql.JSONB(), nullable=True),
        sa.Column(
            "precision", sa.String(length=16), nullable=False, server_default=sa.text("'PAGE'")
        ),
        sa.Column("source_method", sa.String(length=32), nullable=False),
        sa.Column("ocr_confidence", sa.Float(), nullable=True),
        sa.Column("transform_revision", sa.String(length=32), nullable=False),
        sa.Column("extraction_run_id", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
    )


def downgrade() -> None:
    op.drop_table("ui_evidence_locators")
    op.drop_table("ui_operation_requests")
    op.drop_table("ui_import_items")
    op.drop_table("ui_import_batches")
    op.drop_table("ui_saved_searches")
    op.drop_table("ui_review_decisions")
    op.drop_index("ix_ui_notes_paper", table_name="ui_notes")
    op.drop_table("ui_notes")
    op.drop_table("ui_personal_paper_state")
    op.drop_table("ui_preferences")
