"""`accept_files` — accept an explicit set of file records of one collection (unify-issue-model, D6).

Acceptance criteria: requirement "Accept can act on an explicit set of file records" in
``openspec/changes/unify-issue-model/specs/integrity-scanning/spec.md``.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_accept_files.py``
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from tests.conftest import seed_collection


async def _user_id() -> int:
    from src.database import get_sessionmaker
    from src.models.db import User

    async with get_sessionmaker()() as s:
        return await s.scalar(select(User.id))


async def _add_file(cid: int, relpath: str, status: str, *, open_event: str | None = None):
    """Insert one file row directly (plus, optionally, one open event of kind `open_event`)."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        f = FileEntry(
            collection_id=cid,
            relpath=relpath,
            size=3,
            mtime=1.0,
            sha256="0" * 64,
            status=status,
            first_seen=now,
            last_checked=now,
            last_changed=now,
            ots_state="none",
        )
        s.add(f)
        await s.flush()
        if open_event:
            s.add(Event(collection_id=cid, file_id=f.id, kind=open_event, detected_at=now))
        await s.commit()
        return f.id


async def _accept(cid: int, ids, user_id):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import accept_files

    async with get_sessionmaker()() as s:
        return await accept_files(s, await s.get(Collection, cid), ids, user_id)


async def _row(fid: int):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.get(FileEntry, fid)


async def _events(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        return list(
            await s.scalars(select(Event).where(Event.collection_id == cid).order_by(Event.id))
        )


async def _two_collections(cairn_env):
    a = cairn_env / "alpha"
    b = cairn_env / "beta"
    a.mkdir()
    b.mkdir()
    return await seed_collection(a, ots_mode="none"), await seed_collection(b, ots_mode="none")


@pytest.mark.asyncio
async def test_only_the_named_records_are_accepted(cairn_env):
    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    ids = [await _add_file(cid, f"m{i}.txt", "missing", open_event="missing") for i in range(4)]

    out = await _accept(cid, ids[:2], uid)

    assert out == {"accepted": 0, "removed": 2, "events_ack": 2}
    assert await _row(ids[0]) is None and await _row(ids[1]) is None
    for fid in ids[2:]:
        r = await _row(fid)
        assert r is not None and r.status == "missing"
    events = await _events(cid)
    by_file = {e.file_id: e for e in events if e.file_id is not None}
    assert set(by_file) == set(ids[2:])
    assert all(e.acknowledged_at is None for e in by_file.values()), "others' alerts untouched"
    detached = [e for e in events if e.file_id is None]
    assert len(detached) == 2
    assert all(e.acknowledged_at is not None and e.acknowledged_by == uid for e in detached)


@pytest.mark.asyncio
async def test_changed_and_missing_records_in_one_set(cairn_env):
    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    mod = await _add_file(cid, "dir/changed.txt", "modified", open_event="modified")
    miss = await _add_file(cid, "dir/gone.txt", "missing", open_event="missing")
    other = await _add_file(cid, "dir/untouched.txt", "modified", open_event="modified")

    out = await _accept(cid, [mod, miss], uid)

    assert out == {"accepted": 1, "removed": 1, "events_ack": 2}
    assert (await _row(mod)).status == "ok"
    assert await _row(miss) is None
    assert (await _row(other)).status == "modified"
    events = await _events(cid)
    mod_ev = [e for e in events if e.file_id == mod]
    assert len(mod_ev) == 1 and mod_ev[0].acknowledged_at is not None
    assert mod_ev[0].acknowledged_by == uid
    detached = [e for e in events if e.file_id is None]
    assert len(detached) == 1 and detached[0].kind == "missing"
    assert detached[0].detail == "dir/gone.txt", "the detached event keeps the path it was about"
    assert detached[0].acknowledged_at is not None
    other_ev = [e for e in events if e.file_id == other]
    assert other_ev and other_ev[0].acknowledged_at is None


@pytest.mark.asyncio
async def test_a_record_of_another_collection_is_not_touched(cairn_env):
    cid, other_cid = await _two_collections(cairn_env)
    uid = await _user_id()
    mine = await _add_file(cid, "a.txt", "modified", open_event="modified")
    foreign_mod = await _add_file(other_cid, "b.txt", "modified", open_event="modified")
    foreign_miss = await _add_file(other_cid, "c.txt", "missing", open_event="missing")

    out = await _accept(cid, [mine, foreign_mod, foreign_miss], uid)

    assert out == {"accepted": 1, "removed": 0, "events_ack": 1}
    assert (await _row(foreign_mod)).status == "modified"
    assert (await _row(foreign_miss)).status == "missing"
    foreign = await _events(other_cid)
    assert len(foreign) == 2
    assert all(e.acknowledged_at is None and e.file_id is not None for e in foreign)


@pytest.mark.asyncio
async def test_a_record_that_is_already_healthy_is_left_alone(cairn_env):
    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    healthy = await _add_file(cid, "fine.txt", "ok", open_event="modified")

    out = await _accept(cid, [healthy], uid)

    assert out == {"accepted": 0, "removed": 0, "events_ack": 0}
    r = await _row(healthy)
    assert r is not None and r.status == "ok"
    ev = await _events(cid)
    assert len(ev) == 1 and ev[0].acknowledged_at is None, "an ok row's event is not closed"


@pytest.mark.asyncio
async def test_a_very_large_set_is_applied_whole_in_one_transaction(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry
    from src.services import scanner

    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    keep = await _add_file(cid, "keep.txt", "missing", open_event="missing")

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        rows = [
            FileEntry(
                collection_id=cid,
                relpath=f"big/{i:05d}.bin",
                size=1,
                mtime=1.0,
                sha256="1" * 64,
                status="missing",
                first_seen=now,
                last_checked=now,
                last_changed=now,
                ots_state="none",
            )
            for i in range(2500)
        ]
        s.add_all(rows)
        await s.flush()
        s.add_all(
            Event(collection_id=cid, file_id=r.id, kind="missing", detected_at=now) for r in rows
        )
        await s.commit()
        ids = [r.id for r in rows]

    commits = 0
    real_commit = scanner.AsyncSession.commit

    async def _counting_commit(self):
        nonlocal commits
        commits += 1
        return await real_commit(self)

    monkeypatch.setattr(scanner.AsyncSession, "commit", _counting_commit)
    out = await _accept(cid, ids, uid)
    monkeypatch.setattr(scanner.AsyncSession, "commit", real_commit)

    assert commits == 1, "the whole set is applied in ONE transaction"
    assert out == {"accepted": 0, "removed": 2500, "events_ack": 2500}
    async with get_sessionmaker()() as s:
        left = await s.scalar(
            select(func.count()).select_from(FileEntry).where(FileEntry.collection_id == cid)
        )
        assert left == 1
        detached = list(
            await s.scalars(select(Event).where(Event.collection_id == cid, Event.file_id.is_(None)))
        )
    assert len(detached) == 2500
    assert all(e.acknowledged_at is not None for e in detached)
    assert {e.detail for e in detached} == {f"big/{i:05d}.bin" for i in range(2500)}
    assert (await _row(keep)).status == "missing"


@pytest.mark.asyncio
async def test_duplicate_ids_do_not_double_count(cairn_env):
    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    mod = await _add_file(cid, "a.txt", "modified", open_event="modified")
    miss = await _add_file(cid, "b.txt", "missing", open_event="missing")

    out = await _accept(cid, [mod, mod, miss, miss, mod], uid)

    assert out == {"accepted": 1, "removed": 1, "events_ack": 2}


@pytest.mark.asyncio
async def test_empty_list_is_a_noop_that_commits(cairn_env, monkeypatch):
    from src.services import scanner

    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    mod = await _add_file(cid, "a.txt", "modified", open_event="modified")

    commits = 0
    real_commit = scanner.AsyncSession.commit

    async def _counting_commit(self):
        nonlocal commits
        commits += 1
        return await real_commit(self)

    monkeypatch.setattr(scanner.AsyncSession, "commit", _counting_commit)
    out = await _accept(cid, [], uid)
    monkeypatch.setattr(scanner.AsyncSession, "commit", real_commit)

    assert out == {"accepted": 0, "removed": 0, "events_ack": 0}
    assert commits == 1
    assert (await _row(mod)).status == "modified"
    assert (await _events(cid))[0].acknowledged_at is None


@pytest.mark.asyncio
async def test_previously_acknowledged_event_keeps_its_original_ack(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Event

    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    mod = await _add_file(cid, "a.txt", "modified", open_event="modified")
    miss = await _add_file(cid, "b.txt", "missing", open_event="missing")
    earlier = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    async with get_sessionmaker()() as s:
        for fid, kind in ((mod, "modified"), (miss, "modified")):
            s.add(
                Event(
                    collection_id=cid,
                    file_id=fid,
                    kind=kind,
                    detected_at=earlier - timedelta(days=1),
                    acknowledged_at=earlier,
                    acknowledged_by=None,
                )
            )
        await s.commit()

    out = await _accept(cid, [mod, miss], uid)

    assert out["events_ack"] == 2, "only the two events this call closed are counted"
    events = await _events(cid)
    old = [e for e in events if e.detected_at.replace(tzinfo=timezone.utc) < earlier]
    assert len(old) == 2
    for e in old:
        assert e.acknowledged_at.replace(tzinfo=timezone.utc) == earlier
        assert e.acknowledged_by is None
    fresh = [e for e in events if e not in old]
    assert all(e.acknowledged_by == uid for e in fresh)


@pytest.mark.asyncio
async def test_detached_event_detail_carries_relpath_without_clobbering(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Event

    cid, _ = await _two_collections(cairn_env)
    uid = await _user_id()
    miss = await _add_file(cid, "deep/folder/photo.jpg", "missing", open_event="missing")
    async with get_sessionmaker()() as s:
        s.add(
            Event(
                collection_id=cid,
                file_id=miss,
                kind="moved",
                detail="old.jpg → deep/folder/photo.jpg",
                detected_at=datetime.now(timezone.utc),
                acknowledged_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()

    await _accept(cid, [miss], uid)

    events = await _events(cid)
    assert all(e.file_id is None for e in events)
    details = {e.kind: e.detail for e in events}
    assert details["missing"] == "deep/folder/photo.jpg"
    assert details["moved"] == "old.jpg → deep/folder/photo.jpg", "existing detail kept"
