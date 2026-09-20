"""Add explicit version provenance without inventing historical versions.

Revision ID: c21f098af012
Revises: a7c4e91b2f38
"""

import sqlalchemy as sa
from alembic import op

revision = "c21f098af012"
down_revision = "a7c4e91b2f38"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("analysis_runs", "claims"):
        op.add_column(table, sa.Column("spec_version", sa.String(255), nullable=True))
        op.add_column(table, sa.Column("prompt_version", sa.String(255), nullable=True))


def downgrade():
    for table in ("claims", "analysis_runs"):
        op.drop_column(table, "prompt_version")
        op.drop_column(table, "spec_version")
