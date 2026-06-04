"""Scheduler: freshness classification, due-collection selection, daily upgrade, loop smoke, /healthz.

No network: the loop smoke uses an ``ots_mode="none"`` collection so the scanner never invokes ``ots``,
and the upgrade test monkeypatches ``proofs.upgrade_incomplete``. Mirrors the temp-DB fixture from
``tests/test_scanner.py`` / ``tests/test_ots.py``.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_scheduler.py``
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    # Keep the in-process loop out of the TestClient/health tests; we seed runs directly.
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    return tmp_path


async def _make_collection(
    root: Path,
    *,
    mode: str = "worm",
    ots_mode: str = "none",
    cadence: int = 900,
    verify_cadence: int = 604800,
) -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s,
            user_id=uid,
            name=root.name,
            root=str(root),
            mode=mode,
            ots_mode=ots_mode,
            hash_cadence_seconds=cadence,
            verify_cadence_seconds=verify_cadence,
        )
        return collection.id


async def _add_run(cid: int, *, started, finished, result: str) -> None:
    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        s.add(Run(collection_id=cid, started=started, finished=finished, result=result))
        await s.commit()


# --- compute_health -------------------------------------------------------------------------


async def test_compute_health_fresh_is_ok(cairn_env):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.services.scheduler import compute_health
    from src.services.scanner import _utcnow

    root = cairn_env / "fresh"
    root.mkdir()
    cid = await _make_collection(root, cadence=900)  # threshold = max(1800, 900) = 1800s
    now = _utcnow()
    # Successful run 60s ago → well within the freshness window.
    await _add_run(cid, started=now - timedelta(seconds=120), finished=now - timedelta(seconds=60),
                   result="ok")

    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    assert report.status == "ok"
    assert len(report.collections) == 1
    row = report.collections[0]
    assert row.state == "fresh"
    assert row.last_scan_age_seconds is not None and 0 <= row.last_scan_age_seconds < 1800


async def test_compute_health_old_run_is_stale_degraded(cairn_env):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.services.scheduler import compute_health
    from src.services.scanner import _utcnow

    root = cairn_env / "stale"
    root.mkdir()
    cid = await _make_collection(root, cadence=900)  # threshold 1800s
    now = _utcnow()
    # Newest successful run is 2h old → past the 1800s window.
    await _add_run(cid, started=now - timedelta(hours=2), finished=now - timedelta(hours=2),
                   result="ok")

    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    assert report.status == "degraded"
    assert report.collections[0].state == "stale"


async def test_compute_health_new_collection_no_runs_is_pending_ok(cairn_env):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.services.scheduler import compute_health

    root = cairn_env / "brandnew"
    root.mkdir()
    await _make_collection(root, cadence=900)  # created just now, no runs → startup grace

    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    assert report.status == "ok"
    assert report.collections[0].state == "pending"
    assert report.collections[0].last_scan_age_seconds is None


async def test_compute_health_running_run_does_not_count_as_fresh(cairn_env):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.services.scheduler import compute_health
    from src.services.scanner import _utcnow

    # A 'running'/'error' run must not refresh freshness; only ok/partial do.
    root = cairn_env / "running"
    root.mkdir()
    cid = await _make_collection(root, cadence=900)
    now = _utcnow()
    await _add_run(cid, started=now - timedelta(hours=2), finished=None, result="running")

    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    # No ok/partial run + collection is old enough → stale.
    # Force the collection's created_at past the grace so we isolate the run check.
    from src.models.db import Collection

    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        c.created_at = now - timedelta(hours=2)
        await s.commit()
    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    assert report.status == "degraded"
    assert report.collections[0].state == "stale"


# --- due_collections ----------------------------------------------------------------------------


async def test_due_collections_selects_only_past_next_due(cairn_env):
    from src.database import get_sessionmaker
    from src.services.collections import list_collections
    from src.services.scheduler import due_collections

    for name in ("a", "b", "c"):
        d = cairn_env / name
        d.mkdir()
        await _make_collection(d)

    async with get_sessionmaker()() as s:
        collections = await list_collections(s)
    assert [c.name for c in collections] == ["a", "b", "c"]
    ids = [c.id for c in collections]
    now = 1000.0
    # a: no entry (default 0 = due); b: due in the past; c: due in the future.
    next_due = {ids[1]: 900.0, ids[2]: 1100.0}
    selected = due_collections(collections, next_due, now)
    assert [c.name for c in selected] == ["a", "b"]  # stable order, c excluded


def test_due_collections_orders_cheapest_first():
    from types import SimpleNamespace

    from src.services.scheduler import due_collections

    # ids deliberately NOT in cost order, so we prove the sort beats insertion order.
    collections = [SimpleNamespace(id=i) for i in (1, 2, 3, 4)]
    next_due: dict[int, float] = {}  # all due (default 0)
    cost = {
        1: (5_000, 10),   # biggest by bytes
        2: (1_000, 99),   # smallest bytes
        3: (1_000, 5),    # ties 2 on bytes, fewer files → before 2
        4: (3_000, 1),
    }
    ordered = [c.id for c in due_collections(collections, next_due, 1000.0, cost)]
    # ascending (bytes, file_count, id): 3 (1000,5) < 2 (1000,99) < 4 (3000,1) < 1 (5000,10)
    assert ordered == [3, 2, 4, 1]


def test_due_collections_cost_tie_breaks_by_id():
    from types import SimpleNamespace

    from src.services.scheduler import due_collections

    collections = [SimpleNamespace(id=i) for i in (7, 3, 5)]
    cost = {7: (100, 2), 3: (100, 2), 5: (100, 2)}  # fully tied on bytes + count
    ordered = [c.id for c in due_collections(collections, {}, 1000.0, cost)]
    assert ordered == [3, 5, 7]  # deterministic id tie-break


def test_due_collections_without_cost_keeps_id_order():
    from types import SimpleNamespace

    from src.services.scheduler import due_collections

    collections = [SimpleNamespace(id=i) for i in (1, 2, 3)]
    # No cost map → preserve input (id) order; a missing-from-map collection defaults to (0,0).
    ordered = [c.id for c in due_collections(collections, {}, 1000.0)]
    assert ordered == [1, 2, 3]


# --- run_daily_upgrade ----------------------------------------------------------------------


async def test_run_daily_upgrade_records_typed_upgrade_run(cairn_env, monkeypatch):
    """The upgrade pass records its own ``kind='upgrade'`` run (with exact progress) that does NOT
    refresh scan freshness — replacing the old "amend the latest scan run" workaround."""
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import FileEntry, Run
    from src.services import ots, scheduler
    from src.services.scanner import _utcnow

    root = cairn_env / "up"
    root.mkdir()
    cid = await _make_collection(root, ots_mode="perfile")
    now = _utcnow()
    # A prior successful SCAN run is the freshness anchor; the upgrade run must not touch it.
    await _add_run(cid, started=now - timedelta(seconds=60), finished=now - timedelta(seconds=50),
                   result="ok")

    # Seed 4 incomplete proofs with real .ots files on disk (upgrade_incomplete checks existence).
    proof_dir = cairn_env / "proofs" / str(cid)
    proof_dir.mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as s:
        for i in range(4):
            p = proof_dir / f"f{i}.txt.ots"
            p.write_bytes(b"proof")
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=6, sha256=f"{i:064d}",
                status="ok", ots_state="incomplete", ots_path=str(p),
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
        await s.commit()

    # Mock the ots upgrade subprocess: first 3 confirm complete, the 4th stays incomplete.
    calls = {"n": 0}

    def fake_upgrade(path):
        calls["n"] += 1
        return calls["n"] <= 3

    monkeypatch.setattr(ots, "upgrade", fake_upgrade)

    async with get_sessionmaker()() as s:
        total = await scheduler.run_daily_upgrade(s)
    assert total == 3

    async with get_sessionmaker()() as s:
        runs = list(await s.scalars(select(Run).where(Run.collection_id == cid).order_by(Run.id)))
    assert len(runs) == 2  # the scan run + a NEW typed upgrade run
    scan_run, up_run = runs
    assert scan_run.kind == "scan" and scan_run.upgraded == 0  # scan run untouched
    assert up_run.kind == "upgrade"
    assert up_run.total == 4 and up_run.processed == 4 and up_run.upgraded == 3
    assert up_run.result == "ok" and up_run.finished is not None

    # Freshness keys on scan runs only: the collection is fresh from the SCAN run, not the upgrade run.
    async with get_sessionmaker()() as s:
        report = await scheduler.compute_health(s, get_settings())
    assert report.collections[0].state == "fresh"


async def test_run_daily_upgrade_skips_collection_with_no_runs(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services import proofs, scheduler

    root = cairn_env / "norun"
    root.mkdir()
    cid = await _make_collection(root, ots_mode="perfile")

    async def fake_upgrade(session, collection=None, settings=None):
        return {"upgraded": 0, "still_incomplete": 0}

    monkeypatch.setattr(proofs, "upgrade_incomplete", fake_upgrade)

    async with get_sessionmaker()() as s:
        await scheduler.run_daily_upgrade(s)
    async with get_sessionmaker()() as s:
        runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
    assert runs == []  # nothing scanned/stamped → nothing recorded, no run invented


# --- run_due_scans: one failure does not stop the rest --------------------------------------


async def test_run_due_scans_one_failure_does_not_stop_others(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services import scheduler
    from src.services.collections import list_collections

    for name in ("good1", "bad", "good2"):
        d = cairn_env / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        await _make_collection(d, ots_mode="none")

    real_scan = scheduler.scanner.scan_collection

    async def flaky(session, collection, *, deep=False):
        if collection.name == "bad":
            raise RuntimeError("boom")
        return await real_scan(session, collection, deep=deep)

    monkeypatch.setattr(scheduler.scanner, "scan_collection", flaky)

    async with get_sessionmaker()() as s:
        scanned = await scheduler.run_due_scans(s, {}, time.monotonic())
    assert scanned == 2  # good1 + good2 scanned despite bad raising

    async with get_sessionmaker()() as s:
        collections = {c.name: c.id for c in await list_collections(s)}
        for name in ("good1", "good2"):
            runs = list(await s.scalars(select(Run).where(Run.collection_id == collections[name])))
            assert len(runs) == 1 and runs[0].result == "ok"
        bad_runs = list(await s.scalars(select(Run).where(Run.collection_id == collections["bad"])))
        assert bad_runs == []  # failed scan recorded nothing


# --- loop smoke -----------------------------------------------------------------------------


async def test_scheduler_loop_scans_then_stops(cairn_env, monkeypatch):
    monkeypatch.setenv("CAIRN_SCAN_INTERVAL_SECONDS", "1")
    from src.config import get_settings

    get_settings.cache_clear()

    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.scheduler import scheduler_loop

    root = cairn_env / "loop"
    root.mkdir()
    (root / "watched.txt").write_text("data")
    cid = await _make_collection(root, ots_mode="none")  # never invokes ots

    stop_event = asyncio.Event()
    task = asyncio.create_task(scheduler_loop(app=None, stop_event=stop_event))

    # Poll the DB until the startup scan produces a run row (with a timeout).
    async def _wait_for_run() -> int:
        for _ in range(200):  # ~10s max
            async with get_sessionmaker()() as s:
                runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
            if runs:
                return len(runs)
            await asyncio.sleep(0.05)
        raise AssertionError("no run row appeared within timeout")

    try:
        count = await asyncio.wait_for(_wait_for_run(), timeout=15)
        assert count >= 1
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=5)
    assert task.done() and not task.cancelled()

    async with get_sessionmaker()() as s:
        runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
    assert runs and runs[0].result == "ok"


# --- /healthz integration via TestClient ----------------------------------------------------


def test_healthz_fresh_returns_200_ok(cairn_env):
    from fastapi.testclient import TestClient

    from src.main import app

    async def seed() -> None:
        from src.services.scanner import _utcnow

        root = cairn_env / "hfresh"
        root.mkdir()
        cid = await _make_collection(root, cadence=900)
        now = _utcnow()
        await _add_run(cid, started=now - timedelta(seconds=30),
                       finished=now - timedelta(seconds=20), result="ok")

    asyncio.run(seed())
    # Drop the engine built on the throwaway seed loop so TestClient's lifespan rebuilds it
    # on its own event loop (avoids a cross-loop aiosqlite teardown warning).
    from src import database

    database.reset_engine()

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["mode"] == "single"
        assert body["collections"][0]["state"] == "fresh"


def test_healthz_stale_returns_503_degraded(cairn_env):
    from fastapi.testclient import TestClient

    from src.main import app

    async def seed() -> None:
        from src.services.scanner import _utcnow

        root = cairn_env / "hstale"
        root.mkdir()
        cid = await _make_collection(root, cadence=900)
        now = _utcnow()
        await _add_run(cid, started=now - timedelta(hours=2),
                       finished=now - timedelta(hours=2), result="ok")

    asyncio.run(seed())
    # Drop the engine built on the throwaway seed loop so TestClient's lifespan rebuilds it
    # on its own event loop (avoids a cross-loop aiosqlite teardown warning).
    from src import database

    database.reset_engine()

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert any(c["state"] == "stale" for c in body["collections"])


# --- deep verify gating ---------------------------------------------------------------------


def test_deep_owed_truth_table():
    from src.models.db import Collection
    from src.services.scheduler import _deep_owed, _utcnow

    now = _utcnow()
    # Never deep-scanned → owed.
    assert _deep_owed(Collection(verify_cadence_seconds=604800, last_full_scan_at=None), now) is True
    # Deep-scanned recently → not owed.
    assert _deep_owed(
        Collection(verify_cadence_seconds=604800, last_full_scan_at=now - timedelta(days=1)), now
    ) is False
    # Older than the cadence → owed.
    assert _deep_owed(
        Collection(verify_cadence_seconds=604800, last_full_scan_at=now - timedelta(days=8)), now
    ) is True
    # Disabled (0) → never owed, even if never deep-scanned.
    assert _deep_owed(Collection(verify_cadence_seconds=0, last_full_scan_at=None), now) is False


async def _record_scan_kwargs(monkeypatch):
    """Replace scan_collection with a recorder; returns the list it appends (collection_id, deep) to."""
    from src.services import scheduler

    calls: list[tuple[int, bool]] = []

    async def rec(session, collection, *, deep=False):
        calls.append((collection.id, deep))

    monkeypatch.setattr(scheduler.scanner, "scan_collection", rec)
    return calls


async def test_run_due_scans_picks_deep_when_owed(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler

    root = cairn_env / "deepc"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cid = await _make_collection(root, verify_cadence=604800)  # last_full_scan_at=None → owed

    calls = await _record_scan_kwargs(monkeypatch)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())
    assert calls == [(cid, True)]

    async with get_sessionmaker()() as s:
        assert (await s.get(Collection, cid)).last_full_scan_at is not None


async def test_run_due_scans_quick_when_not_owed(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler
    from src.services.scheduler import _utcnow

    root = cairn_env / "freshc"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cid = await _make_collection(root, verify_cadence=604800)
    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        c.last_full_scan_at = _utcnow()  # just deep-scanned
        await s.commit()

    calls = await _record_scan_kwargs(monkeypatch)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())
    assert calls == [(cid, False)]


async def test_run_due_scans_deep_disabled(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.services import scheduler

    root = cairn_env / "offc"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cid = await _make_collection(root, verify_cadence=0)  # disabled, never deep-scanned

    calls = await _record_scan_kwargs(monkeypatch)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())
    assert calls == [(cid, False)]


async def test_deep_not_persisted_on_failure(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler

    root = cairn_env / "failc"
    root.mkdir()
    (root / "f.txt").write_text("x")
    cid = await _make_collection(root, verify_cadence=604800)

    async def boom(session, collection, *, deep=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(scheduler.scanner, "scan_collection", boom)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())

    async with get_sessionmaker()() as s:
        assert (await s.get(Collection, cid)).last_full_scan_at is None  # deep clock not advanced


async def test_run_due_scans_one_deep_per_tick(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.services import scheduler

    for name in ("d1", "d2", "d3"):
        d = cairn_env / name
        d.mkdir()
        (d / "f.txt").write_text(name)
        await _make_collection(d, verify_cadence=604800)  # all owed (last_full_scan_at=None)

    calls = await _record_scan_kwargs(monkeypatch)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())

    deeps = [deep for _cid, deep in calls]
    assert len(deeps) == 3
    assert deeps.count(True) == 1  # exactly one deep pass this tick
    assert deeps.count(False) == 2  # the rest fall back to quick


async def _add_file(cid: int, relpath: str, size: int, *, status: str = "ok") -> None:
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        s.add(FileEntry(collection_id=cid, relpath=relpath, size=size, status=status))
        await s.commit()


async def test_run_due_scans_orders_cheapest_first(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler
    from src.services.scheduler import _utcnow

    # Insert big → small → mid so insertion (id) order is NOT cost order: the sort must win.
    sizes = {"big": 1000, "small": 10, "mid": 100}
    cids = {}
    for name in ("big", "small", "mid"):
        d = cairn_env / name
        d.mkdir()
        cids[name] = await _make_collection(d, verify_cadence=604800)
        await _add_file(cids[name], "f.bin", sizes[name])

    # Only the largest collection is owed a deep pass; the others were just deep-scanned. Proves the
    # deep slot is awarded by owed-ness, not order — the big collection still goes deep though it's last.
    async with get_sessionmaker()() as s:
        for name in ("small", "mid"):
            (await s.get(Collection, cids[name])).last_full_scan_at = _utcnow()
        await s.commit()

    calls = await _record_scan_kwargs(monkeypatch)
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())

    order = [cid for cid, _deep in calls]
    assert order == [cids["small"], cids["mid"], cids["big"]]  # ascending by total bytes
    deep_by_id = dict(calls)
    assert deep_by_id[cids["big"]] is True  # large, owed → the single deep slot
    assert deep_by_id[cids["small"]] is False
    assert deep_by_id[cids["mid"]] is False
