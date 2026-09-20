"""Integration fixtures: disposable test databases (shared P01+).

Each test module gets a dedicated throwaway PostgreSQL database with the full
migration stack applied, dropped afterwards. Tests never touch the dev DB.

Isolation pattern (SQLAlchemy-recommended): every test runs inside an outer
connection-level transaction that is rolled back at teardown; the Session
joins it with SAVEPOINTs, so tests may call ``commit()`` freely (trigger and
constraint behavior is exercised) without leaking rows between tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from paperintel.database.base import Base  # noqa: F401 - ensures models registered

ADMIN_URL = "postgresql+psycopg://paperintel:paperintel@127.0.0.1:5432/postgres"
REPO_ALEMBIC_INI = "alembic.ini"


def _make_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture(scope="module")
def test_db_url() -> Iterator[str]:
    pytest.importorskip("psycopg")
    db_name = f"paperintel_test_{uuid.uuid4().hex[:12]}"
    admin = _make_engine(ADMIN_URL).execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f"CREATE DATABASE {db_name} OWNER paperintel"))
    url = f"postgresql+psycopg://paperintel:paperintel@127.0.0.1:5432/{db_name}"

    cfg = Config(REPO_ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    yield url

    cfg2 = Config(REPO_ALEMBIC_INI)
    cfg2.set_main_option("sqlalchemy.url", url)
    command.downgrade(cfg2, "base")
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {db_name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture()
def engine(test_db_url: str) -> Iterator[Engine]:
    engine = _make_engine(test_db_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def connection(engine: Engine) -> Iterator[Connection]:
    """Outer transactional connection; everything a test does is rolled back."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
    finally:
        if trans.is_active:
            trans.rollback()
        conn.close()


@pytest.fixture()
def session(connection: Connection) -> Iterator[Session]:
    factory = sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
