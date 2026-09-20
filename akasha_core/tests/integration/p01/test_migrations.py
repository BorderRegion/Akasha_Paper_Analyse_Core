"""P01 migration tests: up/down in a disposable DB, trigger + pgvector."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.needs_db

EXPECTED_TABLES = {
    "papers",
    "paper_versions",
    "assets",
    "evidence",
    "sections",
    "claims",
    "claim_evidence",
    "external_provenances",
    "verifications",
    "analysis_runs",
    "model_calls",
    "prompt_versions",
    "jobs",
    "tasks",
    "trace_events",
    "entities",
    "relations",
    "tags",
    "tag_aliases",
    "paper_tags",
    "entity_tags",
    "techniques",
    "technique_papers",
    "quality_assessments",
    "venue_registry",
    "collections",
    "collection_papers",
    "triage_results",
}


def _tables(conn) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename <> 'alembic_version'"
        )
    ).scalars()
    return set(rows)


def test_upgrade_creates_all_canonical_tables(test_db_url: str) -> None:
    engine = create_engine(test_db_url)
    with engine.connect() as conn:
        assert EXPECTED_TABLES <= _tables(conn)
    engine.dispose()


def test_pgvector_extension_present(test_db_url: str) -> None:
    engine = create_engine(test_db_url)
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname='vector'")
        ).scalar()
    assert version is not None
    engine.dispose()


def test_evidence_trigger_installed(test_db_url: str) -> None:
    engine = create_engine(test_db_url)
    with engine.connect() as conn:
        triggers = (
            conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger "
                    "WHERE tgrelid = 'evidence'::regclass AND NOT tgisinternal"
                )
            )
            .scalars()
            .all()
        )
    assert "evidence_immutable_guard" in triggers
    engine.dispose()


def test_schema_matches_model_metadata(test_db_url: str) -> None:
    """No drift between ORM metadata and the migrated schema: alembic
    autogenerate against the migrated DB must produce an empty diff."""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    import paperintel.database.models  # noqa: F401 - register mappers
    from paperintel.database.base import Base

    engine = create_engine(test_db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        diff = compare_metadata(ctx, Base.metadata)
    engine.dispose()
    meaningful = [d for d in diff if d[0] not in ("remove_index",)]
    assert not meaningful, f"schema drift detected: {meaningful}"


def test_migration_roundtrip_full(test_db_url: str) -> None:
    """downgrade base → upgrade head must reproduce the schema (the module
    fixture already did up; here we cycle down and up again)."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", test_db_url)
    command.downgrade(cfg, "base")

    engine = create_engine(test_db_url)
    try:
        with engine.connect() as conn:
            assert _tables(conn) == set()
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(test_db_url)
    try:
        with engine.connect() as conn:
            assert EXPECTED_TABLES <= _tables(conn)
            revision = MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert revision == script.get_current_head()
