"""Programmatic Alembic runner.

Called from the CLI bootstrap so a fresh data directory becomes a fully
migrated SQLite DB on first invocation — no separate ``alembic upgrade head``
step required during development.

Routine no-op upgrades are silenced (Alembic logger lifted to WARNING)
because they otherwise spam ``Context impl SQLiteImpl`` lines on every
command. When an actual migration runs, the structlog ``migrations.applied``
event captures it.
"""

from __future__ import annotations

import logging

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from inbox_scanner.config import Settings, find_project_root
from inbox_scanner.logging import get_logger

log = get_logger("migrations")


class AlembicConfigMissing(RuntimeError):
    """Raised when alembic.ini cannot be located (e.g. installed wheel without
    bundled migrations). Source-checkout dev runs always find it."""


def _alembic_config(settings: Settings) -> Config:
    root = find_project_root()
    if root is None or not (root / "alembic.ini").is_file():
        raise AlembicConfigMissing(
            "alembic.ini not found. Programmatic migrations currently require "
            "a source checkout; wheel installs aren't supported yet."
        )
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{settings.db_path}")
    return cfg


def _pending_migrations(cfg: Config, db_url: str) -> bool:
    """Cheap check that avoids running ``upgrade`` (and its logging chatter)
    when the DB is already at head."""
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    engine = create_engine(db_url)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        current = ctx.get_current_revision()
    engine.dispose()
    return current != head


def apply_migrations(settings: Settings) -> None:
    """Bring the SQLite DB up to ``head``. No-op when already current."""
    # Silence Alembic's stdlib chatter (``Context impl SQLiteImpl``,
    # ``Will assume non-transactional DDL``, ``Running upgrade ...``). Our
    # structlog events below are the canonical record.
    logging.getLogger("alembic").setLevel(logging.WARNING)

    cfg = _alembic_config(settings)
    db_url = f"sqlite:///{settings.db_path}"
    if not _pending_migrations(cfg, db_url):
        return

    log.info("migrations.applying", db=str(settings.db_path))
    command.upgrade(cfg, "head")
    log.info("migrations.applied")
