"""P01 database health probe tests."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from paperintel.database.health import database_health, head_revision
from paperintel.schemas.enums import ModuleHealthState

pytestmark = pytest.mark.needs_db


def test_healthy_database(engine) -> None:
    record = database_health(engine)
    assert record.module_id == "database.core"
    assert record.state is ModuleHealthState.HEALTHY
    assert record.checks["connectivity"] == "PASS"
    assert record.checks["alembic_revision_matches_head"] == "PASS"
    assert record.checks["pgvector_extension"] == "PASS"
    assert record.last_error is None


def test_schema_mismatch_is_degraded_with_db002(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = 'deadbeef0000'"))
    try:
        record = database_health(engine)
        assert record.state is ModuleHealthState.DEGRADED
        assert record.checks["alembic_revision_matches_head"] == "FAIL"
        assert record.last_error is not None
        assert record.last_error["error_code"] == "DB_002"
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :head"),
                {"head": head_revision()},
            )
    record = database_health(engine)
    assert record.state is ModuleHealthState.HEALTHY


def test_unreachable_database_is_unavailable() -> None:
    from sqlalchemy import create_engine

    bad_engine = create_engine(
        "postgresql+psycopg://paperintel:wrong@127.0.0.1:5499/nope",
        pool_pre_ping=False,
        pool_timeout=2,
        connect_args={"connect_timeout": 2},
    )
    try:
        record = database_health(bad_engine)
        assert record.state is ModuleHealthState.UNAVAILABLE
        assert record.checks["connectivity"] == "FAIL"
        assert record.last_error is not None
        assert record.last_error["error_code"] == "DB_001"
    finally:
        bad_engine.dispose()
