"""Folder-tree browser + typed, progress-bearing runs (add-folder-tree-and-scan-progress).

Covers the parts not already exercised by tests/test_panel.py and tests/test_scheduler.py:
- migration 0006 round-trip + backfill (subprocess alembic on a scratch DB),
- browse_tree aggregation / prefix scoping (no full-set materialization),
- scan run progress (growing processed + total estimate from the prior scan; first scan NULL),
- a stamp/upgrade run never refreshing scan freshness,
- the orphaned-run reaper,
- the tree + op-status route fragments.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_folder_tree_and_progress.py``
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_collection(root: Path, *, ots_mode: str = "none", mode: str = "worm") -> int:
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


async def _add_files(collection_id: int, paths: list[tuple[str, str]]) -> None:
    """Insert tracked rows directly: each (relpath, status)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = _utcnow()
    async with get_sessionmaker()() as s:
        for rel, st in paths:
            s.add(FileEntry(
                collection_id=collection_id, relpath=rel, size=10, sha256="a" * 64,
                status=st, first_seen=now, last_checked=now,
            ))
        await s.commit()


def _run_check(coro_factory):
    """Run a post-test DB read on a fresh in-loop engine (mirrors tests/test_panel.py)."""
    from src import database

    database.reset_engine()

    async def _wrapped():
        try:
            return await coro_factory()
        finally:
            await database.get_engine().dispose()

    return asyncio.run(_wrapped())


# --- 7.1 migration round-trip ---------------------------------------------------------------


def test_migration_0006_round_trip_and_backfill(tmp_path):
    """alembic upgrade adds kind/processed/total + the CHECK and backfills kind='scan'; downgrade
    drops them again (the runs table otherwise unchanged)."""
    import sqlite3

    db = tmp_path / "scratch.db"
    env = dict(os.environ, CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{db}")
    repo = Path(__file__).resolve().parent.parent

    def alembic(*args):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, cwd=repo,
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        return r

    # Upgrade to 0005, seed a pre-existing run, then upgrade to head to prove the backfill.
    # NB: at revision 0005 the schema still uses the pre-rename names (the table is `corpora` and
    # the FK column is `corpus_id`); the corpus→collection rename only lands in 0009.
    alembic("upgrade", "0005_rename_detection")
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO corpora (user_id,name,root,mode,hash_cadence_seconds,verify_cadence_seconds,"
        "ots_mode,exclude_globs_json,alert_json,created_at) "
        "VALUES (1,'c','/tmp','worm',900,604800,'none','[]','{}','2026-01-01')"
    )
    con.execute(
        "INSERT INTO runs (corpus_id,started,added,modified,missing,moved,stamped,upgraded,deep,"
        "result) VALUES (1,'2026-01-01',0,0,0,0,0,0,0,'ok')"
    )
    con.commit()
    con.close()

    alembic("upgrade", "head")
    con = sqlite3.connect(db)
    cols = {row[1]: row for row in con.execute("PRAGMA table_info(runs)")}
    assert {"kind", "processed", "total"} <= cols.keys()
    assert cols["kind"][3] == 1  # NOT NULL
    assert cols["total"][3] == 0  # nullable
    assert con.execute("SELECT kind, processed, total FROM runs").fetchone() == ("scan", 0, None)
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='runs'").fetchone()[0]
    assert "ck_runs_kind" in ddl and "ck_runs_result" in ddl
    con.close()

    alembic("downgrade", "0005_rename_detection")
    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    assert not ({"kind", "processed", "total"} & cols)
    con.close()


# --- 7.2 browse_tree -------------------------------------------------------------------------


def test_browse_tree_levels_counts_and_issue_rollup(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.services import collections as cs

        cid = await _seed_collection(root)
        await _add_files(cid, [
            ("root.txt", "ok"),
            ("2024/jan/a.jpg", "ok"),
            ("2024/jan/b.jpg", "missing"),
            ("2024/feb/c.jpg", "modified"),
            ("2023/x.jpg", "ok"),
            ("2024/top.txt", "ok"),
        ])
        async with get_sessionmaker()() as s:
            root_lvl = {f.name: f for f in await cs.browse_tree(s, cid, "")}
            assert set(root_lvl) == {"2024", "2023"}
            assert (root_lvl["2024"].file_count, root_lvl["2024"].issue_count) == (4, 2)
            assert root_lvl["2024"].prefix == "2024/"
            assert (root_lvl["2023"].file_count, root_lvl["2023"].issue_count) == (1, 0)
            # Root immediate files (no '/' in remainder): root.txt only.
            rows, total = await cs.query_files(s, cid, prefix="", page=0, page_size=50)
            assert total == 1 and rows[0].relpath == "root.txt"

            sub = {f.name: f for f in await cs.browse_tree(s, cid, "2024/")}
            assert set(sub) == {"jan", "feb"}
            assert (sub["jan"].file_count, sub["jan"].issue_count) == (2, 1)
            assert (sub["feb"].file_count, sub["feb"].issue_count) == (1, 1)
            rows, total = await cs.query_files(s, cid, prefix="2024/", page=0, page_size=50)
            assert total == 1 and rows[0].relpath == "2024/top.txt"

    _run_check(go)


def test_browse_tree_prefix_escapes_like_wildcards(cairn_env):
    """A folder whose name contains a LIKE wildcard ('%','_') must be matched literally."""
    root = cairn_env / "c"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.services import collections as cs

        cid = await _seed_collection(root)
        await _add_files(cid, [("a%b/x.jpg", "ok"), ("axb/y.jpg", "ok"), ("other/z.jpg", "ok")])
        async with get_sessionmaker()() as s:
            rows, total = await cs.query_files(s, cid, prefix="a%b/", page=0, page_size=50)
            assert total == 1 and rows[0].relpath == "a%b/x.jpg"

    _run_check(go)


# --- 7.3 scan run progress -------------------------------------------------------------------


def test_scan_progress_processed_and_total_estimate(cairn_env):
    """First scan → total NULL (no baseline); second scan → total = prior scan's processed; both
    record a processed count == files walked."""
    root = cairn_env / "files"
    root.mkdir()
    for i in range(7):
        (root / f"f{i}.txt").write_text(f"data-{i}")

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Collection, Run
        from src.services import scanner

        cid = await _seed_collection(root, ots_mode="none")
        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            await scanner.scan_collection(s, collection)
            await scanner.scan_collection(s, collection)
            runs = list(await s.scalars(
                select(Run).where(Run.collection_id == cid).order_by(Run.id)
            ))
        return runs

    runs = _run_check(go)
    assert len(runs) == 2
    first, second = runs
    assert first.kind == "scan" and first.total is None  # no baseline
    assert first.processed == 7 and first.result == "ok"
    assert second.total == 7  # estimate = prior scan's processed
    assert second.processed == 7


# --- 7.4 stamp/upgrade do not refresh freshness ---------------------------------------------


def test_stamp_or_upgrade_run_does_not_refresh_freshness(cairn_env):
    """A collection stale on its scan cadence stays stale despite a recent stamp/upgrade run."""
    root = cairn_env / "stale"
    root.mkdir()

    async def go():
        from src.config import get_settings
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services import scheduler

        cid = await _seed_collection(root, ots_mode="perfile")
        now = _utcnow()
        async with get_sessionmaker()() as s:
            # An old scan run (well past the freshness window) → stale.
            s.add(Run(collection_id=cid, kind="scan", started=now - timedelta(days=30),
                      finished=now - timedelta(days=30), result="ok"))
            # A *recent* stamp + upgrade run that must NOT refresh freshness.
            s.add(Run(collection_id=cid, kind="stamp", started=now - timedelta(seconds=5),
                      finished=now - timedelta(seconds=4), result="ok"))
            s.add(Run(collection_id=cid, kind="upgrade", started=now - timedelta(seconds=3),
                      finished=now - timedelta(seconds=2), result="ok"))
            await s.commit()
            report = await scheduler.compute_health(s, get_settings())
        return report

    report = _run_check(go)
    assert report.status == "degraded"
    assert report.collections[0].state == "stale"


# --- 7.5 orphaned-run reaper -----------------------------------------------------------------


def test_reaper_marks_orphaned_running_runs_interrupted(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.scheduler import reap_orphaned_runs

        # One running run per collection across two collections — the partial unique index
        # uq_runs_one_running_per_collection (issue #4) now forbids two running runs on one collection, so
        # the reaper's "multiple orphans" case is exercised across collections.
        (root2 := root.parent / "c2").mkdir()
        cid = await _seed_collection(root)
        cid2 = await _seed_collection(root2)
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="scan", result="running"))
            s.add(Run(collection_id=cid2, kind="stamp", result="running"))
            s.add(Run(collection_id=cid, kind="scan", result="ok", finished=_utcnow()))
            await s.commit()
        async with get_sessionmaker()() as s:
            reaped = await reap_orphaned_runs(s)
        async with get_sessionmaker()() as s:
            runs = list(await s.scalars(select(Run).where(Run.collection_id.in_((cid, cid2)))))
        return reaped, runs

    reaped, runs = _run_check(go)
    assert reaped == 2
    assert not any(r.result == "running" for r in runs)
    # Reaped runs become 'interrupted' (distinct from a genuine 'error'), with a finished timestamp.
    assert sum(r.result == "interrupted" for r in runs) == 2
    for r in runs:
        if r.result == "interrupted":
            assert r.finished is not None  # reaped runs get a finished timestamp


# --- 7.6 tree + op-status route fragments ---------------------------------------------------


def _csrf(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m
    return m.group(1)


def _make_client(cairn_env, seed_coro):
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()
    return TestClient(app)


def test_tree_endpoint_returns_one_level(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="none")
        await _add_files(cid, [
            ("2024/jan/a.jpg", "ok"), ("2024/feb/b.jpg", "missing"), ("top.txt", "ok"),
        ])
        return cid

    with _make_client(cairn_env, seed) as client:
        # Root level: the "2024" folder + the top-level file, NOT the nested files.
        r = client.get("/collection/1/tree?prefix=")
        assert r.status_code == 200
        assert "2024" in r.text and "top.txt" in r.text
        assert "a.jpg" not in r.text and "b.jpg" not in r.text
        # Drill into 2024/: its subfolders, still not the leaf files.
        r = client.get("/collection/1/tree?prefix=2024/")
        assert r.status_code == 200
        assert "jan" in r.text and "feb" in r.text
        assert "a.jpg" not in r.text


def test_collection_detail_defaults_to_tree_view(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(root, ots_mode="none")) as client:
        r = client.get("/collection/1")
        assert r.status_code == 200
        assert 'data-view="tree"' in r.text  # tree is the default browser view
        assert "browser-tree" in r.text and "browser-list" in r.text


def test_op_status_idle_sends_refresh_running_polls(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import Run

        cid = await _seed_collection(root, ots_mode="none")
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="scan", result="ok", finished=_utcnow(),
                      processed=10, total=10))
            await s.commit()
        return cid

    with _make_client(cairn_env, seed) as client:
        # No op running → static pill, no poll trigger.
        r = client.get("/collection/1/op-status")
        assert r.status_code == 200
        assert "every 4s" not in r.text
        # First poll of a just-started op (was_running unset) must NOT refresh — that would reload
        # the page and cancel polling before the running run is committed (issue #10).
        assert r.headers.get("HX-Refresh") is None
        # The running badge polls with was_running=1; idle then signals the running→idle transition.
        r = client.get("/collection/1/op-status?was_running=1")
        assert r.status_code == 200
        assert "every 4s" not in r.text
        assert r.headers.get("HX-Refresh") == "true"
