"""Database health probe (spec doc 06 §9: "Database: connectivity, schema
revision, pool health")."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Engine

from paperintel.schemas.enums import ModuleHealthState
from paperintel.schemas.health import ModuleHealthRecord

REPO_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def head_revision(alembic_ini: Path = ALEMBIC_INI) -> str | None:
    config = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head()


def database_health(engine: Engine, alembic_ini: Path = ALEMBIC_INI) -> ModuleHealthRecord:
    """Cheap synchronous probe: connectivity + schema revision + pgvector."""
    checks: dict[str, str] = {}
    last_error: dict | None = None
    state = ModuleHealthState.HEALTHY

    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = MigrationContext.configure(connection).get_current_revision()
            has_vector = (
                connection.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                ).scalar()
                is not None
            )
        checks["connectivity"] = "PASS"
    except Exception as exc:  # noqa: BLE001 - recorded as health, never swallowed
        checks["connectivity"] = "FAIL"
        return ModuleHealthRecord(
            module_id="database.core",
            state=ModuleHealthState.UNAVAILABLE,
            checks=checks,
            last_error={
                "error_code": "DB_001",
                "type": type(exc).__name__,
            },
        )

    expected_head = head_revision(alembic_ini)
    if expected_head is not None and current != expected_head:
        checks["alembic_revision_matches_head"] = "FAIL"
        state = ModuleHealthState.DEGRADED
        last_error = {
            "error_code": "DB_002",
            "current_revision": current,
            "head_revision": expected_head,
        }
    else:
        checks["alembic_revision_matches_head"] = "PASS"

    checks["pgvector_extension"] = "PASS" if has_vector else "FAIL"
    if not has_vector and state is ModuleHealthState.HEALTHY:
        state = ModuleHealthState.DEGRADED
        last_error = last_error or {"error_code": "DB_002", "reason": "pgvector missing"}

    pool = engine.pool
    return ModuleHealthRecord(
        module_id="database.core",
        state=state,
        checks=checks,
        metrics={
            "pool_size": getattr(pool, "size", lambda: None)(),
            "checked_in": getattr(pool, "checkedin", lambda: None)(),
            "checked_out": getattr(pool, "checkedout", lambda: None)(),
        },
        last_error=last_error,
    )


__all__ = ["ALEMBIC_INI", "database_health", "head_revision"]
