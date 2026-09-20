"""Persist retry deadlines across worker restarts."""

import sqlalchemy as sa
from alembic import op

revision = "d32a109bf123"
down_revision = "c21f098af012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tasks", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_next_retry_at", "tasks", ["next_retry_at"])


def downgrade():
    op.drop_index("ix_tasks_next_retry_at", "tasks")
    op.drop_column("tasks", "next_retry_at")
