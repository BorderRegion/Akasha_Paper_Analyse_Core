"""Database layer (frozen namespace: database).

PostgreSQL 16+ with pgvector is the single canonical relational database
(spec doc 02 §1). Schema changes happen exclusively through Alembic
migrations in ``migrations/``.
"""

from paperintel.database.base import (
    Base,
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from paperintel.database.health import database_health, head_revision

__all__ = [
    "Base",
    "create_engine_from_settings",
    "create_session_factory",
    "database_health",
    "head_revision",
    "session_scope",
]
