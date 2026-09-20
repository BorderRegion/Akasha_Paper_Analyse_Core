"""Alembic environment (PaperIntel).

Schema changes happen ONLY through Alembic migrations (spec doc 02 §12:
"manually modify production DB schema outside Alembic" is forbidden).

The database URL is resolved from:
1. ``-x database_url=...`` alembic extra arg, else
2. ``sqlalchemy.url`` in alembic.ini / ``Config.set_main_option``, else
3. ``DATABASE_URL`` environment variable, else
4. paperintel settings (config file / defaults).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from paperintel.config.settings import load_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    extra = context.get_x_argument(as_dictionary=True)
    if "database_url" in extra:
        return extra["database_url"]
    # Standard Alembic behavior: honor sqlalchemy.url from the ini file or
    # Config.set_main_option() (used by the test harness for disposable DBs).
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url
    return load_config().database.url


def _resolve_metadata():
    """Bind model metadata once P01 introduces paperintel.database.models."""
    try:
        from paperintel.database.models import Base  # type: ignore[attr-defined]

        return Base.metadata
    except ImportError:
        # P00 scaffold: no ORM models exist yet. Migrations may still run
        # (empty head); importing this env must not fail.
        return None


target_metadata = _resolve_metadata()


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_bindings=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
