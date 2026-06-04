"""Async SQLite datastore: engine, per-connection pragmas, session dependency, bootstrap.

WAL mode + enforced foreign keys are set on every connection via a ``connect`` event listener
(SQLite defaults ``foreign_keys`` OFF and pragmas are connection-scoped, so the listener is the
only reliable place). The scanner is the single writer; WAL keeps panel reads concurrent.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings
from .models.db import User

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, future=True)
        if _engine.dialect.name == "sqlite":
            event.listen(_engine.sync_engine, "connect", _configure_sqlite)
        _sessionmaker = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def reset_engine() -> None:
    """Drop cached engine/sessionmaker (used by tests that swap the database URL)."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an :class:`AsyncSession`."""
    async with get_sessionmaker()() as session:
        yield session


async def ping() -> bool:
    """Return True if the datastore answers a trivial query."""
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _sqlite_file_from_url(url: str) -> Path | None:
    if "sqlite" not in url:
        return None
    _, _, rest = url.partition(":///")
    rest = rest.strip()
    if not rest or rest == ":memory:":
        return None
    return Path(rest)


def ensure_dirs() -> None:
    """Create the data dir (for the SQLite file) and the proof store if missing."""
    settings = get_settings()
    db_file = _sqlite_file_from_url(settings.database_url)
    if db_file is not None:
        db_file.parent.mkdir(parents=True, exist_ok=True)
    Path(settings.proof_store_path).mkdir(parents=True, exist_ok=True)


def run_migrations() -> None:
    """Run ``alembic upgrade head`` synchronously (safe from any thread).

    env.py derives a synchronous SQLite URL from the configured ``database_url``. Alembic uses a
    sync connection that doesn't run the app's pragma listener, so we set WAL persistently here
    (it is stored in the DB header) — that way ``cairn init`` leaves the DB in WAL even before the
    async app first connects.
    """
    import sqlite3

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(cfg, "head")

    db_file = _sqlite_file_from_url(get_settings().database_url)
    if db_file is not None and db_file.exists():
        conn = sqlite3.connect(db_file)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()


async def ensure_implicit_user(session: AsyncSession) -> None:
    """In single-user mode, ensure one implicit owner row exists (idempotent)."""
    settings = get_settings()
    if settings.auth_mode != "single":
        return
    existing = await session.scalar(select(User).limit(1))
    if existing is None:
        session.add(User(username=settings.single_user, is_admin=True, is_active=True))
        await session.commit()
