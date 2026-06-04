"""Foundation smoke test: schema, pragmas, healthz.

Run from the repo root with ``src`` importable, e.g.:

    PYTHONPATH=. pytest tests/test_foundation_smoke.py
"""

from __future__ import annotations

import asyncio
import sqlite3

import sqlalchemy as sa


def test_foundation_boots_and_serves(tmp_path, monkeypatch):
    db_path = tmp_path / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    monkeypatch.setenv("CAIRN_AUTO_MIGRATE", "1")

    # Pick up the patched environment.
    from src.config import get_settings
    from src import database

    get_settings.cache_clear()
    database.reset_engine()

    from fastapi.testclient import TestClient
    from src.main import app

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["mode"] == "single"
        assert payload["status"] == "ok"

    # journal_mode is persisted in the DB header — a fresh connection proves the listener ran.
    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        tables = {row[0] for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"users", "collections", "files", "runs", "events"} <= tables
        # Implicit single-user bootstrap created exactly one owner.
        assert raw.execute("SELECT count(*) FROM users").fetchone()[0] == 1
    finally:
        raw.close()

    # foreign_keys is per-connection — verify our engine's connections enable it.
    # Build a fresh engine inside the new event loop to avoid cross-loop binding.
    database.reset_engine()

    async def _fk_on() -> int:
        engine = database.get_engine()
        async with engine.connect() as conn:
            return (await conn.execute(sa.text("PRAGMA foreign_keys"))).scalar()

    assert asyncio.run(_fk_on()) == 1
