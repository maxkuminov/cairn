"""Alembic environment (synchronous over the SQLite file).

The app runtime uses async aiosqlite, but migrations are simpler and context-agnostic when run
with a plain synchronous SQLite engine pointed at the same file. The URL is derived from
``CAIRN_DATABASE_URL`` by stripping the ``+aiosqlite`` driver suffix.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event, pool

# Make ``src`` importable when alembic runs from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.db import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_url() -> str:
    url = os.environ.get("CAIRN_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    return url.replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_sync_url(), poolclass=pool.NullPool)

    # Enforce foreign keys at the DBAPI level on connect — this fires before any transaction is
    # opened, so it is not a no-op and (unlike an explicit exec) does not start a SQLAlchemy
    # transaction that would roll back Alembic's version stamp on close.
    @event.listens_for(connectable, "connect")
    def _enable_fk(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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
