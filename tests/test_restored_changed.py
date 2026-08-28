"""A file that comes back with different bytes is not "restored" (#21, guard-proof-and-restore-integrity).

The restore branch used to open with ``row.sha256 = await _hash(full)`` — it destroyed the only
record of what the file used to be and *then* declared it healthy, so whatever bytes turned up at
the path were adopted as the baseline and every red count cleared. These tests pin the comparison
that now happens first, in both directions, and pin the things that must NOT quietly undo it:
churn mode, move reconciliation, auto-baseline, and the restore-ack's kind scoping.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_restored_changed.py``
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from tests.conftest import seed_collection


def _sha(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


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
            select(FileEntry).where(
                FileEntry.collection_id == cid, FileEntry.relpath == relpath
            )
        )


async def _events(cid: int, *, kind: str | None = None, relpath: str | None = None):
    """Events of the collection, oldest first; optionally filtered by kind and/or file."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    async with get_sessionmaker()() as s:
        stmt = select(Event).where(Event.collection_id == cid).order_by(Event.id)
        if kind is not None:
            stmt = stmt.where(Event.kind == kind)
        if relpath is not None:
            fid = await s.scalar(
                select(FileEntry.id).where(
                    FileEntry.collection_id == cid, FileEntry.relpath == relpath
                )
            )
            stmt = stmt.where(Event.file_id == fid)
        return list(await s.scalars(stmt))


@pytest.fixture
def no_stamping(monkeypatch):
    """Neutralize the post-scan stamp pass: these tests assert classification, not notarization.

    ``scan_collection`` calls ``proofs.stamp_pending`` for a ``perfile`` collection, which would
    shell out to ``ots`` and hit a calendar. Stubbing it keeps ``ots_state='pending'`` observable —
    the queued-for-re-stamp assertion is the point.
    """
    from src.services import proofs

    async def _noop(session, collection):
        return 0

    monkeypatch.setattr(proofs, "stamp_pending", _noop)


# --- the benign direction is untouched (task 3.9) -----------------------------------------------


@pytest.mark.asyncio
async def test_identical_restore_is_unchanged(cairn_env, no_stamping):
    """Same bytes back ⇒ exactly today's behaviour: ok, born-acked `restored`, missing alert closed."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root = cairn_env / "same"
    root.mkdir()
    (root / "a.txt").write_text("original")
    cid = await seed_collection(root, mode="worm", ots_mode="perfile")

    await _scan(cid)
    async with get_sessionmaker()() as s:  # pretend the first stamp landed and anchored
        row = await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == "a.txt")
        )
        row.ots_state = "complete"
        await s.commit()

    (root / "a.txt").unlink()
    await _scan(cid)
    assert (await _file(cid, "a.txt")).status == "missing"

    (root / "a.txt").write_text("original")
    summary = await _scan(cid)

    assert summary.restored == 1 and summary.restored_changed == 0 and summary.modified == 0
    assert summary.alarming == []

    row = await _file(cid, "a.txt")
    assert row.status == "ok"
    assert row.ots_state == "complete", (
        "an intact restore is never re-queued for stamping — the stored proof still commits to "
        "exactly these bytes"
    )

    restored = await _events(cid, kind="restored", relpath="a.txt")
    assert len(restored) == 1
    assert restored[0].acknowledged_at is not None and restored[0].acknowledged_by is None
    assert await _events(cid, kind="restored_changed") == []

    missing = await _events(cid, kind="missing", relpath="a.txt")
    assert missing and all(e.acknowledged_at is not None for e in missing), (
        "sprint 1's restore-ack must not regress"
    )


# --- the dangerous direction is detected (task 3.10) --------------------------------------------


@pytest.mark.asyncio
async def test_changed_restore_is_modified_and_alarms(cairn_env, no_stamping):
    """Different bytes back ⇒ `modified` + an UNACKNOWLEDGED `restored_changed` carrying both digests."""
    root = cairn_env / "changed"
    root.mkdir()
    (root / "deed.pdf").write_text("the real deed")
    cid = await seed_collection(root, mode="worm", ots_mode="perfile")

    await _scan(cid)
    (root / "deed.pdf").unlink()
    await _scan(cid)

    (root / "deed.pdf").write_text("a different document entirely")
    summary = await _scan(cid)

    assert summary.restored_changed == 1
    assert summary.modified == 1, "it is counted as a modification (runs.modified carries it)"
    assert summary.restored == 0
    assert ("restored_changed", "deed.pdf") in summary.alarming

    row = await _file(cid, "deed.pdf")
    assert row.status == "modified", "never `ok` — the bytes that came back are not the bytes that left"
    assert row.sha256 == _sha("a different document entirely"), (
        "the index still describes what is on disk now"
    )
    assert row.ots_state == "pending", "perfile: the new bytes are queued for their own proof"

    events = await _events(cid, kind="restored_changed", relpath="deed.pdf")
    assert len(events) == 1
    ev = events[0]
    assert ev.acknowledged_at is None, "it alarms; it is not born acknowledged"
    assert _sha("the real deed") in ev.detail and _sha("a different document entirely") in ev.detail
    assert await _events(cid, kind="restored", relpath="deed.pdf") == [], (
        "one reappearance is one event — no `restored` alongside it"
    )

    missing = await _events(cid, kind="missing", relpath="deed.pdf")
    assert missing and all(e.acknowledged_at is not None for e in missing), (
        "the file is no longer absent, so its `missing` alert still closes (design D5)"
    )
    assert all(e.acknowledged_by is None for e in missing)


@pytest.mark.asyncio
async def test_no_recorded_digest_means_no_alarm(cairn_env, no_stamping):
    """A legacy row with no recorded digest establishes nothing, so it alarms nothing."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root = cairn_env / "legacy"
    root.mkdir()
    (root / "old.txt").write_text("body")
    cid = await seed_collection(root, mode="worm")

    await _scan(cid)
    (root / "old.txt").unlink()
    await _scan(cid)

    async with get_sessionmaker()() as s:  # a pre-provenance row: no digest to compare against
        row = await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == "old.txt")
        )
        row.sha256 = None
        await s.commit()

    (root / "old.txt").write_text("something else")
    summary = await _scan(cid)

    assert summary.restored == 1 and summary.restored_changed == 0
    assert summary.alarming == []
    assert (await _file(cid, "old.txt")).status == "ok"
    restored = await _events(cid, kind="restored", relpath="old.txt")
    assert len(restored) == 1 and restored[0].acknowledged_at is not None
    assert "no digest" in (restored[0].detail or ""), (
        "the absence of a comparable digest is recorded, not silently glossed"
    )


# --- churn is exactly why the kind is new (task 3.11) -------------------------------------------


@pytest.mark.asyncio
async def test_changed_restore_alarms_in_churn_mode(cairn_env, no_stamping):
    """The reason `restored_changed` is not a reused `modified`: churn silences `modified` entirely.

    In a churn collection an ordinary content change re-baselines with no event at all. A file that
    was *absent* and came back different is not an ordinary edit, so it must still alarm — otherwise
    the fix for one false negative would have created another (design D4).
    """
    root = cairn_env / "churn"
    root.mkdir()
    (root / "y.txt").write_text("original-y")
    cid = await seed_collection(root, mode="churn")

    await _scan(cid)

    # Baseline: an ordinary churn edit is silent, and stays silent.
    (root / "y.txt").write_text("edited-y-in-place")
    churn_edit = await _scan(cid)
    assert churn_edit.alarming == [] and await _events(cid, kind="modified") == []
    assert (await _file(cid, "y.txt")).status == "ok"

    (root / "y.txt").unlink()
    await _scan(cid)
    (root / "y.txt").write_text("not what left")
    summary = await _scan(cid)

    assert summary.restored_changed == 1
    assert ("restored_changed", "y.txt") in summary.alarming, (
        "it must reach the alert dispatch in churn mode too"
    )
    assert (await _file(cid, "y.txt")).status == "modified", "not silently re-baselined"
    events = await _events(cid, kind="restored_changed", relpath="y.txt")
    assert len(events) == 1 and events[0].acknowledged_at is None


# --- the restore-ack keeps its scoping (task 3.12) ----------------------------------------------


@pytest.mark.asyncio
async def test_open_worm_modified_survives_a_changed_restore(cairn_env, no_stamping):
    """#12's rejected fix 7: the ack is `kind='missing'`-scoped, even for a changed reappearance."""
    root = cairn_env / "scoped"
    root.mkdir()
    (root / "a.txt").write_text("original-a")
    (root / "b.txt").write_text("original-b")
    cid = await seed_collection(root, mode="worm")

    await _scan(cid)
    (root / "a.txt").write_text("tampered-a-longer")  # open WORM `modified` on a.txt
    await _scan(cid)
    (root / "a.txt").unlink()
    (root / "b.txt").unlink()
    await _scan(cid)

    (root / "a.txt").write_text("a THIRD version of a")  # comes back different again
    summary = await _scan(cid)
    assert summary.restored_changed == 1

    a_modified = await _events(cid, kind="modified", relpath="a.txt")
    assert a_modified and all(e.acknowledged_at is None for e in a_modified), (
        "a different, still-unresolved claim about the same file — it is not cleared"
    )
    a_missing = await _events(cid, kind="missing", relpath="a.txt")
    assert a_missing and all(e.acknowledged_at is not None for e in a_missing)
    b_missing = await _events(cid, kind="missing", relpath="b.txt")
    assert b_missing and all(e.acknowledged_at is None for e in b_missing), (
        "another file's missing alert is untouched"
    )


# --- nothing downstream quietly clears it (task 3.13) -------------------------------------------


@pytest.mark.asyncio
async def test_changed_restore_survives_move_reconciliation(cairn_env, no_stamping):
    """A rename in the same scan must not swallow the alarm.

    ``_reconcile_moves`` pairs rows *created this scan* with files that went missing *this scan*. A
    restored row is neither, so it can never be reconciled away — pinned here because the failure
    would be silent: a wrong restore rendered as a tidy `moved` event.
    """
    root = cairn_env / "moves"
    root.mkdir()
    (root / "target.txt").write_text("target-original")
    (root / "mover.txt").write_text("mover-content-unique")
    cid = await seed_collection(root, mode="worm")

    await _scan(cid)
    (root / "target.txt").unlink()
    await _scan(cid)

    # One scan: target.txt comes back WRONG, and mover.txt is renamed (a genuine move).
    (root / "target.txt").write_text("target-IMPOSTOR")
    (root / "mover.txt").rename(root / "renamed.txt")
    summary = await _scan(cid)

    assert summary.moved == 1, "the genuine move still reconciles"
    assert summary.restored_changed == 1
    assert (await _file(cid, "target.txt")).status == "modified"
    rc = await _events(cid, kind="restored_changed", relpath="target.txt")
    assert len(rc) == 1 and rc[0].acknowledged_at is None
    assert await _events(cid, kind="moved", relpath="target.txt") == [], (
        "the wrong restore is not dressed up as a move"
    )


@pytest.mark.asyncio
async def test_changed_restore_is_not_auto_baselined_by_the_deep_pass(cairn_env, no_stamping):
    """Auto-baseline promotes `new` rows only — a restored-changed row is `modified`, so it stays.

    Auto-baseline is enabled in production (the Photos collection), and it runs on the deep pass,
    which is also the pass most likely to notice a restore. If it promoted this row the alarm would
    evaporate one deep scan after it was raised.
    """
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    root = cairn_env / "autobaseline"
    root.mkdir()
    (root / "doc.txt").write_text("doc-original")
    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s,
            user_id=uid,
            name="ab",
            root=str(root),
            mode="worm",
            ots_mode="perfile",
            auto_baseline_new=True,
        )
        cid = collection.id

    await _scan(cid, deep=True)
    (root / "doc.txt").unlink()
    await _scan(cid, deep=True)

    (root / "doc.txt").write_text("doc-IMPOSTOR")
    summary = await _scan(cid, deep=True)  # the deep pass both detects it AND auto-baselines
    assert summary.restored_changed == 1

    row = await _file(cid, "doc.txt")
    assert row.status == "modified", "auto-baseline must not graduate it to ok"
    assert row.ots_state == "pending"
    rc = await _events(cid, kind="restored_changed", relpath="doc.txt")
    assert len(rc) == 1 and rc[0].acknowledged_at is None

    # And a further deep pass (the file now sits unchanged at its new bytes) leaves it alone.
    later = await _scan(cid, deep=True)
    assert later.restored_changed == 0
    assert (await _file(cid, "doc.txt")).status == "modified"
    assert len(await _events(cid, kind="restored_changed", relpath="doc.txt")) == 1


# --- the alert dispatch names it ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_changed_restore_reaches_the_alert_dispatch(cairn_env, monkeypatch, no_stamping):
    """The batched alert fires for a changed reappearance, in a churn collection, naming the file."""
    from src.notify import dispatch as dispatch_mod

    sent: list = []

    async def _capture(alert, collection, settings):
        sent.append(alert)

    monkeypatch.setattr(dispatch_mod, "dispatch", _capture)

    root = cairn_env / "alerting"
    root.mkdir()
    (root / "photo.jpg").write_text("photo-bytes")
    cid = await seed_collection(root, mode="churn")

    await _scan(cid)
    (root / "photo.jpg").unlink()
    sent.clear()
    await _scan(cid)  # missing alert
    assert sent, "sanity: the missing alert fires"

    sent.clear()
    (root / "photo.jpg").write_text("different-photo-bytes")
    await _scan(cid)

    assert len(sent) == 1
    assert "photo.jpg" in sent[0].paths
    assert "came back changed" in sent[0].summary, sent[0].summary
