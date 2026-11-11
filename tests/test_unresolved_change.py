"""An unresolved change stays an issue (unify-issue-model, design D12).

Acceptance criteria: requirement "An unresolved change stays an issue" in
``openspec/changes/unify-issue-model/specs/integrity-scanning/spec.md``. A file that carries an
open ``modified``/``restored_changed`` event must never be recorded ``ok`` (or ``new``) by a scan —
not in its final state, and not in any batch it commits on the way there.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_unresolved_change.py``
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from tests.conftest import seed_collection


async def _scan(cid: int, *, deep: bool = False):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    async with get_sessionmaker()() as s:
        return await scan_collection(s, await s.get(Collection, cid), deep=deep)


async def _file(cid: int, relpath: str):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == relpath)
        )


async def _events(cid: int, *, kind: str | None = None, file_id: int | None = None):
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        stmt = select(Event).where(Event.collection_id == cid).order_by(Event.id)
        if kind is not None:
            stmt = stmt.where(Event.kind == kind)
        if file_id is not None:
            stmt = stmt.where(Event.file_id == file_id)
        return list(await s.scalars(stmt))


async def _event_count(cid: int) -> int:
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(func.count()).select_from(Event).where(Event.collection_id == cid)
        )


async def _add_open_event(cid: int, file_id: int, kind: str = "modified") -> None:
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        s.add(
            Event(
                collection_id=cid,
                file_id=file_id,
                kind=kind,
                detected_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()


async def _set_status(file_id: int, status: str) -> None:
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        (await s.get(FileEntry, file_id)).status = status
        await s.commit()


@pytest.fixture
def alerts(monkeypatch):
    """Capture every alert dispatch (the scanner imports `dispatch` at call time)."""
    from src.notify import dispatch as dispatch_mod

    sent: list = []

    async def _capture(alert, collection, settings):
        sent.append(alert)

    monkeypatch.setattr(dispatch_mod, "dispatch", _capture)
    return sent


async def _changed_then_missing(cairn_env, name: str, changed: str):
    """worm collection with `a.txt` found changed (unaccepted) and then found missing."""
    root = cairn_env / name
    root.mkdir()
    (root / "a.txt").write_text("original")
    (root / "b.txt").write_text("bystander")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    await _scan(cid)
    (root / "a.txt").write_text(changed)
    await _scan(cid)
    assert (await _file(cid, "a.txt")).status == "modified"
    (root / "a.txt").unlink()
    await _scan(cid)
    assert (await _file(cid, "a.txt")).status == "missing"
    return root, cid


@pytest.mark.asyncio
async def test_changed_file_deleted_and_restored_is_still_changed(cairn_env, alerts):
    root, cid = await _changed_then_missing(cairn_env, "restore", "changed-bytes-longer")
    fid = (await _file(cid, "a.txt")).id

    (root / "a.txt").write_text("changed-bytes-longer")  # the bytes recorded when it changed
    alerts.clear()
    summary = await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified"
    assert summary.reflagged == 1
    assert summary.restored == 1 and summary.restored_changed == 0
    missing = await _events(cid, kind="missing", file_id=fid)
    assert missing and all(e.acknowledged_at is not None for e in missing)
    modified = await _events(cid, kind="modified", file_id=fid)
    assert len(modified) == 1 and modified[0].acknowledged_at is None
    assert alerts == [], "re-flagging raises no alert"


@pytest.mark.asyncio
async def test_changed_file_renamed_is_still_changed(cairn_env, alerts):
    root = cairn_env / "rename"
    root.mkdir()
    (root / "a.txt").write_text("original")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    await _scan(cid)
    (root / "a.txt").write_text("changed-and-unique-content")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id

    (root / "a.txt").rename(root / "renamed.txt")
    alerts.clear()
    summary = await _scan(cid)

    assert summary.moved == 1
    survivor = await _file(cid, "renamed.txt")
    assert survivor.id == fid and survivor.status == "modified"
    assert await _file(cid, "a.txt") is None
    assert summary.reflagged == 1
    assert alerts == [], "no additional alert for the reconciled move"
    open_mod = [e for e in await _events(cid, kind="modified") if e.acknowledged_at is None]
    assert len(open_mod) == 1 and open_mod[0].file_id == fid


@pytest.mark.asyncio
async def test_no_committed_batch_shows_the_file_healthy(cairn_env, monkeypatch, alerts):
    """Read the committed state from a SECOND connection after every hash during the scan.

    The walk order is pinned (target first) and the batch size is 2, so the target's batch commits
    long before the scan ends; every file is hashed (deep pass), giving a deterministic observation
    point after each one. Layer 2 alone (the end sweep) would let a mid-scan read see `ok`.
    """
    from src.services import scanner

    root, cid = await _changed_then_missing(cairn_env, "batches", "changed-bytes-longer")
    for i in range(8):
        (root / f"f{i}.txt").write_text(f"filler {i}")
    await _scan(cid)  # track the fillers
    fid = (await _file(cid, "a.txt")).id
    (root / "a.txt").write_text("changed-bytes-longer")

    real_iter = scanner.iter_relpaths
    monkeypatch.setattr(
        scanner,
        "iter_relpaths",
        lambda r, g: iter(sorted(real_iter(r, g), key=lambda p: (p != "a.txt", p))),
    )
    monkeypatch.setattr(scanner, "BATCH", 2)

    db_path = cairn_env / "db" / "cairn.db"
    observed: list[str] = []
    real_hash = scanner._hash

    async def _observing_hash(path):
        out = await real_hash(path)
        con = sqlite3.connect(db_path)
        try:
            observed.append(
                con.execute("SELECT status FROM files WHERE id = ?", (fid,)).fetchone()[0]
            )
        finally:
            con.close()
        return out

    monkeypatch.setattr(scanner, "_hash", _observing_hash)
    summary = await _scan(cid, deep=True)

    assert summary.reflagged == 1
    assert len(observed) >= 10
    assert observed[0] == "missing", "first hash is the target itself, before its batch commits"
    assert "modified" in observed[:-2], "the target's batch committed before the scan finished"
    assert "ok" not in observed, f"a committed batch showed the file healthy: {observed}"
    assert (await _file(cid, "a.txt")).status == "modified"


@pytest.mark.asyncio
async def test_expected_edit_does_not_launder_an_earlier_alarm(cairn_env, alerts):
    root = cairn_env / "churn"
    root.mkdir()
    (root / "p.jpg").write_text("photo")
    cid = await seed_collection(root, ots_mode="none", mode="churn")
    await _scan(cid)
    (root / "p.jpg").unlink()
    await _scan(cid)
    (root / "p.jpg").write_text("a-different-photo")
    first = await _scan(cid)
    assert first.restored_changed == 1
    fid = (await _file(cid, "p.jpg")).id

    (root / "p.jpg").write_text("an ordinary later edit, longer")
    before = await _event_count(cid)
    alerts.clear()
    summary = await _scan(cid)

    assert (await _file(cid, "p.jpg")).status == "modified"
    assert summary.reflagged == 1
    assert await _event_count(cid) == before
    rc = await _events(cid, kind="restored_changed", file_id=fid)
    assert len(rc) == 1 and rc[0].acknowledged_at is None
    assert alerts == []


@pytest.mark.asyncio
async def test_reflagging_writes_no_event_and_dispatches_no_alert(cairn_env, alerts):
    """Legacy `ok` row with an open `modified` event, unchanged on disk: the fast path holds it."""
    root = cairn_env / "quiet"
    root.mkdir()
    (root / "a.txt").write_text("stable")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    await _scan(cid)
    row = await _file(cid, "a.txt")
    await _set_status(row.id, "ok")
    await _add_open_event(cid, row.id, "modified")
    before_sha, before_size = row.sha256, row.size

    before = await _event_count(cid)
    alerts.clear()
    summary = await _scan(cid)

    assert summary.reflagged == 1
    assert summary.modified == 0, "a re-flag is not a newly detected change"
    assert summary.alarming == []
    assert await _event_count(cid) == before
    assert alerts == []
    after = await _file(cid, "a.txt")
    assert after.status == "modified"
    assert (after.sha256, after.size) == (before_sha, before_size)


@pytest.mark.asyncio
async def test_accepted_change_is_not_reflagged(cairn_env, alerts):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import accept_file

    root = cairn_env / "accepted"
    root.mkdir()
    (root / "a.txt").write_text("original")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    await _scan(cid)
    (root / "a.txt").write_text("changed-and-accepted")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    async with get_sessionmaker()() as s:
        from src.models.db import FileEntry

        await accept_file(s, await s.get(Collection, cid), await s.get(FileEntry, fid), None)

    summary = await _scan(cid)
    assert (await _file(cid, "a.txt")).status == "ok"
    assert summary.reflagged == 0


@pytest.mark.asyncio
async def test_records_left_by_earlier_versions_are_corrected(cairn_env, alerts):
    import logging

    root = cairn_env / "legacy"
    root.mkdir()
    (root / "a.txt").write_text("legacy")
    (root / "b.txt").write_text("clean")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    await _scan(cid)
    a = await _file(cid, "a.txt")
    await _set_status(a.id, "ok")
    await _add_open_event(cid, a.id, "modified")

    # A handler on the logger itself: alembic's fileConfig can stop propagation to caplog's root.
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("cairn.scanner")
    handler, old_level, old_disabled = _Collect(level=logging.INFO), logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        summary = await _scan(cid)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.disabled = old_disabled

    assert (await _file(cid, "a.txt")).status == "modified"
    assert (await _file(cid, "b.txt")).status == "new", "a file with no open change is untouched"
    assert summary.reflagged == 1
    assert any("open, unaccepted change alert" in r.getMessage() for r in records)


@pytest.mark.asyncio
async def test_missing_file_is_left_missing(cairn_env, alerts):
    root, cid = await _changed_then_missing(cairn_env, "gone", "changed-then-deleted")
    fid = (await _file(cid, "a.txt")).id
    assert [e for e in await _events(cid, kind="modified", file_id=fid) if not e.acknowledged_at]

    summary = await _scan(cid)

    assert (await _file(cid, "a.txt")).status == "missing"
    assert summary.reflagged == 0


@pytest.mark.asyncio
async def test_auto_baseline_does_not_promote_a_new_row_with_an_open_change(cairn_env, alerts):
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    root = cairn_env / "autobaseline"
    root.mkdir()
    (root / "flagged.txt").write_text("flagged")
    (root / "plain.txt").write_text("plain")
    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s,
            user_id=uid,
            name="ab",
            root=str(root),
            mode="worm",
            ots_mode="none",
            auto_baseline_new=True,
        )
        cid = collection.id
    await _scan(cid)
    flagged = await _file(cid, "flagged.txt")
    assert flagged.status == "new"
    await _add_open_event(cid, flagged.id, "modified")

    summary = await _scan(cid, deep=True)

    assert (await _file(cid, "flagged.txt")).status == "modified"
    assert (await _file(cid, "plain.txt")).status == "ok"
    assert summary.baselined == 1, "only the clean new file graduates"
    assert summary.reflagged == 1
