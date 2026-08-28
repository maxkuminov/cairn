"""Shared pytest fixtures for the Cairn test suite.

Holds the `cairn_env` fixture and the `seed_collection` helper that every test module used to
copy for itself. Lifted verbatim from `tests/test_panel.py`, which deliberately keeps its own
copies — pytest prefers a module-local fixture over a conftest one, so nothing there changes.

New test modules can rely on the `cairn_env` fixture directly and import the helper:

    from tests.conftest import seed_collection
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    return tmp_path


async def seed_collection(
    root: Path, *, ots_mode: str = "perfile", mode: str = "worm"
) -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s, user_id=uid, name=root.name, root=str(root), mode=mode, ots_mode=ots_mode
        )
        return collection.id
