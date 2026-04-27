"""Alembic environment.

Reads the SQLite path from ``inbox_scanner.config.load_settings()`` so
migrations always run against the user's data directory. Override the data
directory with ``INBOX_SCANNER_DATA_DIR`` (handy for tests / CI).
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context

from inbox_scanner.config import load_settings
from inbox_scanner.models import Base

# Note: alembic.ini ships with a default ``[loggers]`` / ``[handlers]`` block
# but we deliberately don't ``fileConfig`` it. Logging is configured by
# ``inbox_scanner.logging.configure_logging`` (structlog), and reapplying
# alembic's stdlib config here would clobber the level filters we set in
# ``inbox_scanner.migrations.apply_migrations``.

config = context.config

target_metadata = Base.metadata


def _resolve_url() -> str:
    override = os.environ.get("INBOX_SCANNER_DATA_DIR")
    settings = load_settings(Path(override)) if override else load_settings()
    return f"sqlite:///{settings.db_path}"


# Honor a pre-set URL (e.g. from programmatic ``apply_migrations``) so the CLI
# bootstrap can pin the URL to the already-loaded settings without re-walking
# the project root from inside env.py. Fall back to env-driven discovery when
# Alembic is invoked from the shell.
if not config.get_main_option("sqlalchemy.url", default=""):
    config.set_main_option("sqlalchemy.url", _resolve_url())


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
