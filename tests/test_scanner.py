"""Scanner behavior: classification, fast-path hashing, WORM vs churn, accept.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_scanner.py``
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import select


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    return tmp_path


async def _make_collection(root: Path, mode: str = "worm", ots_mode: str = "none") -> int:
    from sqlalchemy import select as _select

    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(_select(User.id))
        collection = await create_collection(
            s, user_id=uid, name="c", root=str(root), mode=mode, ots_mode=ots_mode
        )
        return collection.id


async def _file(session, cid: int, relpath: str):
    from src.models.db import FileEntry

    return await session.scalar(
        select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == relpath)
    )


async def _events(session, cid: int, kind: str | None = None, unack: bool = False):
    from src.models.db import Event

    stmt = select(Event).where(Event.collection_id == cid)
    if kind:
        stmt = stmt.where(Event.kind == kind)
    if unack:
        stmt = stmt.where(Event.acknowledged_at.is_(None))
    return list(await session.scalars(stmt))


@pytest.mark.asyncio
async def test_worm_lifecycle(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import scanner
    from src.services.scanner import scan_collection

    root = cairn_env / "collection"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    # Count real hash calls to prove the fast-path skips unchanged files.
    calls = {"n": 0}
    real = scanner.sha256_file

    def counting(path, chunk=scanner.CHUNK):
        calls["n"] += 1
        return real(path, chunk)

    monkeypatch.setattr(scanner, "sha256_file", counting)

    # Scan 1: both files added + hashed.
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 2
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.status for f in files} == {"new"}
        assert len(await _events(s, cid, kind="added")) == 2
    assert calls["n"] == 2

    # Scan 2: unchanged → no re-hash.
    calls["n"] = 0
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 0 and summ.modified == 0
    assert calls["n"] == 0

    # Modify a.txt → modified (worm), only a re-hashed.
    (root / "a.txt").write_text("ALPHA has been modified and is longer")
    calls["n"] = 0
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 1
        assert (await _file(s, cid, "a.txt")).status == "modified"
        assert len(await _events(s, cid, kind="modified")) == 1
    assert calls["n"] == 1

    # Touch b.txt mtime only (same bytes) → not modified.
    bpath = root / "b.txt"
    st = bpath.stat()
    os.utime(bpath, (st.st_atime + 100, st.st_mtime + 100))
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 0
        assert (await _file(s, cid, "b.txt")).status == "new"  # pending status preserved
        assert len(await _events(s, cid, kind="modified")) == 1  # no new modified event

    # Delete b.txt → missing.
    bpath.unlink()
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.missing == 1
        assert (await _file(s, cid, "b.txt")).status == "missing"

    # Restore b.txt → restored.
    (root / "b.txt").write_text("beta")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.restored == 1
        assert (await _file(s, cid, "b.txt")).status == "ok"
        assert len(await _events(s, cid, kind="restored")) == 1


@pytest.mark.asyncio
async def test_informational_events_autoacked(cairn_env):
    """`added`/`restored` are born acknowledged (system ack); `missing`/worm-`modified` still nag."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "info"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    # Scan 1: two `added` events, each acknowledged at creation with no user → zero open events.
    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 2
        added = await _events(s, cid, kind="added")
        assert len(added) == 2
        assert all(e.acknowledged_at is not None and e.acknowledged_by is None for e in added)
        assert len(await _events(s, cid, unack=True)) == 0

    # Modify a → an unacknowledged `modified` nag.
    (root / "a.txt").write_text("alpha changed and is now longer")
    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
        assert len(await _events(s, cid, kind="modified", unack=True)) == 1

    # Delete b → unacknowledged `missing` nag.
    (root / "b.txt").unlink()
    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
        assert len(await _events(s, cid, kind="missing", unack=True)) == 1

    # Restore b → a `restored` event, also born acknowledged.
    (root / "b.txt").write_text("beta")
    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
        restored = await _events(s, cid, kind="restored")
        assert len(restored) == 1
        assert restored[0].acknowledged_at is not None
        assert restored[0].acknowledged_by is None


@pytest.mark.asyncio
async def test_churn_rebaseline_but_missing_still_nags(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "churn"
    root.mkdir()
    (root / "x.txt").write_text("one")
    (root / "y.txt").write_text("two")
    cid = await _make_collection(root, mode="churn")
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 2

    # Modify x in a churn collection → silent re-baseline, no nag.
    (root / "x.txt").write_text("one-changed")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 0
        assert (await _file(s, cid, "x.txt")).status == "ok"
        assert len(await _events(s, cid, kind="modified")) == 0

    # Delete y → missing still nags, even in churn.
    (root / "y.txt").unlink()
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.missing == 1
        assert len(await _events(s, cid, kind="missing", unack=True)) == 1


@pytest.mark.asyncio
async def test_accept_rebaselines_and_is_idempotent(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Collection, User
    from src.services.scanner import accept_collection, scan_collection

    root = cairn_env / "acc"
    root.mkdir()
    for n in ("p.txt", "q.txt", "r.txt"):
        (root / n).write_text(n)
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))  # p,q,r -> new

    # Modify p, delete q.
    (root / "p.txt").write_text("p changed and longer")
    (root / "q.txt").unlink()
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 1 and summ.missing == 1

    async with sm() as s:
        uid = await s.scalar(select(User.id))
        result = await accept_collection(s, await s.get(Collection, cid), uid)
        assert result["accepted"] == 2  # p (modified) + r (new)
        assert result["removed"] == 1  # q
        # The 3 `added` events are born acknowledged; only the modified + missing nags remain open.
        assert result["events_ack"] == 2

    async with sm() as s:
        assert (await _file(s, cid, "p.txt")).status == "ok"
        assert (await _file(s, cid, "r.txt")).status == "ok"
        assert await _file(s, cid, "q.txt") is None
        assert len(await _events(s, cid, unack=True)) == 0

    # Idempotent: nothing pending now.
    async with sm() as s:
        uid = await s.scalar(select(User.id))
        result = await accept_collection(s, await s.get(Collection, cid), uid)
        assert result == {"accepted": 0, "removed": 0, "events_ack": 0}


# --- deep verify (full re-hash) -------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_rehashes_every_unchanged_file(cairn_env, monkeypatch):
    """A deep scan re-hashes every tracked file even when nothing changed (quick scan skips)."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import scanner
    from src.services.scanner import scan_collection

    root = cairn_env / "deep"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    calls = {"n": 0}
    real = scanner.sha256_file

    def counting(path, chunk=scanner.CHUNK):
        calls["n"] += 1
        return real(path, chunk)

    monkeypatch.setattr(scanner, "sha256_file", counting)

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 2
    assert calls["n"] == 2

    # Quick scan: unchanged → fast-path, no re-hash.
    calls["n"] = 0
    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
    assert calls["n"] == 0

    # Deep scan: re-hashes both files despite no change; classifies nothing as modified.
    calls["n"] = 0
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.modified == 0
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.status for f in files} == {"new"}  # intact, status preserved
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_deep_detects_silent_bitrot_worm(cairn_env):
    """Bytes change with size+mtime intact: a quick scan misses it, a deep scan catches it."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "rot"
    root.mkdir()
    f = root / "img.raw"
    f.write_text("AAAAA")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 1

    # Simulate bit-rot: same byte length, original mtime restored exactly (via ns).
    st = f.stat()
    f.write_text("BBBBB")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))

    # Quick scan can't see it — size + mtime are unchanged, so the fast-path skips it.
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 0
        assert (await _file(s, cid, "img.raw")).status == "new"

    # Deep scan re-hashes everything and detects the corruption.
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.modified == 1
        assert (await _file(s, cid, "img.raw")).status == "modified"
        assert len(await _events(s, cid, kind="modified")) == 1


@pytest.mark.asyncio
async def test_deep_bitrot_churn_rebaselines(cairn_env):
    """In churn mode a deep-detected byte change silently re-baselines (no nag), hash updated."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "crot"
    root.mkdir()
    f = root / "data.bin"
    f.write_text("AAAAA")
    cid = await _make_collection(root, mode="churn")
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
        old_sha = (await _file(s, cid, "data.bin")).sha256

    st = f.stat()
    f.write_text("BBBBB")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.modified == 0
        row = await _file(s, cid, "data.bin")
        assert row.status == "ok"
        assert row.sha256 != old_sha  # re-baselined to the new bytes
        assert len(await _events(s, cid, kind="modified")) == 0


@pytest.mark.asyncio
async def test_deep_does_not_restamp_intact_files(cairn_env, monkeypatch):
    """A deep pass over intact perfile files re-queues nothing for OTS stamping."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services.scanner import scan_collection

    async def fake_stamp(session, collection):
        rows = list(
            await session.scalars(
                select(FileEntry).where(
                    FileEntry.collection_id == collection.id, FileEntry.ots_state == "pending"
                )
            )
        )
        for r in rows:
            r.ots_state = "complete"
        await session.commit()
        return len(rows)

    monkeypatch.setattr("src.services.proofs.stamp_pending", fake_stamp)

    root = cairn_env / "stamp"
    root.mkdir()
    (root / "img").write_text("AAAAA")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    # Scan 1: new file queued + stamped → complete.
    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 1
        assert (await _file(s, cid, "img")).ots_state == "complete"

    # Deep scan over the intact file must NOT re-queue it to pending or re-stamp it.
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.modified == 0
        assert (await _file(s, cid, "img")).ots_state == "complete"
        last = await s.scalar(
            select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()).limit(1)
        )
        assert last.deep is True
        assert last.stamped == 0  # nothing pending → nothing stamped


# --- move/rename reconciliation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_reconciled_to_single_moved_event(cairn_env):
    """A 1:1 content match (file relocated) → one `moved` event, no missing/added; the surviving
    row keeps its identity (first_seen, sha256) and proof (ots_path), and is left `ok`."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services.scanner import scan_collection

    root = cairn_env / "mv"
    root.mkdir()
    (root / "a.txt").write_text("the-relocated-bytes")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 1
        row = await _file(s, cid, "a.txt")
        orig_first_seen, orig_sha, orig_id = row.first_seen, row.sha256, row.id
        # Simulate a pre-existing proof on the row so we can assert it is carried forward verbatim.
        row.ots_path = "/proofs/1/a.txt.ots"
        row.ots_state = "complete"
        await s.commit()

    # Move a.txt → sub/b.txt (content unchanged), then scan.
    (root / "sub").mkdir()
    (root / "a.txt").rename(root / "sub" / "b.txt")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.moved == 1
        assert summ.added == 0 and summ.missing == 0

        # Exactly one surviving row: the original, repointed to the new path.
        assert await _file(s, cid, "a.txt") is None
        survivor = await _file(s, cid, "sub/b.txt")
        assert survivor is not None
        assert survivor.id == orig_id
        assert survivor.status == "ok"
        assert survivor.first_seen == orig_first_seen  # identity preserved
        assert survivor.sha256 == orig_sha
        assert survivor.ots_path == "/proofs/1/a.txt.ots"  # proof carried forward
        assert survivor.ots_state == "complete"  # NOT re-queued (not 'pending')

        # One informational `moved` event (old → new), born acknowledged; no missing/added events.
        moved = await _events(s, cid, kind="moved")
        assert len(moved) == 1
        assert moved[0].detail == "a.txt → sub/b.txt"
        assert moved[0].acknowledged_at is not None and moved[0].acknowledged_by is None
        assert await _events(s, cid, kind="missing") == []
        # The only `added` event is the baseline one for a.txt; the move added nothing.
        assert len(await _events(s, cid, kind="added")) == 1

        run = await s.scalar(
            select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()).limit(1)
        )
        assert run.moved == 1 and run.added == 0 and run.missing == 0


@pytest.mark.asyncio
async def test_ambiguous_content_does_not_reconcile(cairn_env, caplog):
    """When a content key matches more than one candidate, no move is inferred — it falls back to
    plain missing + added (logged), so a file's proof is never attached to the wrong path."""
    import logging

    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "amb"
    root.mkdir()
    (root / "orig.txt").write_text("shared-content")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 1

    # Move orig.txt → x.txt AND create y.txt with the SAME content: 1 missing maps to 2 added.
    (root / "orig.txt").rename(root / "x.txt")
    (root / "y.txt").write_text("shared-content")
    # The migration fixture runs alembic's fileConfig, which disables non-configured loggers;
    # re-enable cairn.scanner so its INFO fallback log is capturable.
    scanner_log = logging.getLogger("cairn.scanner")
    scanner_log.disabled = False
    scanner_log.propagate = True
    with caplog.at_level(logging.INFO, logger="cairn.scanner"):
        async with sm() as s:
            summ = await scan_collection(s, await s.get(Collection, cid))
            assert summ.moved == 0
            assert summ.added == 2 and summ.missing == 1
            assert (await _file(s, cid, "orig.txt")).status == "missing"
            assert await _file(s, cid, "x.txt") is not None
            assert await _file(s, cid, "y.txt") is not None
            assert await _events(s, cid, kind="moved") == []
            assert len(await _events(s, cid, kind="missing")) == 1
    assert "ambiguous" in caplog.text


@pytest.mark.asyncio
async def test_zero_byte_files_never_reconcile(cairn_env):
    """Empty files share one hash, so a moved empty file is not an unambiguous match → no move."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "zero"
    root.mkdir()
    (root / "empty.txt").write_text("")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))

    (root / "empty.txt").rename(root / "moved-empty.txt")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.moved == 0
        assert summ.missing == 1 and summ.added == 1
        assert await _events(s, cid, kind="moved") == []


@pytest.mark.asyncio
async def test_run_records_deep_flag(cairn_env):
    """A quick scan records deep=False; a deep scan records deep=True."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services.scanner import scan_collection

    root = cairn_env / "flag"
    root.mkdir()
    (root / "a.txt").write_text("x")
    cid = await _make_collection(root)
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid), deep=True)
    async with sm() as s:
        runs = list(
            await s.scalars(select(Run).where(Run.collection_id == cid).order_by(Run.id))
        )
        assert [r.deep for r in runs] == [False, True]


def _write_non_utf8_file(root: Path, content: bytes = b"latin-1 photo") -> bytes:
    """Create a file under root whose name is not valid UTF-8 (Latin-1 byte 0xe0 = 'à').

    Returns the bare bytes filename. os.walk will surface it as a lone-surrogate str
    ('1\\udce0.jpg') that SQLite cannot store — the case that wedged the Photos collection.
    """
    name = b"1\xe0.jpg"
    full = os.path.join(os.fsencode(str(root)), name)
    with open(full, "wb") as fh:
        fh.write(content)
    return name


@pytest.mark.asyncio
async def test_non_utf8_filename_is_skipped_not_fatal(cairn_env):
    """A non-UTF-8 filename is skipped (no row), the scan finishes `partial`, peers track fine."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services.scanner import scan_collection

    root = cairn_env / "photos"
    root.mkdir()
    (root / "ok.jpg").write_bytes(b"clean name")
    _write_non_utf8_file(root)
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        # The storable file is tracked; the bad one is skipped and counted as an error → partial.
        assert summ.added == 1
        assert summ.errors == 1
        assert summ.result == "partial"

        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert [f.relpath for f in files] == ["ok.jpg"]  # no row for the un-storable name

        run = await s.scalar(select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()))
        assert run.result == "partial" and run.finished is not None


@pytest.mark.asyncio
async def test_non_utf8_filename_does_not_churn(cairn_env):
    """Re-scanning is stable: the skipped file never reads as missing/added, peers stay ok."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "photos"
    root.mkdir()
    (root / "ok.jpg").write_bytes(b"clean name")
    _write_non_utf8_file(root)
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        # Second pass: nothing new, nothing gone — the bad file is skipped again, not churned.
        assert summ.added == 0 and summ.missing == 0 and summ.modified == 0
        assert summ.errors == 1 and summ.result == "partial"
        assert (await _file(s, cid, "ok.jpg")).status == "new"  # storable peer untouched
        assert await _events(s, cid, kind="missing") == []
        # Only ok.jpg ever produced an `added` event (scan 1); the skipped file never does.
        assert len(await _events(s, cid, kind="added")) == 1


@pytest.mark.asyncio
async def test_scan_failure_finalizes_run_not_left_running(cairn_env, monkeypatch):
    """An unexpected exception mid-scan finalizes the run to error — never left `running`."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services import scanner
    from src.services.scanner import scan_collection

    root = cairn_env / "boom"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    cid = await _make_collection(root, mode="worm")
    sm = get_sessionmaker()

    # Force a non-OSError failure inside the scan body (escapes the per-file OSError guard).
    def boom(path, chunk=scanner.CHUNK):
        raise RuntimeError("simulated hashing failure")

    monkeypatch.setattr(scanner, "sha256_file", boom)

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.result == "error"

    async with sm() as s:
        runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        assert runs, "a run row must exist"
        # No run may be left `running`; the run is terminal with `finished` set.
        assert all(r.result != "running" for r in runs)
        assert all(r.finished is not None for r in runs)
        assert any(r.result == "error" for r in runs)


async def _set_auto_baseline(cid: int, value: bool) -> None:
    from src.database import get_sessionmaker
    from src.models.db import Collection

    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        c.auto_baseline_new = value
        await s.commit()


@pytest.mark.asyncio
async def test_auto_baseline_promotes_intact_new_on_deep(cairn_env):
    """With auto_baseline_new on, a deep pass graduates intact pre-existing `new` files to `ok`."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services.scanner import scan_collection

    root = cairn_env / "ab"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    cid = await _make_collection(root, mode="worm")
    await _set_auto_baseline(cid, True)
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 2
        assert {f.status for f in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid))} == {"new"}

    # Deep pass promotes both intact new files.
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.baselined == 2
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.status for f in files} == {"ok"}


@pytest.mark.asyncio
async def test_auto_baseline_quick_scan_does_not_promote(cairn_env):
    """A quick (non-deep) scan never auto-baselines, even with the flag on."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services.scanner import scan_collection

    root = cairn_env / "abq"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    cid = await _make_collection(root, mode="worm")
    await _set_auto_baseline(cid, True)
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))           # -> new
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))    # quick re-scan
        assert summ.baselined == 0
        assert (await _file(s, cid, "a.txt")).status == "new"


@pytest.mark.asyncio
async def test_auto_baseline_off_keeps_new(cairn_env):
    """With the flag off (default), even a deep pass leaves new files new."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "aboff"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    cid = await _make_collection(root, mode="worm")  # auto_baseline_new defaults False
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.baselined == 0
        assert (await _file(s, cid, "a.txt")).status == "new"


@pytest.mark.asyncio
async def test_auto_baseline_never_touches_modified_or_missing(cairn_env):
    """Auto-baseline only promotes intact new files — modified and missing are left as issues."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root = cairn_env / "abmix"
    root.mkdir()
    (root / "keep.txt").write_text("intact-new")
    (root / "mod.txt").write_text("original")
    (root / "gone.txt").write_text("to-delete")
    cid = await _make_collection(root, mode="worm")
    await _set_auto_baseline(cid, True)
    sm = get_sessionmaker()

    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 3  # all new

    # Change one file's bytes, delete another; leave keep.txt intact.
    (root / "mod.txt").write_text("tampered-different-length")
    (root / "gone.txt").unlink()

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid), deep=True)
        assert summ.baselined == 1                                   # only keep.txt graduated
        assert (await _file(s, cid, "keep.txt")).status == "ok"
        assert (await _file(s, cid, "mod.txt")).status == "modified"
        assert (await _file(s, cid, "gone.txt")).status == "missing"
