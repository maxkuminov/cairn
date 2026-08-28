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
    # Every batch drain is also the claim's heartbeat: a scan that is progressing must never look
    # abandoned to the startup reaper, which would revoke a live claim (design D10).
    assert first.heartbeat_at is not None and second.heartbeat_at is not None


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
        from datetime import timedelta

        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.scheduler import RUN_HEARTBEAT_TIMEOUT_SECONDS, reap_orphaned_runs

        # One running run per collection across two collections — the partial unique index
        # uq_runs_one_running_per_collection (issue #4) now forbids two running runs on one collection, so
        # the reaper's "multiple orphans" case is exercised across collections.
        (root2 := root.parent / "c2").mkdir()
        cid = await _seed_collection(root)
        cid2 = await _seed_collection(root2)
        dead = _utcnow() - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60)
        async with get_sessionmaker()() as s:
            # Reported progress once, then stopped: the ordinary crashed-scan orphan.
            s.add(Run(collection_id=cid, kind="scan", result="running",
                      started=dead, heartbeat_at=dead))
            # Never reported at all (a pre-0011 row, or a claim that died immediately): `started`
            # is the fallback liveness reference.
            s.add(Run(collection_id=cid2, kind="stamp", result="running",
                      started=dead, heartbeat_at=None))
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


def test_reaper_leaves_a_live_claim_alone(cairn_env):
    """Startup must not revoke the claim of a live SECOND process (design D10).

    The claim is cross-process and cross-host: a cron `cairn stamp`/`cairn upgrade` can hold a
    collection while the web app restarts. Bulk-reaping every `running` row on startup would clear
    that claim and let the scheduler or panel start a second writer over the same proofs — the
    concurrent check-then-act the claim exists to prevent. Liveness decides, not startup: a run that
    is still reporting progress keeps its claim however long it has been running.
    """
    root = cairn_env / "live"
    root.mkdir()

    async def go():
        from datetime import timedelta

        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.scheduler import RUN_HEARTBEAT_TIMEOUT_SECONDS, reap_orphaned_runs

        cid = await _seed_collection(root)
        async with get_sessionmaker()() as s:
            # Hours old — a multi-hour upgrade pass — but it reported progress moments ago.
            s.add(
                Run(
                    collection_id=cid,
                    kind="upgrade",
                    result="running",
                    started=_utcnow() - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS * 20),
                    heartbeat_at=_utcnow(),
                )
            )
            await s.commit()
        async with get_sessionmaker()() as s:
            reaped = await reap_orphaned_runs(s)
        async with get_sessionmaker()() as s:
            run = await s.scalar(select(Run).where(Run.collection_id == cid))
        return reaped, run

    reaped, run = _run_check(go)
    assert reaped == 0
    assert run.result == "running" and run.finished is None



def test_a_concurrent_heartbeat_beats_the_reaping_update(cairn_env, monkeypatch):
    """The reaper's selection and its UPDATE are separate statements — the holder may revive between.

    Same race the claim path guards (`test_a_concurrent_heartbeat_beats_the_reclaiming_update`), on
    the fleet-wide sweep: the run reads stale, then the live process that owns it commits a heartbeat
    from another connection before the reap lands. The UPDATE re-asserts `result='running'` AND
    `coalesce(heartbeat_at, started) <= cutoff`, so it matches zero rows and the lease survives —
    reaping it would have admitted a second writer over that collection's proofs (design D10).
    """
    root = cairn_env / "reap-race"
    root.mkdir()

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services import scheduler as scheduler_svc

        cid = await _seed_collection(root)
        dead = _utcnow() - timedelta(
            seconds=scheduler_svc.RUN_HEARTBEAT_TIMEOUT_SECONDS + 60
        )
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="upgrade", result="running",
                      started=dead, heartbeat_at=dead))
            await s.commit()

        real_stale_run_ids = scheduler_svc._stale_run_ids

        async def racing_stale_run_ids(session, cutoff):
            stale, live = await real_stale_run_ids(session, cutoff)
            if stale:
                # The "dead" holder was not dead: it reports progress before we can reap it.
                async with get_sessionmaker()() as other:
                    await other.execute(
                        sql_update(Run)
                        .where(Run.id.in_(stale))
                        .values(heartbeat_at=_utcnow())
                    )
                    await other.commit()
            return stale, live

        monkeypatch.setattr(scheduler_svc, "_stale_run_ids", racing_stale_run_ids)
        async with get_sessionmaker()() as s:
            reaped = await scheduler_svc.reap_orphaned_runs(s)
        async with get_sessionmaker()() as s:
            runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        return reaped, runs

    reaped, runs = _run_check(go)
    assert reaped == 0  # the guarded UPDATE lost the race to the heartbeat
    assert len(runs) == 1
    assert runs[0].result == "running" and runs[0].finished is None

def test_claiming_a_run_stamps_its_heartbeat(cairn_env):
    """A lease with no liveness signal is indistinguishable from a corpse, so the claim starts one."""
    root = cairn_env / "claimed"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.collections import claim_run

        cid = await _seed_collection(root)
        async with get_sessionmaker()() as s:
            claimed = await claim_run(s, Run(collection_id=cid, kind="stamp", result="running"))
            assert claimed is not None
        async with get_sessionmaker()() as s:
            return await s.scalar(select(Run).where(Run.collection_id == cid))

    run = _run_check(go)
    assert run.heartbeat_at is not None


def test_claim_reclaims_an_abandoned_lease_without_a_reaper(cairn_env):
    """A SIGKILLed CLI op must not wedge its collection until the service restarts.

    The lease was reaped only at web startup, so an orphan created afterwards (`cairn stamp` killed
    mid-flight) kept satisfying the one-running-run-per-collection index forever — and a CLI-only
    deployment, which runs no scheduler and no web process, had no reclamation path at all. The
    claim itself now reconciles: a blocked claim whose blocker has stopped heartbeating marks that
    blocker `interrupted` and retries once. No reaper is called anywhere in this test.
    """
    root = cairn_env / "wedged"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.collections import RUN_HEARTBEAT_TIMEOUT_SECONDS, claim_run

        cid = await _seed_collection(root)
        dead = _utcnow() - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60)
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="stamp", result="running",
                      started=dead, heartbeat_at=dead))
            await s.commit()
        async with get_sessionmaker()() as s:
            claimed = await claim_run(
                s, Run(collection_id=cid, kind="scan", result="running")
            )
            claimed_id = claimed.id if claimed is not None else None
        async with get_sessionmaker()() as s:
            runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        return claimed_id, runs

    claimed_id, runs = _run_check(go)
    assert claimed_id is not None  # the new op got the slot
    abandoned = [r for r in runs if r.kind == "stamp"]
    assert len(abandoned) == 1
    # Reclaimed with the reaper's terminal semantics: 'interrupted' (not 'error'), finished stamped.
    assert abandoned[0].result == "interrupted" and abandoned[0].finished is not None
    assert [r.id for r in runs if r.result == "running"] == [claimed_id]


def test_claim_refuses_a_lease_that_is_still_heartbeating(cairn_env):
    """Liveness decides, never impatience: a live claim still refuses the second op (design D10).

    The reclamation path must not become a way for a second writer to walk into a collection a live
    `cairn upgrade` is grinding through — that is the concurrent check-then-act the claim exists to
    prevent.
    """
    root = cairn_env / "live-claim"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.collections import RUN_HEARTBEAT_TIMEOUT_SECONDS, claim_run

        cid = await _seed_collection(root)
        async with get_sessionmaker()() as s:
            # Hours old, but reported progress moments ago — a legitimate long CLI upgrade.
            s.add(Run(
                collection_id=cid, kind="upgrade", result="running",
                started=_utcnow() - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS * 20),
                heartbeat_at=_utcnow(),
            ))
            await s.commit()
        async with get_sessionmaker()() as s:
            claimed = await claim_run(
                s, Run(collection_id=cid, kind="scan", result="running")
            )
        async with get_sessionmaker()() as s:
            runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        return claimed, runs

    claimed, runs = _run_check(go)
    assert claimed is None  # refused, exactly as before the reclamation path existed
    assert len(runs) == 1 and runs[0].result == "running" and runs[0].finished is None


def test_the_operation_gate_releases_an_abandoned_claim(cairn_env):
    """The advisory pre-check in front of every operation must reclaim too, not just `claim_run`.

    Panel routes, `cairn scan`, `cairn upgrade` and the scheduler pass all gate on a cheap
    `active_run` read that answers BEFORE `claim_run` is reached; if that gate kept reporting a dead
    holder, reclaiming inside the claim would never be given the chance. `blocking_run` is that gate.
    `active_run` itself stays a pure read — the status badge must never write.
    """
    root = cairn_env / "gate"
    root.mkdir()

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services.collections import (
            RUN_HEARTBEAT_TIMEOUT_SECONDS,
            active_run,
            blocking_run,
        )

        cid = await _seed_collection(root)
        dead = _utcnow() - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60)
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="stamp", result="running",
                      started=dead, heartbeat_at=dead))
            await s.commit()
        async with get_sessionmaker()() as s:
            seen_by_display = await active_run(s, cid)
        async with get_sessionmaker()() as s:
            blocker = await blocking_run(s, cid)
        async with get_sessionmaker()() as s:
            run = await s.scalar(select(Run).where(Run.collection_id == cid))
        return seen_by_display is not None, blocker, run

    display_saw_it, blocker, run = _run_check(go)
    assert display_saw_it  # the plain read reports what the table says, and writes nothing
    assert blocker is None  # the gate lets the new operation through
    assert run.result == "interrupted" and run.finished is not None


def test_a_concurrent_heartbeat_beats_the_reclaiming_update(cairn_env, monkeypatch):
    """The read and the guarded UPDATE are separate statements — the holder may revive between them.

    Simulates that race by heartbeating the blocker (from another connection, as the live process
    would) after the staleness read has already selected it. The UPDATE re-asserts the full stale
    condition in its WHERE, so it matches zero rows, the reclamation reports failure, and the claim
    refuses. The claim is never taken from a process that is still working.
    """
    root = cairn_env / "race"
    root.mkdir()

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Run
        from src.services import collections as collections_svc

        cid = await _seed_collection(root)
        dead = _utcnow() - timedelta(
            seconds=collections_svc.RUN_HEARTBEAT_TIMEOUT_SECONDS + 60
        )
        async with get_sessionmaker()() as s:
            s.add(Run(collection_id=cid, kind="stamp", result="running",
                      started=dead, heartbeat_at=dead))
            await s.commit()

        real_stale_claim_id = collections_svc._stale_claim_id

        async def racing_stale_claim_id(session, collection_id, cutoff):
            stale_id = await real_stale_claim_id(session, collection_id, cutoff)
            if stale_id is not None:
                # The "dead" holder was not dead: it reports progress before we can revoke it.
                async with get_sessionmaker()() as other:
                    await other.execute(
                        sql_update(Run)
                        .where(Run.id == stale_id)
                        .values(heartbeat_at=_utcnow())
                    )
                    await other.commit()
            return stale_id

        monkeypatch.setattr(
            collections_svc, "_stale_claim_id", racing_stale_claim_id
        )
        async with get_sessionmaker()() as s:
            claimed = await collections_svc.claim_run(
                s, Run(collection_id=cid, kind="scan", result="running")
            )
        async with get_sessionmaker()() as s:
            runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        return claimed, runs

    claimed, runs = _run_check(go)
    assert claimed is None  # the guarded UPDATE lost the race, so the claim refuses
    assert len(runs) == 1
    assert runs[0].result == "running" and runs[0].finished is None


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
