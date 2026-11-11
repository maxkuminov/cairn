"""Scan safety nets (add-scan-safety-nets): root refusal, walk errors, possible corruption, kept
prior contents + returns, the mass-change ceiling, and their alerts.

Acceptance criteria: ``openspec/changes/add-scan-safety-nets/specs/integrity-scanning/spec.md``
(the four ADDED requirements and the two MODIFIED ones), ``specs/alerting/spec.md`` and
``specs/configuration/spec.md``. One test per scenario, named after it.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_scan_safety_nets.py``
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update

from tests.conftest import seed_collection

needs_permissions = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores directory permissions, so a directory cannot be made unlistable",
)


def _sha(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


# --- helpers ------------------------------------------------------------------------------------


async def _scan(cid: int, *, deep: bool = False, allow_empty: bool = False):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    async with get_sessionmaker()() as s:
        return await scan_collection(
            s, await s.get(Collection, cid), deep=deep, allow_empty=allow_empty
        )


async def _file(cid: int, relpath: str):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == relpath)
        )


async def _files(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return {
            f.relpath: f
            for f in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid))
        }


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


async def _open_events(cid: int, file_id: int | None = None):
    return [e for e in await _events(cid, file_id=file_id) if e.acknowledged_at is None]


async def _last_run(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()).limit(1)
        )


def _snapshot(db: Path, cid: int):
    con = sqlite3.connect(db)
    try:
        return (
            con.execute("SELECT * FROM files WHERE collection_id=? ORDER BY id", (cid,)).fetchall(),
            con.execute("SELECT * FROM events WHERE collection_id=? ORDER BY id", (cid,)).fetchall(),
        )
    finally:
        con.close()


def _corrupt(path: Path, data: str) -> None:
    """Rewrite ``path`` with same-length bytes and restore its exact mtime (the D2 signature)."""
    st = path.stat()
    assert len(data.encode()) == st.st_size
    path.write_text(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def _edit(path: Path, data: str) -> None:
    """Rewrite ``path`` and make sure its mtime moves (an ordinary edit)."""
    st = path.stat()
    path.write_text(data)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def alerts(monkeypatch):
    """Capture every alert dispatch (the scanner imports `dispatch` at call time)."""
    from src.notify import dispatch as dispatch_mod

    sent: list = []

    async def _capture(alert, collection, settings):
        sent.append(alert)

    monkeypatch.setattr(dispatch_mod, "dispatch", _capture)
    return sent


@pytest.fixture
def no_stamping(monkeypatch):
    from src.services import proofs

    async def _noop(session, collection, settings=None, *, progress=None, run_id=None):
        return 0

    monkeypatch.setattr(proofs, "stamp_pending", _noop)


async def _collection(cairn_env, name: str, files: dict[str, str], *, mode: str = "worm"):
    root = cairn_env / name
    root.mkdir()
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data)
    cid = await seed_collection(root, ots_mode="none", mode=mode)
    first = await _scan(cid)
    assert first.result == "ok"
    return root, cid


@pytest.fixture
def unlistable():
    """chmod a directory 000 for the test, restoring it afterwards so tmp cleanup works."""
    touched: list[Path] = []

    def _lock(path: Path) -> None:
        os.chmod(path, 0)
        touched.append(path)

    yield _lock
    for p in touched:
        os.chmod(p, 0o755)


# ================================================================================================
# Requirement: A scan refuses an unreachable or empty root
# ================================================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["gone", "not-a-directory"])
async def test_a_root_that_does_not_exist(cairn_env, alerts, shape):
    from src.services.scanner import REFUSAL_UNREACHABLE, refusal_reason

    root, cid = await _collection(cairn_env, f"gone-{shape}", {"a.txt": "a", "d/b.txt": "b"})
    db = cairn_env / "db" / "cairn.db"
    before = _snapshot(db, cid)
    shutil.rmtree(root)
    if shape == "not-a-directory":
        root.write_text("I am a file now")

    summary = await _scan(cid)

    assert summary.result == "error"
    assert summary.refused == REFUSAL_UNREACHABLE
    assert summary.missing == 0
    assert _snapshot(db, cid) == before, "no file record changed and no event was written"
    run = await _last_run(cid)
    assert run.result == "error" and run.errors == 0
    sample = json.loads(run.error_sample)
    assert len(sample) == 1 and sample[0].startswith("unreachable: ")
    assert str(root) in sample[0]
    assert refusal_reason(run) == sample[0][len("unreachable: ") :]


@pytest.mark.asyncio
async def test_a_root_that_is_empty_while_files_are_tracked(cairn_env, alerts):
    from src.services.scanner import REFUSAL_EMPTY, refusal_reason

    root, cid = await _collection(cairn_env, "empty", {"a.txt": "a", "b.txt": "b"})
    db = cairn_env / "db" / "cairn.db"
    before = _snapshot(db, cid)
    for p in root.iterdir():
        p.unlink()

    summary = await _scan(cid)

    assert summary.result == "error" and summary.refused == REFUSAL_EMPTY
    assert _snapshot(db, cid) == before
    assert {f.status for f in (await _files(cid)).values()} == {"new"}
    run = await _last_run(cid)
    assert run.result == "error" and run.errors == 0
    reason = refusal_reason(run)
    assert reason.startswith(f"the folder {root} is empty but 2 files are tracked")
    assert "--allow-empty" in reason


@pytest.mark.asyncio
async def test_the_override_marks_an_emptied_collection_missing(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "override", {"a.txt": "a", "b.txt": "b"})
    for p in root.iterdir():
        p.unlink()

    summary = await _scan(cid, allow_empty=True)

    assert summary.result == "ok" and summary.refused is None
    assert summary.missing == 2
    assert {f.status for f in (await _files(cid)).values()} == {"missing"}


@pytest.mark.asyncio
async def test_the_cli_override_flag(cairn_env, alerts, capsys):
    """`cairn scan --allow-empty` reaches the scan; without it the CLI reports the refusal."""
    import asyncio

    from src import cli

    root, cid = await _collection(cairn_env, "clic", {"a.txt": "a"})
    (root / "a.txt").unlink()

    rc = await asyncio.to_thread(cli.main, ["scan", "--collection", "clic"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "REFUSED (empty)" in err and "is empty but 1 files are tracked" in err
    assert (await _file(cid, "a.txt")).status == "new"

    rc = await asyncio.to_thread(cli.main, ["scan", "--collection", "clic", "--allow-empty"])
    assert rc == 0
    assert (await _file(cid, "a.txt")).status == "missing"


@pytest.mark.asyncio
async def test_a_collection_that_tracks_nothing_is_not_refused(cairn_env, alerts):
    root = cairn_env / "nothing"
    root.mkdir()
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    assert (await _scan(cid)).result == "ok"

    # ...and one holding only `missing` records.
    (root / "a.txt").write_text("a")
    await _scan(cid)
    (root / "a.txt").unlink()
    assert (await _scan(cid, allow_empty=True)).missing == 1
    summary = await _scan(cid)
    assert summary.result == "ok" and summary.refused is None


@pytest.mark.asyncio
async def test_a_partly_missing_tree_is_scanned(cairn_env, alerts):
    root, cid = await _collection(
        cairn_env, "partly", {"keep.txt": "k", "gone/a.txt": "a", "gone/b.txt": "b"}
    )
    shutil.rmtree(root / "gone")

    summary = await _scan(cid)

    assert summary.result == "ok" and summary.refused is None
    assert summary.missing == 2
    files = await _files(cid)
    assert files["gone/a.txt"].status == "missing" and files["keep.txt"].status == "new"


@needs_permissions
@pytest.mark.asyncio
async def test_an_unreadable_subfolder_does_not_read_as_deleted_files(
    cairn_env, alerts, unlistable
):
    from src.services.scanner import WALK_UNREADABLE

    root, cid = await _collection(
        cairn_env, "unread", {"top.txt": "top", "sub/a.txt": "a", "sub/b.txt": "b"}
    )
    _edit(root / "top.txt", "top, edited")
    unlistable(root / "sub")

    summary = await _scan(cid)

    assert summary.result == "partial" and summary.walk_incomplete
    assert summary.missing == 0
    files = await _files(cid)
    assert files["sub/a.txt"].status == "new" and files["sub/b.txt"].status == "new"
    assert files["top.txt"].status == "modified", "a change in a readable folder is recorded"
    assert not await _events(cid, kind="missing")
    run = await _last_run(cid)
    assert run.result == "partial" and run.errors == 1
    sample = json.loads(run.error_sample)
    assert sample[0].startswith(f"{WALK_UNREADABLE}: ") and "sub" in sample[0]


@needs_permissions
@pytest.mark.asyncio
async def test_an_incomplete_walk_still_reconciles_a_move_between_readable_folders(
    cairn_env, alerts, unlistable
):
    """An unreadable folder shields only what lies beneath it: a rename elsewhere in the
    collection is still seen for what it is, and nothing is reported missing."""
    root, cid = await _collection(
        cairn_env, "nomove", {"a/unique.txt": "unique-content", "locked/x.txt": "x"}
    )
    (root / "a" / "unique.txt").rename(root / "renamed.txt")
    unlistable(root / "locked")

    summary = await _scan(cid)

    assert summary.walk_incomplete and summary.moved == 1 and summary.missing == 0
    files = await _files(cid)
    assert "renamed.txt" in files and "a/unique.txt" not in files
    assert files["locked/x.txt"].status == "new", "beneath the unreadable folder: not missing"


@needs_permissions
@pytest.mark.asyncio
async def test_a_file_moved_out_of_an_unreadable_folder_is_not_called_missing(
    cairn_env, alerts, unlistable
):
    """The old path lies beneath the unreadable folder, so it is no candidate: the new path is
    recorded as an addition and nothing is reported missing by this pass."""
    root, cid = await _collection(
        cairn_env, "moveout", {"locked/unique.txt": "unique-content", "open/y.txt": "y"}
    )
    (root / "locked" / "unique.txt").rename(root / "open" / "unique.txt")
    unlistable(root / "locked")

    summary = await _scan(cid)

    assert summary.walk_incomplete and summary.missing == 0 and summary.moved == 0
    assert (await _file(cid, "locked/unique.txt")).status == "new"


@needs_permissions
@pytest.mark.asyncio
async def test_unreadable_takes_precedence_over_empty(cairn_env, alerts, unlistable):
    root, cid = await _collection(cairn_env, "prec", {"sub/a.txt": "a", "sub/b.txt": "b"})
    unlistable(root / "sub")

    summary = await _scan(cid)

    assert summary.result == "partial" and summary.refused is None and summary.walk_incomplete
    assert summary.missing == 0
    assert {f.status for f in (await _files(cid)).values()} == {"new"}
    run = await _last_run(cid)
    assert json.loads(run.error_sample)[0].startswith("unreadable: ")


async def _scheduler_deep_pass(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler

    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        c.verify_cadence_seconds = 604800
        c.last_full_scan_at = None  # owed
        await s.commit()
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, time.monotonic())
    async with get_sessionmaker()() as s:
        return (await s.get(Collection, cid)).last_full_scan_at


@pytest.mark.asyncio
async def test_a_refused_deep_pass_is_still_owed(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "owed", {"a.txt": "a"})
    shutil.rmtree(root)
    assert await _scheduler_deep_pass(cid) is None
    run = await _last_run(cid)
    assert run.deep and run.result == "error"


@pytest.mark.asyncio
async def test_a_deep_pass_that_ended_in_error_is_still_owed(cairn_env, alerts, monkeypatch):
    from src.services import scanner

    root, cid = await _collection(cairn_env, "owed-err", {"a.txt": "a"})

    async def boom(path):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(scanner, "_hash", boom)
    assert await _scheduler_deep_pass(cid) is None
    assert (await _last_run(cid)).result == "error"


@needs_permissions
@pytest.mark.asyncio
async def test_a_deep_pass_that_could_not_list_a_folder_is_still_owed(
    cairn_env, alerts, unlistable
):
    root, cid = await _collection(cairn_env, "owed-walk", {"a.txt": "a", "sub/b.txt": "b"})
    unlistable(root / "sub")
    assert await _scheduler_deep_pass(cid) is None
    assert (await _last_run(cid)).result == "partial"


@pytest.mark.asyncio
async def test_a_deep_pass_partial_only_by_a_skipped_file_counts(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "owed-skip", {"a.txt": "a"})
    with open(os.path.join(os.fsencode(str(root)), b"1\xe0.jpg"), "wb") as fh:
        fh.write(b"latin-1 name")
    assert await _scheduler_deep_pass(cid) is not None
    run = await _last_run(cid)
    assert run.result == "partial" and run.deep


@pytest.mark.asyncio
async def test_a_refused_pass_does_not_refresh_freshness(cairn_env, alerts):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.scheduler import _utcnow, compute_health

    root, cid = await _collection(cairn_env, "fresh", {"a.txt": "a"})
    # Age the only completed scan well past the freshness window.
    old = _utcnow() - timedelta(days=2)
    async with get_sessionmaker()() as s:
        await s.execute(update(Run).where(Run.collection_id == cid).values(started=old, finished=old))
        await s.commit()
    shutil.rmtree(root)
    assert (await _scan(cid)).refused is not None

    async with get_sessionmaker()() as s:
        report = await compute_health(s, get_settings())
    state = {c.id: c for c in report.collections}[cid]
    assert state.state == "stale"
    assert state.last_scan_age_seconds is not None and state.last_scan_age_seconds > 86400


# ================================================================================================
# Requirement: Possible corruption is its own finding (+ MODIFIED deep-verify scenarios)
# ================================================================================================


@pytest.mark.asyncio
async def test_corruption_in_a_churn_collection_is_flagged(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "c-churn", {"data.bin": "AAAAA"}, mode="churn")
    old = await _file(cid, "data.bin")
    _corrupt(root / "data.bin", "BBBBB")

    quick = await _scan(cid)
    assert quick.corrupted == 0, "a quick pass cannot see the corruption signature"

    alerts.clear()
    summary = await _scan(cid, deep=True)

    row = await _file(cid, "data.bin")
    assert row.status == "modified", "not silently re-baselined"
    assert summary.corrupted == 1 and summary.modified == 1
    ev = await _events(cid, kind="corrupted", file_id=row.id)
    assert len(ev) == 1 and ev[0].acknowledged_at is None
    assert not await _events(cid, kind="modified")
    assert row.prior_sha256 == old.sha256 and row.prior_size == old.size
    assert row.prior_mtime == old.mtime
    detail = json.loads(ev[0].detail)
    assert detail["path"] == "data.bin"
    assert detail["from"]["sha256"] == old.sha256 and detail["to"]["sha256"] == _sha("BBBBB")
    # Alerting: possible corruption alerts in a churn corpus.
    assert len(alerts) == 1
    assert alerts[0].summary == "1 possibly corrupted"
    assert alerts[0].paths == ["data.bin"]


@pytest.mark.asyncio
async def test_corruption_in_a_worm_collection_is_named_as_such(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "c-worm", {"img.raw": "AAAAA"})
    _corrupt(root / "img.raw", "BBBBB")

    summary = await _scan(cid, deep=True)

    row = await _file(cid, "img.raw")
    assert row.status == "modified" and summary.corrupted == 1
    assert len(await _events(cid, kind="corrupted")) == 1
    assert await _events(cid, kind="modified") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["worm", "churn"])
async def test_an_edit_that_preserved_the_size_is_still_an_edit(cairn_env, alerts, mode):
    root, cid = await _collection(cairn_env, f"same-size-{mode}", {"a.txt": "AAAAA"}, mode=mode)
    _edit(root / "a.txt", "BBBBB")

    summary = await _scan(cid, deep=True)

    assert summary.corrupted == 0
    assert await _events(cid, kind="corrupted") == []
    if mode == "worm":
        assert len(await _events(cid, kind="modified")) == 1
    else:
        # Churn modification does not nag (MODIFIED "WORM and churn modes differ in nagging").
        assert (await _file(cid, "a.txt")).status == "ok"
        assert await _events(cid, kind="modified") == []
        assert alerts == []


@pytest.mark.asyncio
async def test_a_file_with_no_recorded_digest_is_not_corrupted(cairn_env, alerts):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root, cid = await _collection(cairn_env, "nodigest", {"a.txt": "AAAAA"})
    fid = (await _file(cid, "a.txt")).id
    async with get_sessionmaker()() as s:
        await s.execute(update(FileEntry).where(FileEntry.id == fid).values(sha256=None, status="ok"))
        await s.commit()
    before = len(await _events(cid))

    summary = await _scan(cid, deep=True)

    row = await _file(cid, "a.txt")
    assert row.sha256 == _sha("AAAAA") and row.status == "ok"
    assert summary.corrupted == 0 and summary.modified == 0
    assert len(await _events(cid)) == before
    assert alerts == []


async def _corrupted(cairn_env, name: str, mode: str = "churn"):
    root, cid = await _collection(cairn_env, name, {"x.bin": "ORIGINAL-1", "other.txt": "o"}, mode=mode)
    _corrupt(root / "x.bin", "CORRUPTED!")
    await _scan(cid, deep=True)
    row = await _file(cid, "x.bin")
    assert row.status == "modified" and row.prior_sha256 == _sha("ORIGINAL-1")
    return root, cid, row.id


@pytest.mark.asyncio
async def test_a_corrupted_file_stays_an_issue_when_renamed(cairn_env, alerts):
    root, cid, fid = await _corrupted(cairn_env, "c-rename")
    (root / "x.bin").rename(root / "y.bin")

    summary = await _scan(cid)

    assert summary.moved == 1
    row = await _file(cid, "y.bin")
    assert row.id == fid and row.status == "modified"
    assert row.prior_sha256 == _sha("ORIGINAL-1")


@pytest.mark.asyncio
async def test_a_corrupted_file_stays_an_issue_when_restored_corrupted(cairn_env, alerts):
    root, cid, fid = await _corrupted(cairn_env, "c-restore")
    (root / "x.bin").unlink()
    await _scan(cid)
    assert (await _file(cid, "x.bin")).status == "missing"
    (root / "x.bin").write_text("CORRUPTED!")

    summary = await _scan(cid)

    assert (await _file(cid, "x.bin")).status == "modified"
    assert summary.reverted == 0
    assert len([e for e in await _events(cid, kind="corrupted") if e.acknowledged_at is None]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["replaced", "deleted-and-restored"])
async def test_restoring_a_corrupted_files_original_bytes_closes_it(cairn_env, alerts, how):
    root, cid, fid = await _corrupted(cairn_env, f"c-close-{how}")
    if how == "deleted-and-restored":
        (root / "x.bin").unlink()
        await _scan(cid)
        (root / "x.bin").write_text("ORIGINAL-1")
    else:
        _edit(root / "x.bin", "ORIGINAL-1")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "x.bin")
    assert row.status == "ok" and row.prior_sha256 is None
    assert summary.reverted == 1
    assert await _open_events(cid, fid) == []
    corrupted = await _events(cid, kind="corrupted", file_id=fid)
    assert corrupted[0].acknowledged_at is not None and corrupted[0].acknowledged_by is None
    assert alerts == []


# ================================================================================================
# Requirement: The contents on record before an unaccepted change are kept
# ================================================================================================


@pytest.mark.asyncio
async def test_a_change_keeps_what_it_replaced(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "keep", {"a.txt": "original"})
    old = await _file(cid, "a.txt")
    _edit(root / "a.txt", "changed, longer")

    await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified"
    assert row.sha256 == _sha("changed, longer")
    assert (row.prior_sha256, row.prior_size, row.prior_mtime) == (old.sha256, old.size, old.mtime)
    (ev,) = await _events(cid, kind="modified", file_id=row.id)
    detail = json.loads(ev.detail)
    assert detail == {
        "path": "a.txt",
        "from": {"sha256": old.sha256, "size": old.size, "mtime": old.mtime},
        "to": {"sha256": row.sha256, "size": row.size, "mtime": row.mtime},
    }


@pytest.mark.asyncio
async def test_a_second_change_does_not_replace_what_was_kept(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "keep2", {"a.txt": "original"})
    _edit(root / "a.txt", "first change")
    await _scan(cid)
    _edit(root / "a.txt", "second change!!")
    await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified" and row.prior_sha256 == _sha("original")


@pytest.mark.asyncio
async def test_putting_the_original_back_closes_the_issue(cairn_env, alerts):
    from src.services.scanner import RETURN_DETAIL

    root, cid = await _collection(cairn_env, "back", {"a.txt": "original", "b.txt": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    _edit(root / "a.txt", "original")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "ok" and row.sha256 == _sha("original")
    assert (row.prior_sha256, row.prior_size, row.prior_mtime) == (None, None, None)
    assert summary.reverted == 1 and summary.modified == 0
    modified = await _events(cid, kind="modified", file_id=fid)
    assert modified[0].acknowledged_at is not None and modified[0].acknowledged_by is None
    restored = await _events(cid, kind="restored", file_id=fid)
    assert len(restored) == 1 and restored[0].detail == RETURN_DETAIL
    assert restored[0].acknowledged_at is not None
    assert summary.alarming == [] and alerts == []


@pytest.mark.asyncio
async def test_a_changed_file_deleted_and_restored_from_the_original_closes_too(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "back2", {"a.txt": "original", "b.txt": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    (root / "a.txt").unlink()
    await _scan(cid)
    (root / "a.txt").write_text("original")
    alerts.clear()

    summary = await _scan(cid)

    assert (await _file(cid, "a.txt")).status == "ok"
    assert summary.reverted == 1 and summary.restored_changed == 0
    for kind in ("modified", "missing"):
        evs = await _events(cid, kind=kind, file_id=fid)
        assert evs and all(e.acknowledged_at is not None for e in evs)
    assert await _events(cid, kind="restored_changed") == []
    assert alerts == []


@pytest.mark.asyncio
async def test_coming_back_as_the_changed_version_is_still_unresolved(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "asch", {"a.txt": "original", "b.txt": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    (root / "a.txt").unlink()
    await _scan(cid)
    (root / "a.txt").write_text("changed")

    await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified" and row.prior_sha256 == _sha("original")


@pytest.mark.asyncio
async def test_a_changed_file_that_came_back_as_a_third_version_keeps_the_original(
    cairn_env, alerts
):
    root, cid = await _collection(cairn_env, "third", {"a.txt": "original", "b.txt": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    (root / "a.txt").unlink()
    await _scan(cid)
    (root / "a.txt").write_text("a third version")
    s3 = await _scan(cid)
    assert s3.restored_changed == 1
    row = await _file(cid, "a.txt")
    assert row.status == "modified" and row.prior_sha256 == _sha("original")

    _edit(root / "a.txt", "changed")  # back as the changed version: still not a return
    await _scan(cid)
    row = await _file(cid, "a.txt")
    assert row.status == "modified" and row.prior_sha256 == _sha("original")

    _edit(root / "a.txt", "original")  # only the original closes it
    assert (await _scan(cid)).reverted == 1
    assert (await _file(cid, "a.txt")).status == "ok"
    assert await _open_events(cid, row.id) == []


@pytest.mark.asyncio
async def test_a_file_that_was_healthy_when_it_went_missing_keeps_that_digest(cairn_env, alerts):
    """`restored_changed` on a healthy-when-missing file keeps the digest recorded when it left."""
    root, cid = await _collection(cairn_env, "rc", {"a.txt": "original", "b.txt": "b"})
    (root / "a.txt").unlink()
    await _scan(cid)
    (root / "a.txt").write_text("something else")
    await _scan(cid)
    row = await _file(cid, "a.txt")
    assert row.status == "modified" and row.prior_sha256 == _sha("original")
    _edit(root / "a.txt", "original")
    assert (await _scan(cid)).reverted == 1
    assert await _open_events(cid, row.id) == []


@pytest.mark.asyncio
async def test_something_else_again_is_still_a_change(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "else", {"a.txt": "original"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    _edit(root / "a.txt", "yet another thing")
    alerts.clear()

    summary = await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "modified" and summary.modified == 1 and summary.reverted == 0
    assert len(await _events(cid, kind="modified", file_id=row.id)) == 2
    assert len(alerts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["file", "set", "collection"])
async def test_accepting_discards_what_was_kept(cairn_env, alerts, how):
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services.scanner import accept_collection, accept_file, accept_files

    root, cid = await _collection(cairn_env, f"acc-{how}", {"a.txt": "original"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        if how == "file":
            await accept_file(s, c, await s.get(FileEntry, fid), None)
        elif how == "set":
            await accept_files(s, c, [fid], None)
        else:
            await accept_collection(s, c, None, scope={"modified"})
    row = await _file(cid, "a.txt")
    assert row.status == "ok"
    assert (row.prior_sha256, row.prior_size, row.prior_mtime) == (None, None, None)

    _edit(root / "a.txt", "original")
    summary = await _scan(cid)
    assert summary.reverted == 0 and summary.modified == 1
    assert (await _file(cid, "a.txt")).status == "modified"


@pytest.mark.asyncio
async def test_same_size_is_not_a_return(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "samesize", {"a.txt": "AAAA"})
    _edit(root / "a.txt", "BBBBBB")
    await _scan(cid)
    _edit(root / "a.txt", "CCCC")

    summary = await _scan(cid)

    assert summary.reverted == 0
    assert (await _file(cid, "a.txt")).status == "modified"


@pytest.mark.asyncio
async def test_a_manifest_import_that_leaves_a_row_ok_clears_the_kept_contents(
    cairn_env, tmp_path, alerts
):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.manifest import import_manifest

    root, cid = await _collection(cairn_env, "mani", {"a.txt": "original"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    row = await _file(cid, "a.txt")
    assert row.prior_sha256 is not None
    # The change is acknowledged out of band (no accept), so the import may leave the row `ok`.
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        await s.execute(update(Event).where(Event.file_id == row.id).values(acknowledged_at=row.first_seen))
        await s.commit()
    manifest = tmp_path / "manifest.tsv"
    manifest.write_text(f"a.txt\t{row.size}\t{row.mtime}\t{row.sha256}\n")
    async with get_sessionmaker()() as s:
        await import_manifest(s, await s.get(Collection, cid), manifest)
    row = await _file(cid, "a.txt")
    assert row.status == "ok" and row.prior_sha256 is None


@pytest.mark.asyncio
async def test_a_return_and_its_acknowledgement_commit_together(cairn_env, monkeypatch, alerts):
    """From a SECOND connection after every hash: the returned file is never `ok` while one of its
    change alerts is open, and never acknowledged while still `modified` (batch size 2, target
    first in the walk, so its batch commits long before the pass ends)."""
    from src.services import scanner

    root, cid = await _collection(
        cairn_env, "atomic", {"a.txt": "original", **{f"f{i}.txt": f"filler {i}" for i in range(7)}}
    )
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    fid = (await _file(cid, "a.txt")).id
    _edit(root / "a.txt", "original")

    real_iter = scanner.iter_relpaths
    monkeypatch.setattr(
        scanner,
        "iter_relpaths",
        lambda r, g, *a: iter(sorted(real_iter(r, g, *a), key=lambda p: (p != "a.txt", p))),
    )
    monkeypatch.setattr(scanner, "BATCH", 2)
    db = cairn_env / "db" / "cairn.db"
    observed: list[tuple[str, int]] = []
    real_hash = scanner._hash

    async def _observing_hash(path):
        out = await real_hash(path)
        con = sqlite3.connect(db)
        try:
            status = con.execute("SELECT status FROM files WHERE id=?", (fid,)).fetchone()[0]
            open_n = con.execute(
                "SELECT count(*) FROM events WHERE file_id=? AND acknowledged_at IS NULL "
                "AND kind IN ('modified','restored_changed','corrupted')",
                (fid,),
            ).fetchone()[0]
            observed.append((status, open_n))
        finally:
            con.close()
        return out

    monkeypatch.setattr(scanner, "_hash", _observing_hash)
    summary = await scanner_scan_deep(cid)

    assert summary.reverted == 1
    assert observed[0] == ("modified", 1)
    assert ("ok", 0) in observed[:-2], "the return's batch committed before the pass ended"
    assert set(observed) <= {("modified", 1), ("ok", 0)}, observed
    assert (await _file(cid, "a.txt")).status == "ok"


async def scanner_scan_deep(cid: int):
    return await _scan(cid, deep=True)


# ================================================================================================
# Requirement: A mass change is not absorbed (+ configuration)
# ================================================================================================


@pytest.fixture
def mass(monkeypatch):
    from src.config import get_settings

    def _set(percent: int, min_files: int) -> None:
        monkeypatch.setenv("CAIRN_MASS_CHANGE_PERCENT", str(percent))
        monkeypatch.setenv("CAIRN_MASS_CHANGE_MIN_FILES", str(min_files))
        get_settings.cache_clear()

    yield _set
    get_settings.cache_clear()


async def _churn_burst(cairn_env, name: str, total: int, changed: int):
    files = {f"f{i}.txt": f"file {i}" for i in range(total)}
    root, cid = await _collection(cairn_env, name, files, mode="churn")
    before = await _files(cid)
    for i in range(changed):
        _edit(root / f"f{i}.txt", f"file {i} rewritten")
    return root, cid, before


@pytest.mark.asyncio
async def test_a_burst_over_both_thresholds_becomes_issues(cairn_env, alerts, mass):
    mass(50, 3)
    root, cid, before = await _churn_burst(cairn_env, "burst", total=4, changed=3)
    alerts.clear()

    summary = await _scan(cid)

    assert summary.mass_change == 3 and summary.modified == 3
    files = await _files(cid)
    for i in range(3):
        row = files[f"f{i}.txt"]
        assert row.status == "modified"
        # Converted files keep what they replaced.
        assert row.prior_sha256 == before[f"f{i}.txt"].sha256
        assert row.prior_size == before[f"f{i}.txt"].size
        (ev,) = await _events(cid, kind="modified", file_id=row.id)
        assert ev.acknowledged_at is None
        assert json.loads(ev.detail)["mass"] == 3
    assert files["f3.txt"].status == "new"
    assert (await _last_run(cid)).modified == 3
    # Alerting: a mass change alerts, naming the count and saying it is unusually many.
    assert len(alerts) == 1
    assert alerts[0].summary == "3 modified — unusually many for one scan"


@pytest.mark.asyncio
async def test_a_burst_under_the_share_is_absorbed(cairn_env, alerts, mass):
    mass(90, 2)
    root, cid, _ = await _churn_burst(cairn_env, "share", total=4, changed=3)
    summary = await _scan(cid)
    assert summary.mass_change == 0
    assert {f.status for f in (await _files(cid)).values()} <= {"ok", "new"}
    assert await _events(cid, kind="modified") == [] and alerts == []


@pytest.mark.asyncio
async def test_a_burst_under_the_minimum_is_absorbed(cairn_env, alerts, mass):
    mass(25, 10)
    root, cid, _ = await _churn_burst(cairn_env, "minimum", total=4, changed=4)
    summary = await _scan(cid)
    assert summary.mass_change == 0
    assert {f.status for f in (await _files(cid)).values()} == {"ok"}
    assert alerts == []


@pytest.mark.asyncio
async def test_the_rule_can_be_disabled(cairn_env, alerts, mass):
    mass(0, 0)
    root, cid, _ = await _churn_burst(cairn_env, "disabled", total=3, changed=3)
    summary = await _scan(cid)
    assert summary.mass_change == 0
    assert {f.status for f in (await _files(cid)).values()} == {"ok"}


@pytest.mark.asyncio
async def test_converted_files_keep_what_they_replaced(cairn_env, alerts, mass):
    mass(50, 2)
    root, cid, before = await _churn_burst(cairn_env, "conv", total=2, changed=2)
    await _scan(cid)
    row = await _file(cid, "f0.txt")
    assert row.status == "modified" and row.prior_sha256 == before["f0.txt"].sha256

    _edit(root / "f0.txt", "file 0")  # put it back
    summary = await _scan(cid)
    assert summary.reverted == 1
    assert (await _file(cid, "f0.txt")).status == "ok"
    assert await _open_events(cid, row.id) == []


@pytest.mark.asyncio
async def test_the_mass_rule_does_not_apply_to_worm(cairn_env, alerts, mass):
    mass(50, 2)
    files = {f"f{i}.txt": f"file {i}" for i in range(3)}
    root, cid = await _collection(cairn_env, "wormmass", files)
    for i in range(3):
        _edit(root / f"f{i}.txt", f"file {i} edited")
    summary = await _scan(cid)
    assert summary.mass_change == 0 and summary.modified == 3
    assert all("mass" not in json.loads(e.detail) for e in await _events(cid, kind="modified"))


def test_mass_change_settings_defaults_and_validation(monkeypatch):
    from pydantic import ValidationError

    from src.config import Settings

    monkeypatch.delenv("CAIRN_MASS_CHANGE_PERCENT", raising=False)
    monkeypatch.delenv("CAIRN_MASS_CHANGE_MIN_FILES", raising=False)
    s = Settings()
    assert s.mass_change_percent == 25 and s.mass_change_min_files == 200
    for env, value in (
        ("CAIRN_MASS_CHANGE_PERCENT", "-1"),
        ("CAIRN_MASS_CHANGE_PERCENT", "101"),
        ("CAIRN_MASS_CHANGE_MIN_FILES", "-1"),
    ):
        monkeypatch.setenv(env, value)
        with pytest.raises(ValidationError):
            Settings()
        monkeypatch.delenv(env)
    monkeypatch.setenv("CAIRN_MASS_CHANGE_PERCENT", "0")
    assert Settings().mass_change_percent == 0


# ================================================================================================
# Alerting: refused roots and unreadable folders
# ================================================================================================


async def _age_runs(cid: int, hours: float) -> None:
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.scheduler import _utcnow

    async with get_sessionmaker()() as s:
        await s.execute(
            update(Run)
            .where(Run.collection_id == cid)
            .values(started=_utcnow() - timedelta(hours=hours))
        )
        # The alert limit is keyed on the last alert ATTEMPT (an `app_settings` row), so "time
        # passing" has to age that as well as the runs.
        from src.models.db import AppSetting

        await s.execute(
            update(AppSetting)
            .where(AppSetting.key.like(f"safety_alert_at:{cid}:%"))
            .values(value=(_utcnow() - timedelta(hours=hours)).isoformat())
        )
        await s.commit()


@pytest.mark.asyncio
async def test_an_unreachable_root_alerts_and_not_on_every_scan(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "outage", {"a.txt": "a"})
    shutil.rmtree(root)
    alerts.clear()

    await _scan(cid)
    await _age_runs(cid, 1)  # "an hour later"
    await _scan(cid)

    assert len(alerts) == 1
    assert "not reachable" in alerts[0].summary and "outage" == alerts[0].collection_name
    assert alerts[0].paths == []


@pytest.mark.asyncio
async def test_a_lasting_outage_is_repeated_daily(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "daily", {"a.txt": "a"})
    shutil.rmtree(root)
    await _scan(cid)
    await _age_runs(cid, 25)
    alerts.clear()

    await _scan(cid)

    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_either_refusal_kind_suppresses_the_other_within_a_day(cairn_env, alerts):
    root, cid = await _collection(cairn_env, "either", {"a.txt": "a"})
    (root / "a.txt").unlink()
    await _scan(cid)  # refused: empty
    shutil.rmtree(root)
    alerts.clear()
    summary = await _scan(cid)  # refused: unreachable
    assert summary.refused == "unreachable"
    assert alerts == []


@needs_permissions
@pytest.mark.asyncio
async def test_an_unreadable_folder_alerts(cairn_env, alerts, unlistable):
    root, cid = await _collection(cairn_env, "ualert", {"a.txt": "a", "sub/b.txt": "b"})
    unlistable(root / "sub")
    alerts.clear()

    await _scan(cid)
    assert len(alerts) == 1
    assert alerts[0].summary == (
        "1 folder could not be read — the files beneath it are not being checked"
    )
    assert alerts[0].paths == []

    await _scan(cid)
    assert len(alerts) == 1, "not repeated within twenty-four hours"


@pytest.mark.asyncio
async def test_an_unrelated_earlier_failure_does_not_suppress_the_alert(cairn_env, alerts):
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.scheduler import _utcnow

    root, cid = await _collection(cairn_env, "unrel", {"a.txt": "a"})
    async with get_sessionmaker()() as s:
        for sample in (None, json.dumps(["hash: b'x.bin'"]), json.dumps(["unreadable: b'sub'"])):
            s.add(
                Run(
                    collection_id=cid,
                    kind="scan",
                    started=_utcnow() - timedelta(minutes=30),
                    finished=_utcnow(),
                    result="error",
                    error_sample=sample,
                )
            )
        await s.commit()
    shutil.rmtree(root)
    alerts.clear()

    await _scan(cid)

    assert len(alerts) == 1 and "not reachable" in alerts[0].summary


@pytest.mark.asyncio
async def test_a_failing_alert_never_fails_a_refused_scan(cairn_env, monkeypatch):
    from src.notify import dispatch as dispatch_mod

    async def _boom(alert, collection, settings):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(dispatch_mod, "dispatch", _boom)
    root, cid = await _collection(cairn_env, "boomalert", {"a.txt": "a"})
    shutil.rmtree(root)
    summary = await _scan(cid)
    assert summary.result == "error" and summary.refused == "unreachable"
    run = await _last_run(cid)
    assert run.result == "error" and run.finished is not None


@pytest.mark.asyncio
async def test_a_refused_run_is_not_the_progress_estimate(cairn_env, alerts):
    """A refused run records processed=0; it must not become the next scan's progress total."""
    from src.database import get_sessionmaker
    from src.models.db import Run

    root, cid = await _collection(cairn_env, "estimate", {"a.txt": "a", "b.txt": "b"})
    for p in root.iterdir():
        p.unlink()
    await _scan(cid)
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    await _scan(cid)
    async with get_sessionmaker()() as s:
        totals = list(
            await s.scalars(select(Run.total).where(Run.collection_id == cid).order_by(Run.id))
        )
        assert totals[-1] == 2
        n = await s.scalar(select(func.count()).select_from(Run).where(Run.collection_id == cid))
        assert n == 3


@pytest.mark.asyncio
async def test_a_refused_pass_runs_no_stamp_tail(cairn_env, alerts, monkeypatch):
    """A refused pass writes nothing about any file — including the perfile stamp tail."""
    from src.services import proofs

    calls: list[int] = []

    async def _record(session, collection, settings=None, *, progress=None, run_id=None):
        calls.append(collection.id)
        return 0

    monkeypatch.setattr(proofs, "stamp_pending", _record)
    root = cairn_env / "nostamp"
    root.mkdir()
    (root / "a.txt").write_text("a")
    cid = await seed_collection(root, ots_mode="perfile", mode="worm")
    await _scan(cid)
    assert calls == [cid]
    shutil.rmtree(root)

    assert (await _scan(cid)).refused == "unreachable"
    assert calls == [cid], "no stamp pass after a refusal"


@pytest.mark.asyncio
async def test_a_row_recorded_healthy_carries_no_kept_contents(cairn_env, alerts):
    """A change whose alert was marked reviewed (not accepted) and that then comes back as the
    recorded bytes is recorded `ok` by the existing rule — and so must not keep a prior."""
    from src.database import get_sessionmaker
    from src.models.db import Event

    root, cid = await _collection(cairn_env, "reviewed", {"a.txt": "original", "b.txt": "b"})
    _edit(root / "a.txt", "changed")
    await _scan(cid)
    row = await _file(cid, "a.txt")
    assert row.prior_sha256 is not None
    async with get_sessionmaker()() as s:
        await s.execute(
            update(Event).where(Event.file_id == row.id).values(acknowledged_at=row.first_seen)
        )
        await s.commit()
    (root / "a.txt").unlink()
    await _scan(cid)
    (root / "a.txt").write_text("changed")
    await _scan(cid)

    row = await _file(cid, "a.txt")
    assert row.status == "ok"
    assert (row.prior_sha256, row.prior_size, row.prior_mtime) == (None, None, None)


# --- implementation review, scope A ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_same_size_change_with_no_recorded_date_is_not_absorbed(cairn_env):
    """A record with a digest and a size but NO modification time (a manifest import that carried
    none): a same-size digest change gives no evidence of an ordinary edit, so `churn` must not
    absorb it."""
    from sqlalchemy import select, update

    from src.database import get_sessionmaker
    from src.models.db import Collection, Event, FileEntry
    from src.services.scanner import scan_collection

    root = cairn_env / "nodate"
    root.mkdir()
    (root / "a.bin").write_bytes(b"AAAA")
    (root / "b.bin").write_bytes(b"bystander")
    cid = await seed_collection(root, ots_mode="none", mode="churn")
    async with get_sessionmaker()() as s:
        await scan_collection(s, await s.get(Collection, cid))
        await s.execute(update(FileEntry).where(FileEntry.relpath == "a.bin").values(mtime=None))
        await s.commit()

    (root / "a.bin").write_bytes(b"BBBB")
    async with get_sessionmaker()() as s:
        summary = await scan_collection(s, await s.get(Collection, cid))
        row = await s.scalar(select(FileEntry).where(FileEntry.relpath == "a.bin"))
        kinds = list(
            await s.scalars(
                select(Event.kind).where(Event.file_id == row.id, Event.acknowledged_at.is_(None))
            )
        )
    assert row.status == "modified"
    assert kinds == ["corrupted"]
    assert summary.corrupted == 1


# --- implementation review, scope B ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_file_that_is_listed_but_cannot_be_examined_is_not_missing(cairn_env, monkeypatch):
    """A directory can be listable while lookups beneath it fail (search permission removed, I/O
    errors). Every name is returned, every `stat` fails — and none of those files was deleted."""
    from pathlib import Path

    from sqlalchemy import func, select

    from src.database import get_sessionmaker
    from src.models.db import Collection, Event, FileEntry
    from src.services.scanner import scan_collection

    root = cairn_env / "nostat"
    (root / "locked").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / "locked" / name).write_text(name)
    (root / "fine.txt").write_text("fine")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    async with get_sessionmaker()() as s:
        await scan_collection(s, await s.get(Collection, cid))

    real_stat = Path.stat

    def failing_stat(self, *args, **kwargs):
        if self.parent.name == "locked":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    async with get_sessionmaker()() as s:
        summary = await scan_collection(s, await s.get(Collection, cid))
        statuses = dict((await s.execute(select(FileEntry.relpath, FileEntry.status))).all())
        missing_events = await s.scalar(
            select(func.count()).select_from(Event).where(Event.kind == "missing")
        )

    assert set(statuses.values()) == {"new"}, statuses  # nothing went missing
    assert missing_events == 0
    assert summary.missing == 0 and summary.errors == 3 and summary.result == "partial"


@pytest.mark.asyncio
async def test_a_stray_symlink_does_not_make_an_empty_root_pass(cairn_env):
    """An unmounted volume's mount point holding nothing but a symlink is still an empty root."""
    from sqlalchemy import select

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services.scanner import REFUSAL_EMPTY, scan_collection

    root = cairn_env / "linkonly"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    async with get_sessionmaker()() as s:
        await scan_collection(s, await s.get(Collection, cid))

    (root / "a.txt").unlink()
    (root / "b.txt").unlink()
    (root / "stray").symlink_to(cairn_env)
    async with get_sessionmaker()() as s:
        summary = await scan_collection(s, await s.get(Collection, cid))
        statuses = set(await s.scalars(select(FileEntry.status)))

    assert summary.refused == REFUSAL_EMPTY and summary.result == "error"
    assert statuses == {"new"}


@pytest.mark.asyncio
async def test_an_unexamined_file_is_not_graduated_by_a_deep_pass(cairn_env, monkeypatch):
    """Listed but unreadable: present, so not missing — and not looked at, so not graduated."""
    from pathlib import Path

    from sqlalchemy import select, update

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services.scanner import scan_collection

    root = cairn_env / "nograd"
    root.mkdir()
    (root / "locked.txt").write_text("one")
    (root / "fine.txt").write_text("fine")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    async with get_sessionmaker()() as s:
        await s.execute(update(Collection).values(auto_baseline_new=True))
        await s.commit()
        await scan_collection(s, await s.get(Collection, cid))

    real_stat = Path.stat

    def failing_stat(self, *args, **kwargs):
        if self.name == "locked.txt":
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", failing_stat)
    async with get_sessionmaker()() as s:
        summary = await scan_collection(s, await s.get(Collection, cid), deep=True)
        statuses = dict((await s.execute(select(FileEntry.relpath, FileEntry.status))).all())

    assert statuses == {"locked.txt": "new", "fine.txt": "ok"}
    assert summary.baselined == 1 and summary.result == "partial"


@pytest.mark.asyncio
async def test_a_file_cairn_cannot_track_does_not_make_an_empty_root_pass(cairn_env, monkeypatch):
    """A root holding only an un-storable name (never tracked) is, for the tracked files, empty."""
    from sqlalchemy import select

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import scanner

    root = cairn_env / "unstorable"
    root.mkdir()
    (root / "a.txt").write_text("a")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    async with get_sessionmaker()() as s:
        await scanner.scan_collection(s, await s.get(Collection, cid))

    (root / "a.txt").unlink()
    # The walk yields exactly one name, and it is one the datastore cannot hold.
    monkeypatch.setattr(scanner, "iter_relpaths", lambda *a, **k: iter(["bad\udcff.jpg"]))
    async with get_sessionmaker()() as s:
        summary = await scanner.scan_collection(s, await s.get(Collection, cid))
        statuses = set(await s.scalars(select(FileEntry.status)))

    assert summary.refused == scanner.REFUSAL_EMPTY and summary.result == "error"
    assert statuses == {"new"}


# ================================================================================================
# Verifier round: gaps found by the independent audit
# ================================================================================================


@pytest.mark.asyncio
async def test_a_file_whose_bytes_could_not_be_read_is_not_graduated(cairn_env, monkeypatch):
    """Failing media: the name lists, `stat` works, reading the bytes raises. The file is present
    (never missing) and was NOT verified — a deep pass must not graduate it from `new`."""
    from sqlalchemy import select
    from sqlalchemy import update as sa_update

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import scanner

    root, cid = await _collection(cairn_env, "eio", {"bad.bin": "payload", "fine.txt": "fine"})
    async with get_sessionmaker()() as s:
        await s.execute(sa_update(Collection).values(auto_baseline_new=True))
        await s.commit()

    real_hash = scanner._hash

    async def eio(path):
        if str(path).endswith("bad.bin"):
            raise OSError(5, "Input/output error", str(path))
        return await real_hash(path)

    monkeypatch.setattr(scanner, "_hash", eio)
    summary = await _scan(cid, deep=True)

    async with get_sessionmaker()() as s:
        statuses = dict((await s.execute(select(FileEntry.relpath, FileEntry.status))).all())
    assert statuses == {"bad.bin": "new", "fine.txt": "ok"}
    assert summary.result == "partial" and summary.errors == 1 and summary.missing == 0


@pytest.mark.asyncio
async def test_a_continuous_outage_is_re_announced_once_a_day(cairn_env, alerts):
    """Scans more frequent than the limit: a refused run always exists inside the window, so the
    limit must be keyed on the last alert ATTEMPT. Hourly refusals for 26 hours → two alerts."""
    import shutil

    root, cid = await _collection(cairn_env, "hourly", {"a.txt": "a"})
    shutil.rmtree(root)
    alerts.clear()

    for _hour in range(26):
        await _scan(cid)
        await _age_runs_by(cid, hours=1)

    assert len(alerts) == 2
    assert all("refused" in a.summary for a in alerts)


async def _age_runs_by(cid: int, *, hours: float) -> None:
    """Shift every run of the collection, and its alert-attempt marker, `hours` further back."""
    from sqlalchemy import select

    from src.database import get_sessionmaker
    from src.models.db import AppSetting, Run

    async with get_sessionmaker()() as s:
        for run in await s.scalars(select(Run).where(Run.collection_id == cid)):
            run.started = run.started - timedelta(hours=hours)
        for row in await s.scalars(
            select(AppSetting).where(AppSetting.key.like(f"safety_alert_at:{cid}:%"))
        ):
            row.value = (datetime.fromisoformat(row.value) - timedelta(hours=hours)).isoformat()
        await s.commit()


@pytest.mark.asyncio
async def test_a_metadata_preserving_restore_of_a_corrupted_file_closes_on_a_quick_scan(
    cairn_env, alerts
):
    """restic / `rsync -a` put the original bytes back under the ORIGINAL size and mtime — which,
    for a corrupted file, are exactly what is on record. A quick scan must still re-read it."""
    root, cid, fid = await _corrupted(cairn_env, "c-preserve")
    assert (await _file(cid, "x.bin")).status == "modified"

    _corrupt(root / "x.bin", "ORIGINAL-1")  # original bytes, size and mtime untouched
    alerts.clear()
    summary = await _scan(cid)  # a quick pass — "Scan now"

    row = await _file(cid, "x.bin")
    assert row.status == "ok" and row.prior_sha256 is None
    assert summary.reverted == 1
    assert await _open_events(cid, fid) == []
    assert alerts == []


@needs_permissions
@pytest.mark.asyncio
async def test_an_unreadable_folder_shields_only_the_files_beneath_it(cairn_env, unlistable, alerts):
    """One folder that cannot be listed takes away knowledge of the files BENEATH it and nothing
    else: a deletion elsewhere in the collection is still found by the same pass."""
    root, cid = await _collection(
        cairn_env, "narrow",
        {"locked/a.txt": "a", "locked/deep/b.txt": "b", "open/gone.txt": "g", "open/stays.txt": "s"},
    )
    (root / "open" / "gone.txt").unlink()
    unlistable(root / "locked")
    alerts.clear()
    summary = await _scan(cid)

    statuses = {relpath: f.status for relpath, f in (await _files(cid)).items()}
    assert statuses["open/gone.txt"] == "missing"
    assert statuses["locked/a.txt"] != "missing" and statuses["locked/deep/b.txt"] != "missing"
    assert summary.missing == 1 and summary.walk_incomplete is True and summary.result == "partial"
    # Two alerts, about two different things: the deletion, and the folder that is not checked.
    assert sorted("could not be read" in a.summary for a in alerts) == [False, True]


@needs_permissions
@pytest.mark.asyncio
async def test_an_unreadable_folder_with_nothing_tracked_in_it_changes_nothing_else(
    cairn_env, unlistable, alerts
):
    """A root-owned `lost+found`, a NAS recycle folder: never listable by this process, nothing
    tracked inside. It must not switch off deletion detection, keep the deep pass owed, or alert."""
    root, cid = await _collection(cairn_env, "lostfound", {"a.txt": "a", "b.txt": "b"})
    (root / "lost+found").mkdir()
    unlistable(root / "lost+found")
    (root / "b.txt").unlink()
    alerts.clear()
    summary = await _scan(cid, deep=True)

    statuses = {relpath: f.status for relpath, f in (await _files(cid)).items()}
    assert statuses == {"a.txt": "new", "b.txt": "missing"}
    # Reported (the run is partial and names the folder) — but the tracked files were all covered.
    assert summary.result == "partial" and summary.errors == 1
    assert summary.walk_incomplete is False
    assert [a for a in alerts if "could not be read" in a.summary] == []
    run = await _last_run(cid)
    assert "unreadable" in (run.error_sample or "")


@pytest.mark.asyncio
async def test_a_deep_pass_that_did_not_complete_is_retried_a_day_later_not_every_tick(
    cairn_env, monkeypatch
):
    """A collection whose deep pass cannot complete must not take the one deep slot on every tick
    (starving the others) or re-hash itself every cadence: it backs off for a day."""
    import time as _time

    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import scheduler
    from src.services.scanner import RunSummary

    monkeypatch.setattr(scheduler, "_DEEP_RETRY_AFTER", {})
    _root_a, broken = await _collection(cairn_env, "abroken", {"a.txt": "a"})
    _root_b, healthy = await _collection(cairn_env, "bhealthy", {"b.txt": "b"})
    async with get_sessionmaker()() as s:
        for c in await s.scalars(select(Collection)):
            c.verify_cadence_seconds = 604800
            c.last_full_scan_at = None  # both owe a deep pass
        await s.commit()

    calls: list[tuple[int, bool]] = []

    async def fake_scan(session, collection, *, deep=False):
        calls.append((collection.id, deep))
        if collection.id == broken and deep:
            return RunSummary(collection_id=collection.id, result="error", refused="unreachable")
        return RunSummary(collection_id=collection.id)

    monkeypatch.setattr(scheduler.scanner, "scan_collection", fake_scan)

    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, _time.monotonic())
    async with get_sessionmaker()() as s:
        await scheduler.run_due_scans(s, {}, _time.monotonic())

    deep_calls = [cid for cid, deep in calls if deep]
    # The broken collection got exactly one deep attempt; the healthy one got its deep pass on the
    # next tick instead of being starved.
    assert deep_calls.count(broken) == 1
    assert deep_calls.count(healthy) == 1
    async with get_sessionmaker()() as s:
        assert (await s.get(Collection, broken)).last_full_scan_at is None
        assert (await s.get(Collection, healthy)).last_full_scan_at is not None
