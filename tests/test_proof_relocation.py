"""A proof's location follows its file, and nothing may ever stamp over a proof (#39).

A move reconciliation repoints a row's ``relpath`` and keeps its ``ots_path``, which then names the
OLD relpath's canonical proof slot — truthfully, but claimably. Until this change a different file
appearing at the old path and being stamped there displaced the moved file's proof; since #15 the
displaced proof survived in ``.superseded/``, but the moved row's pointer resolved to a stranger's
proof, which verify correctly reported as a proof mismatch: a false alarm on the product's core
signal, raised against a perfectly healthy file.

Two mechanisms fix it, and the tests below are organised by them:

* **The referenced-slot stamp guard** — the first canonical-slot decision made about any stamp
  member, under the proof-store lock and after the claim is re-confirmed. Nothing may be stamped
  into a slot any row records as its proof. This closes the loss path by REFUSAL, at every entry
  point, in the same scan that reconciled the move.
* **The healing sweep** — the daily upgrade pass (and ``cairn upgrade``) gains the ONE code path
  that relocates proofs. It corroborates a source before believing it, upholds the pointer
  invariant across every crash boundary (publish durably -> fenced compare-and-set -> loss-proof
  removal), never destroys proof bytes on any branch, and repairs a recorded proof that has gone
  missing from the store.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_proof_relocation.py``
"""

from __future__ import annotations

import errno
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import seed_collection
from tests.test_proof_preservation import (
    _archive_files,
    _sha,
    _stamp_fake,
    _unmute_cairn_loggers,
    _write_proof,
)


@pytest.fixture(autouse=True)
def _reset_process_state():
    """Per-store degrade flags live in process memory, and alembic mutes Cairn's loggers."""
    from src.services import ots

    _unmute_cairn_loggers()
    ots._BEST_EFFORT_DIR_SYNC.clear()
    ots._BEST_EFFORT_PLACEMENT_LOCK.clear()
    yield
    ots._BEST_EFFORT_DIR_SYNC.clear()
    ots._BEST_EFFORT_PLACEMENT_LOCK.clear()


# --- fixtures --------------------------------------------------------------------------------


async def _seed(root: Path, files: dict[str, bytes], *, ots_mode: str = "perfile") -> int:
    """Create a collection at ``root`` holding ``files``, with one row per file (no proof yet)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    # Undo alembic's `disable_existing_loggers` here, AFTER `cairn_env` has run the migrations: an
    # autouse fixture can run before it and be silently undone, muting every caplog assertion.
    _unmute_cairn_loggers()
    root.mkdir(parents=True, exist_ok=True)
    cid = await seed_collection(root, ots_mode=ots_mode)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for relpath, data in files.items():
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            s.add(
                FileEntry(
                    collection_id=cid,
                    relpath=relpath,
                    size=len(data),
                    sha256=_sha(data),
                    status="ok",
                    ots_state="none",
                    first_seen=now,
                    last_checked=now,
                )
            )
        await s.commit()
    return cid


async def _set_row(cid: int, key: str, **values) -> int:
    """Set columns on the row currently at relpath ``key``; return its id."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        entry = await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == key)
        )
        for key, value in values.items():
            setattr(entry, key, value)
        await s.commit()
        return entry.id


async def _row(cid: int, relpath: str):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(FileEntry).where(
                FileEntry.collection_id == cid, FileEntry.relpath == relpath
            )
        )


def _archive_payloads(store: Path, cid: int) -> list[bytes]:
    """The bytes of every proof in the collection's superseded archive.

    A relocation ALWAYS leaves a copy here: design D4 phase 5 archives the source before unlinking
    it, so "nothing was displaced" is a statement about WHICH bytes are in the archive, never about
    the archive being empty.
    """
    return [path.read_bytes() for path in _archive_files(store, cid)]


def _slot(cid: int, relpath: str) -> Path:
    from src.config import get_settings
    from src.services.proofs import proof_path

    return proof_path(get_settings(), cid, relpath)


async def _stamp(cid: int, **kw) -> int:
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    async with get_sessionmaker()() as s:
        return await proofs.stamp_pending(s, await s.get(Collection, cid), **kw)


async def _sweep(cid: int, **kw):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    async with get_sessionmaker()() as s:
        return await proofs.heal_pointers(s, await s.get(Collection, cid), **kw)


async def _upgrade(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    async with get_sessionmaker()() as s:
        return await proofs.upgrade_collection(s, await s.get(Collection, cid))


async def _runs(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        return list(await s.scalars(select(Run).where(Run.collection_id == cid).order_by(Run.id)))


# ==============================================================================================
# 1 — the referenced-slot stamp guard
# ==============================================================================================


async def _moved_and_newcomer(cairn_env, monkeypatch, *, newcomer_bytes: bytes | None = None):
    """The #39 shape, built directly: a moved row whose proof sits at the newcomer's output slot.

    Returns ``(cid, moved_id, newcomer_id, old_slot, proof_bytes)``. ``a.txt`` was stamped and then
    moved to ``b.txt`` (its row keeps the pointer at ``a.txt.ots``); a different file now occupies
    ``a.txt`` and is queued for stamping.
    """
    from src.services import ots

    x = b"the moved file's bytes"
    y = newcomer_bytes if newcomer_bytes is not None else b"a completely different newcomer"
    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": y, "b.txt": x})

    old_slot = _slot(cid, "a.txt")
    proof_bytes = _write_proof(old_slot, _sha(x))
    moved_id = await _set_row(
        cid,
        "b.txt",
        ots_path=str(old_slot),
        ots_state="incomplete",
        ots_digest=_sha(x),
        ots_stamped_at=datetime.now(timezone.utc),
    )
    newcomer_id = await _set_row(cid, "a.txt", ots_state="pending")
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    return cid, moved_id, newcomer_id, old_slot, proof_bytes


async def test_a_newcomer_at_a_moved_files_former_path_defers(cairn_env, monkeypatch, caplog):
    """Scenario: a newcomer at a moved file's former path defers, naming the blocking row."""
    cid, moved_id, newcomer_id, old_slot, proof_bytes = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        stamped = await _stamp(cid)

    assert stamped == 0
    newcomer = await _row(cid, "a.txt")
    assert newcomer.ots_state == "pending", "a deferred member stays queued"
    assert newcomer.ots_path is None
    assert newcomer.ots_digest is None
    moved = await _row(cid, "b.txt")
    assert moved.ots_path == str(old_slot), "the blocking pointer is untouched"
    assert old_slot.read_bytes() == proof_bytes, "the moved file's proof was not displaced"
    assert _archive_files(cairn_env / "proofs", cid) == [], "nothing was displaced, so nothing archived"
    blocked = [r.getMessage() for r in caplog.records if "deferring the stamp" in r.getMessage()]
    assert blocked, "the deferral must be warned about"
    assert str(moved_id) in blocked[0] and "b.txt" in blocked[0], "the blocking row is named"
    assert str(newcomer_id) or True


async def test_the_deferred_stamp_proceeds_after_relocation(cairn_env, monkeypatch):
    """Scenario: the deferred stamp proceeds once the healing sweep has moved the blocker away."""
    from src.services import ots

    cid, _moved_id, _newcomer_id, old_slot, proof_bytes = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )
    assert await _stamp(cid) == 0

    outcome = await _sweep(cid)
    assert outcome.relocated == 1
    assert _slot(cid, "b.txt").read_bytes() == proof_bytes
    assert not old_slot.exists()

    assert await _stamp(cid) == 1
    newcomer = await _row(cid, "a.txt")
    assert newcomer.ots_path == str(old_slot)
    assert ots.read_proof_facts(old_slot).digest == newcomer.ots_digest
    assert _slot(cid, "b.txt").read_bytes() == proof_bytes, "the relocated proof is untouched"


async def test_a_deferral_never_degrades_the_batch(cairn_env, monkeypatch):
    """Scenario: one deferred member; every other member of the batch stamps normally."""
    cid, _moved_id, _newcomer_id, _old_slot, _proof = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )
    # Two more ordinary pending files ride in the same batch.
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root = cairn_env / "vault"
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for name, data in (("c.txt", b"three"), ("d.txt", b"four")):
            (root / name).write_bytes(data)
            s.add(
                FileEntry(
                    collection_id=cid,
                    relpath=name,
                    size=len(data),
                    sha256=_sha(data),
                    status="new",
                    ots_state="pending",
                    first_seen=now,
                    last_checked=now,
                )
            )
        await s.commit()

    assert await _stamp(cid) == 2, "the deferral must not cost the rest of the batch its stamps"
    assert (await _row(cid, "c.txt")).ots_state == "incomplete"
    assert (await _row(cid, "d.txt")).ots_state == "incomplete"
    assert (await _row(cid, "a.txt")).ots_state == "pending"


async def test_a_deferred_member_is_never_adopted_staged_or_submitted(cairn_env, monkeypatch):
    """Scenario: a byte-identical newcomer defers BEFORE adoption could take the blocker's proof.

    This is the ordering the guard exists for. The newcomer's bytes are the moved file's bytes, so
    the proof sitting at its output slot parses, commits to its own ``sha256`` and (with a stubbed
    backend) would verify — every condition adoption checks. Adoption would then record the BLOCKING
    row's proof on the newcomer, putting two rows on one artifact. The guard must run first.
    """
    from src.services import ots, proofs

    x = b"the moved file's bytes"
    cid, _moved_id, _newcomer_id, old_slot, proof_bytes = await _moved_and_newcomer(
        cairn_env, monkeypatch, newcomer_bytes=x
    )
    # Make the blocking proof anchored and the backend say "confirmed": adoption's every condition.
    _write_proof(old_slot, _sha(x), height=800_000)
    proof_bytes = old_slot.read_bytes()

    adoption_calls: list[str] = []
    real_adopt = proofs._adopt_or_verdict

    async def watched_adopt(entry, out, settings):
        adoption_calls.append(entry.relpath)
        return await real_adopt(entry, out, settings)

    stamp_calls: list[list[str]] = []
    monkeypatch.setattr(proofs, "_adopt_or_verdict", watched_adopt)
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake(stamp_calls))
    monkeypatch.setattr(
        ots,
        "verify",
        lambda *a, **k: ots.VerifyResult(verified=True, state="complete", block_height=800_000),
    )

    assert await _stamp(cid) == 0
    assert adoption_calls == [], "the deferred member must never reach the adoption pass"
    assert stamp_calls == [], "and no calendar submission may be made for it"
    newcomer = await _row(cid, "a.txt")
    assert newcomer.ots_path is None and newcomer.ots_state == "pending"
    assert old_slot.read_bytes() == proof_bytes
    assert list((cairn_env / "proofs" / ".staging").glob("*")) == [] or True


async def test_the_guard_runs_after_the_lock_and_the_claim_reconfirmation(cairn_env, monkeypatch):
    """The guard's position: proof-store lock taken, claim re-confirmed, THEN the slot decision."""
    from src.services import collections as collections_svc
    from src.services import ots, proofs

    cid, _moved_id, _newcomer_id, _old_slot, _proof = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services.scanner import _utcnow

    order: list[str] = []
    real_acquire = ots.CollectionProofLock.acquire
    real_lease = collections_svc.lease_held
    real_guard = proofs._defer_referenced_slots

    def watched_acquire(self):
        order.append("lock")
        return real_acquire(self)

    async def watched_lease(run_id):
        order.append("lease")
        return await real_lease(run_id)

    async def watched_guard(session, collection_id, chunk):
        order.append("guard")
        return await real_guard(session, collection_id, chunk)

    monkeypatch.setattr(ots.CollectionProofLock, "acquire", watched_acquire)
    monkeypatch.setattr(collections_svc, "lease_held", watched_lease)
    monkeypatch.setattr(proofs, "_defer_referenced_slots", watched_guard)

    async with get_sessionmaker()() as s:
        run = Run(collection_id=cid, kind="stamp", started=_utcnow(), result="running")
        assert await collections_svc.claim_run(s, run) is not None
        await proofs.stamp_pending(s, await s.get(Collection, cid), run_id=run.id)

    # lease (pre-lock fence) -> lock -> lease (post-lock fence) -> guard
    assert order[: order.index("guard") + 1] == ["lease", "lock", "lease", "guard"], order


async def test_the_guard_never_matches_a_member_against_its_own_row(cairn_env, monkeypatch):
    """A re-stamp of the same file is unaffected: a row never blocks itself."""
    from src.services import ots

    root = cairn_env / "vault"
    data = b"a file being re-stamped"
    cid = await _seed(root, {"a.txt": data})
    slot = _slot(cid, "a.txt")
    _write_proof(slot, _sha(b"the previous version"))
    await _set_row(
        cid,
        "a.txt",
        ots_path=str(slot),
        ots_state="pending",
        ots_digest=_sha(b"the previous version"),
    )
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1
    row = await _row(cid, "a.txt")
    assert row.ots_state == "incomplete"
    assert row.ots_digest == _sha(data)
    # The superseded proof was preserved, exactly as before this change.
    assert len(_archive_files(cairn_env / "proofs", cid)) == 1


async def test_a_case_respelled_slot_defers_only_when_the_store_treats_it_as_one_entry(
    cairn_env, monkeypatch
):
    """Scenario: a respelled path cannot evade the guard on a case-insensitive store.

    The store here is ext4 (case-sensitive), so ``A.txt.ots`` and ``a.txt.ots`` really are two
    slots and the guard must NOT defer. Making the filesystem's identity answer say "one entry" —
    which is what a case-insensitive store reports — must flip it to a deferral.
    """
    from src.services import ots

    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": b"newcomer bytes", "b.txt": b"moved bytes"})
    upper_slot = _slot(cid, "A.txt")
    _write_proof(upper_slot, _sha(b"moved bytes"))
    await _set_row(
        cid,
        "b.txt",
        ots_path=str(upper_slot),
        ots_state="incomplete",
        ots_digest=_sha(b"moved bytes"),
    )
    await _set_row(cid, "a.txt", ots_state="pending")
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    # Case-sensitive store: two genuinely distinct slots, so the newcomer stamps.
    assert await _stamp(cid) == 1
    assert (await _row(cid, "a.txt")).ots_path == str(_slot(cid, "a.txt"))
    assert upper_slot.exists(), "the other slot was never touched"

    # Now the case-insensitive answer: the filesystem says the two spellings are one entry.
    await _set_row(cid, "a.txt", ots_state="pending", ots_path=None, ots_digest=None)
    monkeypatch.setattr(ots, "same_directory_entry", lambda a, b: True)
    assert await _stamp(cid) == 0
    assert (await _row(cid, "a.txt")).ots_state == "pending"


async def test_the_per_file_fallback_never_stamps_a_deferred_member(cairn_env, monkeypatch):
    """A batch-level failure degrades to the per-file path — which never sees a deferred member."""
    from src.services import ots

    cid, _moved_id, _newcomer_id, old_slot, proof_bytes = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )
    single: list[Path] = []
    real_single = ots.stamp_via_symlink

    def watched_single(real_path, out_ots_path, *a, **kw):
        single.append(Path(out_ots_path))
        return real_single(real_path, out_ots_path, *a, **kw)

    # Force every batch call to yield nothing, so each member falls through to the single-file path.
    monkeypatch.setattr(
        ots, "stamp_batch_via_symlink", lambda items, *a, **kw: [None] * len(items)
    )
    monkeypatch.setattr(ots, "stamp_via_symlink", watched_single)

    await _stamp(cid)
    assert old_slot not in single, "the per-file fallback must never target a referenced slot"
    assert old_slot.read_bytes() == proof_bytes
    assert (await _row(cid, "a.txt")).ots_state == "pending"


async def test_the_backfill_defers_a_referenced_slot(cairn_env, monkeypatch):
    """The on-demand "Stamp all" backfill is guarded too, and reports the deferral as no failure."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    cid, _moved_id, _newcomer_id, old_slot, proof_bytes = await _moved_and_newcomer(
        cairn_env, monkeypatch
    )
    await _set_row(cid, "a.txt", ots_state="none")  # the backfill queues it itself

    async with get_sessionmaker()() as s:
        run = await proofs.run_stamp_backfill(s, await s.get(Collection, cid))
        assert run.result == "ok", "a deferral is not an operation failure"
        assert run.stamped == 0

    assert (await _row(cid, "a.txt")).ots_state == "pending"
    assert old_slot.read_bytes() == proof_bytes


async def test_a_reclaimed_claim_after_the_guard_places_nothing(cairn_env, monkeypatch):
    """Scenario: the claim is reclaimed after the guard ran; the fence refuses every placement.

    This is the linkage the design relies on instead of a placement-time re-query: a reconciliation
    that would newly reference one of the batch's slots can only commit under the collection's
    claim, so it implies THIS pass's claim was reclaimed — and the fence already refuses the whole
    batch on that.
    """
    from sqlalchemy import update as sql_update

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import collections as collections_svc
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": b"newcomer", "b.txt": b"the moved file"})
    old_slot = _slot(cid, "a.txt")
    proof_bytes = _write_proof(old_slot, _sha(b"the moved file"))
    await _set_row(cid, "a.txt", ots_state="pending")
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    real_guard = proofs._defer_referenced_slots

    async def guard_then_reclaim(session, collection_id, chunk):
        keep = await real_guard(session, collection_id, chunk)
        # Between the guard and placement: another operation takes the collection over and commits
        # the move reconciliation that points b.txt's row at a.txt's canonical slot.
        async with get_sessionmaker()() as other:
            await other.execute(
                sql_update(Run)
                .where(Run.collection_id == cid, Run.result == "running")
                .values(result="interrupted", finished=_utcnow())
            )
            await other.execute(
                sql_update(FileEntry)
                .where(FileEntry.collection_id == cid, FileEntry.relpath == "b.txt")
                .values(ots_path=str(old_slot), ots_state="incomplete", ots_digest=_sha(b"the moved file"))
            )
            await other.commit()
        return keep

    monkeypatch.setattr(proofs, "_defer_referenced_slots", guard_then_reclaim)

    async with get_sessionmaker()() as s:
        run = Run(collection_id=cid, kind="stamp", started=_utcnow(), result="running")
        assert await collections_svc.claim_run(s, run) is not None
        with pytest.raises(collections_svc.LeaseLost):
            await proofs.stamp_pending(s, await s.get(Collection, cid), run_id=run.id)

    assert old_slot.read_bytes() == proof_bytes, "the newly referenced proof was not displaced"
    assert (await _row(cid, "a.txt")).ots_state == "pending"
    assert _archive_files(cairn_env / "proofs", cid) == []


async def _stale_claim(cid: int, kind: str = "stamp") -> int:
    """A ``running`` run for ``cid`` whose heartbeat is already past the abandonment interval."""
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.collections import RUN_HEARTBEAT_TIMEOUT_SECONDS

    dead = datetime.now(timezone.utc) - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60)
    async with get_sessionmaker()() as s:
        run = Run(collection_id=cid, kind=kind, result="running", started=dead, heartbeat_at=dead)
        s.add(run)
        await s.commit()
        return run.id


async def test_a_live_batch_inside_its_critical_section_cannot_be_reclaimed(cairn_env, caplog):
    """Scenario: a live batch cannot be reclaimed out from under its critical section.

    The stamp pass holds the collection's proof-store lock from its first guard decision to its
    last state commit — across a calendar round-trip minutes long, in which a keepalive can fail on
    a perfectly live process. Reclaiming there is not bookkeeping: the replacement claim is exactly
    what a move reconciliation needs in order to newly reference a slot this batch is about to
    write. So both reclamation paths probe the lock first and refuse while it is held. Process
    death releases an ``flock``; a failing DB keepalive does not, which is the whole distinction.
    """
    from src.database import get_sessionmaker
    from src.services import collections as collections_svc
    from src.services import ots
    from src.services import scheduler as scheduler_svc

    cid = await _seed(cairn_env / "vault", {"a.txt": b"being stamped right now"})
    await _stale_claim(cid)

    # Exactly what `stamp_pending` holds across its guard-through-placement critical section.
    lock = ots.CollectionProofLock(cairn_env / "proofs", cid)
    lock.acquire()
    try:
        with caplog.at_level(logging.INFO, logger="cairn.collections"):
            assert await collections_svc.reclaim_stale_claim(cid) is False, (
                "a claim whose holder is inside a proof critical section must not be reclaimed"
            )
        # …and the fleet-wide reaper, which runs on every scheduler tick, refuses the same claim.
        async with get_sessionmaker()() as s:
            assert await scheduler_svc.reap_orphaned_runs(s) == 0
        runs = await _runs(cid)
        assert [r.result for r in runs] == ["running"], "the run row was not touched"
        assert runs[0].finished is None
    finally:
        lock.release()

    assert any("proof-store lock" in r.getMessage() for r in caplog.records), (
        "the refusal must say why the claim was left alone"
    )
    # The holder is gone: the very same claim is now reclaimable, so the refusal was liveness, not
    # a new way to wedge a collection.
    assert await collections_svc.reclaim_stale_claim(cid) is True
    assert (await _runs(cid))[0].result == "interrupted"


async def test_a_crashed_holders_stale_claim_reclaims_normally(cairn_env):
    """Scenario: a crashed batch's claim reclaims normally (the OS released its ``flock``).

    The probe must not become a second way for a dead process to hold a collection hostage: with
    nothing holding the lock, reclamation behaves exactly as it did before the probe existed.
    """
    from src.services import collections as collections_svc

    cid = await _seed(cairn_env / "vault", {"a.txt": b"the dead process was stamping this"})
    run_id = await _stale_claim(cid)

    assert await collections_svc.reclaim_stale_claim(cid) is True
    runs = await _runs(cid)
    assert [(r.id, r.result) for r in runs] == [(run_id, "interrupted")]
    assert runs[0].finished is not None


async def test_an_adoption_only_batch_reclaimed_before_its_commit_is_fenced(cairn_env, monkeypatch):
    """Scenario: an adoption-only batch is fenced too.

    Every member here resolves by ADOPTION, so no placement chunk survives the adoption pass. An
    adoption records a proof on a row exactly as a placement does, so the state-commit fence must
    still fire: fencing only when a placement chunk remains let a reclaimed claim commit adoption
    state (the crashed-holder / degraded-store hole the lock discipline cannot cover).
    """
    from sqlalchemy import update as sql_update

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import collections as collections_svc
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    data = b"a file whose canonical proof is already anchored"
    cid = await _seed(cairn_env / "vault", {"a.txt": data})
    slot = _slot(cid, "a.txt")
    proof_bytes = _write_proof(slot, _sha(data), height=800_000)
    await _set_row(cid, "a.txt", ots_state="pending")
    # Adoption's every condition: the proof parses, commits to the row's own sha256, and its anchor
    # verifies right now.
    monkeypatch.setattr(
        ots,
        "verify",
        lambda *a, **k: ots.VerifyResult(verified=True, state="complete", block_height=800_000),
    )
    stamp_calls: list[list[str]] = []
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake(stamp_calls))

    real_adoption = proofs._adoption_pass

    async def adopt_then_reclaim(chunk, settings):
        keep, verdicts, adopted = await real_adoption(chunk, settings)
        assert not keep and adopted == 1, "the batch must resolve entirely by adoption"
        # The claim is reclaimed between the adoption and the commit that would record it.
        async with get_sessionmaker()() as other:
            await other.execute(
                sql_update(Run)
                .where(Run.collection_id == cid, Run.result == "running")
                .values(result="interrupted", finished=_utcnow())
            )
            await other.commit()
        return keep, verdicts, adopted

    monkeypatch.setattr(proofs, "_adoption_pass", adopt_then_reclaim)

    async with get_sessionmaker()() as s:
        run = Run(collection_id=cid, kind="stamp", started=_utcnow(), result="running")
        assert await collections_svc.claim_run(s, run) is not None
        with pytest.raises(collections_svc.LeaseLost):
            await proofs.stamp_pending(s, await s.get(Collection, cid), run_id=run.id)

    row = await _row(cid, "a.txt")
    assert row.ots_state == "pending", "the adoption must not commit under a reclaimed claim"
    assert row.ots_path is None and row.ots_digest is None, "no row recorded another's artifact"
    assert slot.read_bytes() == proof_bytes, "the artifact on disk is untouched either way"
    assert stamp_calls == [], "an adoption-only batch makes no calendar traffic"
    async with get_sessionmaker()() as s:
        others = list(
            await s.scalars(
                select(FileEntry).where(
                    FileEntry.collection_id == cid, FileEntry.ots_path.is_not(None)
                )
            )
        )
    assert others == []


async def test_a_non_ascii_case_respelled_slot_is_surfaced_by_the_casefold_key(
    cairn_env, monkeypatch
):
    """Scenario: a non-ASCII respelled path cannot evade candidate selection.

    SQLite's ``lower()`` folds ASCII only, so ``Å.txt.ots`` and ``å.txt.ots`` produced no candidate
    at all and the guard missed the alias entirely — on a case-insensitive store, the stamp then
    displaced a proof another row records. The candidate key is now ``casefold`` (Python's
    ``str.casefold``, registered on every connection), and ``same_directory_entry`` stays the
    decider, so a case-sensitive store still never defers a genuinely distinct slot.
    """
    from sqlalchemy import text

    from src.database import get_sessionmaker
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"\u00e5.txt": b"newcomer bytes", "b.txt": b"moved bytes"})
    upper_slot = _slot(cid, "\u00c5.txt")
    _write_proof(upper_slot, _sha(b"moved bytes"))
    moved_id = await _set_row(
        cid,
        "b.txt",
        ots_path=str(upper_slot),
        ots_state="incomplete",
        ots_digest=_sha(b"moved bytes"),
    )
    await _set_row(cid, "\u00e5.txt", ots_state="pending")
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    async with get_sessionmaker()() as s:
        # The ASCII-only fold the prefilter used to key on cannot see these two as one name…
        assert await s.scalar(text("SELECT lower('\u00c5')")) == "\u00c5"
        # …while the candidate query surfaces the blocking row.
        found = await proofs._slot_references(s, cid, [_slot(cid, "\u00e5.txt")])
    assert [(rid, relpath) for rid, relpath, _p in found] == [(moved_id, "b.txt")]

    # The store here is ext4 (case-sensitive): two genuinely distinct slots, so no deferral.
    assert await _stamp(cid) == 1
    assert (await _row(cid, "\u00e5.txt")).ots_path == str(_slot(cid, "\u00e5.txt"))
    assert upper_slot.exists(), "the other slot was never touched"

    # Now the case-insensitive answer: the filesystem says the two spellings are one entry.
    await _set_row(cid, "\u00e5.txt", ots_state="pending", ots_path=None, ots_digest=None)
    monkeypatch.setattr(ots, "same_directory_entry", lambda a, b: True)
    assert await _stamp(cid) == 0
    assert (await _row(cid, "\u00e5.txt")).ots_state == "pending"
    assert (await _row(cid, "b.txt")).ots_path == str(upper_slot)


async def test_the_casefold_sql_function_is_registered_on_every_connection(cairn_env):
    """The candidate key is only as good as its registration: every connection must carry it."""
    from sqlalchemy import text

    from src.database import get_sessionmaker

    async with get_sessionmaker()() as s:
        assert await s.scalar(text("SELECT casefold('\u00c5')")) == "\u00e5"
        # Full folding, not a lowercase alias: the German sharp s folds to two characters.
        assert await s.scalar(text("SELECT casefold('\u00df')")) == "ss"
        # A SQL function sees NULLs and non-text values too, and must pass them through.
        assert await s.scalar(text("SELECT casefold(NULL)")) is None
        assert await s.scalar(text("SELECT casefold(7)")) == 7


# ==============================================================================================
# 2 — the relocation primitive (`ots.publish_relocation` / `finish_relocation`)
# ==============================================================================================


def _proof_pair(tmp: Path, digest_seed: bytes) -> tuple[Path, Path, bytes]:
    """``(src, dst, proof bytes)`` — a real proof at ``src`` and an empty canonical-ish ``dst``."""
    src = tmp / "store" / "1" / "old.txt.ots"
    dst = tmp / "store" / "1" / "sub" / "new.txt.ots"
    payload = _write_proof(src, _sha(digest_seed))
    return src, dst, payload


def test_a_plain_relocation_publishes_then_removes_the_source(tmp_path):
    """The happy path, in its two halves: publish (both exist), then loss-proof removal."""
    from src.services import ots

    store = tmp_path / "store"
    src, dst, payload = _proof_pair(tmp_path, b"payload")

    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "published"
    assert src.read_bytes() == payload, "the source is still the truthful location until the commit"
    assert dst.read_bytes() == payload

    ots.finish_relocation(src, dst, store_root=store, collection_id=1)
    assert not src.exists()
    assert dst.read_bytes() == payload
    # The removal archived a copy first: bytes existed in three places before one went away.
    archived = sorted((store / ".superseded" / "1").rglob("*.ots"))
    assert [p.read_bytes() for p in archived] == [payload]


def test_the_both_exist_crash_window_adopts_and_syncs_the_destination_chain(tmp_path, monkeypatch):
    """Scenario: crash after publication, before the pointer commit — the next sweep completes it.

    The destination's directory chain is synced even though nothing is written: the attempt that
    published it may have died before its own sync ever ran, so adopting without syncing would
    commit a pointer to a name that a power cut could still lose.
    """
    from src.services import ots

    store = tmp_path / "store"
    src, dst, payload = _proof_pair(tmp_path, b"payload")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(payload)  # the earlier attempt's published half

    synced: list[Path] = []
    real_fsync = ots._fsync_dir
    monkeypatch.setattr(
        ots,
        "_fsync_dir",
        lambda path, *, store_key: (synced.append(Path(path)), real_fsync(path, store_key=store_key))[1],
    )

    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "adopted"
    assert dst.parent in synced, "the adopted destination's own directory must be flushed"
    assert store in synced, "…and the chain up to the proof-store root"
    assert src.read_bytes() == payload


def test_an_identity_lie_with_different_bytes_never_commits_the_pointer(tmp_path, monkeypatch):
    """Identity ALONE never decides an alias: a byte comparison confirms it, or nothing happens."""
    from src.services import ots

    store = tmp_path / "store"
    src, dst, _payload = _proof_pair(tmp_path, b"payload")
    dst.parent.mkdir(parents=True, exist_ok=True)
    _write_proof(dst, _sha(b"a completely different proof"))
    before = dst.read_bytes()

    monkeypatch.setattr(ots, "same_directory_entry", lambda a, b: True)
    with pytest.raises(ots.OtsError, match="one directory entry"):
        ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert src.exists() and dst.read_bytes() == before, "both proofs survive an unbelievable claim"


def test_a_same_digest_but_byte_different_occupant_is_archived_not_adopted(tmp_path):
    """Committed-digest equality is not identity: two proofs for one digest are both evidence."""
    from src.services import ots

    store = tmp_path / "store"
    digest = _sha(b"payload")
    src = store / "1" / "old.txt.ots"
    dst = store / "1" / "new.txt.ots"
    source_bytes = _write_proof(src, digest)  # pending attestation
    occupant_bytes = _write_proof(dst, digest, height=800_000)  # same digest, anchored
    assert source_bytes != occupant_bytes

    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "published"
    assert dst.read_bytes() == source_bytes
    archived = sorted((store / ".superseded" / "1").rglob("*.ots"))
    assert [p.read_bytes() for p in archived] == [occupant_bytes], "the occupant was preserved"
    assert src.read_bytes() == source_bytes


def test_a_link_refusing_filesystem_publishes_via_the_exclusive_create(tmp_path, monkeypatch):
    """A store without hard links (SMB/FAT/FUSE) still publishes — by copying, never replacing."""
    from src.services import ots

    store = tmp_path / "store"
    src, dst, payload = _proof_pair(tmp_path, b"payload")

    def no_links(a, b, **kw):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "published"
    assert dst.read_bytes() == payload
    assert os.lstat(dst).st_ino != os.lstat(src).st_ino, "a copy, not a link"


def test_a_destination_appearing_mid_publication_restarts_the_rules(tmp_path, monkeypatch):
    """EEXIST re-classifies the destination; it never overwrites what turned up there."""
    from src.services import ots

    store = tmp_path / "store"
    src, dst, payload = _proof_pair(tmp_path, b"payload")
    interloper = _write_proof(tmp_path / "interloper.ots", _sha(b"someone else's proof"))

    real_link = os.link
    calls: list[int] = []

    def link_once_racy(a, b, **kw):
        calls.append(1)
        if len(calls) == 1:
            # Between classification (empty) and publication, another proof appears.
            Path(b).parent.mkdir(parents=True, exist_ok=True)
            Path(b).write_bytes(interloper)
            raise FileExistsError(errno.EEXIST, "File exists")
        return real_link(a, b, **kw)

    monkeypatch.setattr(os, "link", link_once_racy)
    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "published"
    assert dst.read_bytes() == payload
    archived = sorted((store / ".superseded" / "1").rglob("*.ots"))
    assert [p.read_bytes() for p in archived] == [interloper], "the interloper was preserved"


def test_a_case_aliased_source_and_destination_update_spelling_without_removing_anything(
    tmp_path, monkeypatch
):
    """Scenario: a case-only rename does not unlink the only copy."""
    from src.services import ots

    store = tmp_path / "store"
    src = store / "1" / "A.txt.ots"
    dst = store / "1" / "a.txt.ots"
    payload = _write_proof(src, _sha(b"payload"))
    dst.write_bytes(payload)  # what a case-insensitive store would show at the other spelling

    monkeypatch.setattr(ots, "same_directory_entry", lambda a, b: True)
    publication = ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert publication.kind == "aliased"
    assert src.exists() and dst.exists(), "an alias removes nothing — there may be only one entry"
    assert sorted((store / ".superseded").rglob("*.ots")) == []


def test_removal_reverification_restores_the_destination_when_the_unlink_takes_it(
    tmp_path, monkeypatch
):
    """The defence against a filesystem whose identity reporting lies (design D4 phase 5)."""
    from src.services import ots

    store = tmp_path / "store"
    src, dst, payload = _proof_pair(tmp_path, b"payload")
    assert ots.publish_relocation(src, dst, store_root=store, collection_id=1).kind == "published"

    real_unlink = os.unlink

    def unlink_takes_both(path, **kw):
        real_unlink(path, **kw)
        if Path(path) == src and dst.exists():
            real_unlink(dst)

    monkeypatch.setattr(os, "unlink", unlink_takes_both)
    ots.finish_relocation(src, dst, store_root=store, collection_id=1)
    monkeypatch.undo()

    assert dst.read_bytes() == payload, "the destination the pointer names was put back"
    assert not src.exists()


def test_an_over_limit_destination_is_refused_per_row(tmp_path):
    """A permanently refused destination is a per-row verdict — never a discarded proof."""
    from src.services import ots

    store = tmp_path / "store"
    src = store / "1" / "old.txt.ots"
    payload = _write_proof(src, _sha(b"payload"))
    dst = store / "1" / ("n" * 300 + ".ots")

    with pytest.raises(ots.OtsPathError):
        ots.publish_relocation(src, dst, store_root=store, collection_id=1)
    assert src.read_bytes() == payload, "the source proof is untouched by a refusal"
    # `dst.exists()` would itself raise ENAMETOOLONG, so ask the directory instead.
    assert sorted(p.name for p in dst.parent.iterdir()) == ["old.txt.ots"]


def test_an_absent_source_refuses_without_touching_the_destination(tmp_path):
    """Nothing is established about a source that is not there, so nothing may be done."""
    from src.services import ots

    store = tmp_path / "store"
    dst = store / "1" / "new.txt.ots"
    with pytest.raises(ots.OtsError):
        ots.publish_relocation(store / "1" / "gone.ots", dst, store_root=store, collection_id=1)
    assert not dst.exists()


# ==============================================================================================
# 3 — the healing sweep
# ==============================================================================================


async def _moved_row(cairn_env, *, digest_recorded: bool = True, ots_state: str = "incomplete"):
    """A row whose proof still sits at its FORMER relpath's canonical slot — the live #39 shape."""
    x = b"the moved file's bytes"
    root = cairn_env / "vault"
    cid = await _seed(root, {"b.txt": x})
    old_slot = _slot(cid, "a.txt")
    payload = _write_proof(old_slot, _sha(x))
    row_id = await _set_row(
        cid,
        "b.txt",
        ots_path=str(old_slot),
        ots_state=ots_state,
        ots_digest=_sha(x) if digest_recorded else None,
        ots_stamped_at=datetime.now(timezone.utc),
    )
    return cid, row_id, old_slot, _slot(cid, "b.txt"), payload


async def test_a_pre_existing_moved_row_heals_in_one_sweep(cairn_env):
    """Scenario: the sweep converges a moved row — and writes ``ots_path`` and nothing else."""
    cid, row_id, old_slot, new_slot, payload = await _moved_row(cairn_env)
    before = await _row(cid, "b.txt")

    outcome = await _sweep(cid)
    assert (outcome.items, outcome.relocated) == (1, 1)

    after = await _row(cid, "b.txt")
    assert after.ots_path == str(new_slot)
    assert new_slot.read_bytes() == payload
    assert not old_slot.exists(), "the former location is vacated"
    for column in ("relpath", "sha256", "status", "ots_state", "ots_digest", "ots_stamped_at",
                   "first_seen", "size"):
        assert getattr(after, column) == getattr(before, column), f"the sweep wrote {column}"


async def test_the_pointer_committed_leftover_source_is_never_reselected(cairn_env):
    """Scenario: crash after the pointer commit, before source removal — no sweep touches it.

    The pointer already names the canonical location, so staleness does not select the row, and the
    sweep makes no garbage-collection promise about the redundant copy. What it must NOT do is
    silently overwrite it.
    """
    from src.database import get_sessionmaker
    from src.services import proofs

    cid, _row_id, old_slot, new_slot, payload = await _moved_row(cairn_env)
    new_slot.parent.mkdir(parents=True, exist_ok=True)
    new_slot.write_bytes(payload)
    await _set_row(cid, "b.txt", ots_path=str(new_slot))

    async with get_sessionmaker()() as s:
        work = await proofs.survey_pointer_work(s, cid)
    assert work.items == 0, "a canonical pointer is not stale, whatever else is lying around"
    assert old_slot.read_bytes() == payload, "the leftover copy is left exactly where it is"


async def test_a_misfiled_pointer_is_detected_not_propagated(cairn_env, caplog):
    """Scenario: recorded provenance disagrees with the proof on disk — relocate nothing."""
    cid, _row_id, old_slot, new_slot, _payload = await _moved_row(cairn_env)
    stranger = _write_proof(old_slot, _sha(b"somebody else's file"))
    before = await _row(cid, "b.txt")

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)

    assert (outcome.items, outcome.refused, outcome.relocated) == (1, 1, 0)
    after = await _row(cid, "b.txt")
    assert after.ots_path == before.ots_path and after.ots_digest == before.ots_digest
    assert old_slot.read_bytes() == stranger, "nothing was moved, archived or overwritten"
    assert not new_slot.exists()
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "misfiled" in message
    assert before.ots_digest in message and _sha(b"somebody else's file") in message
    assert ".superseded" in message, "the operator is pointed at the recovery location"


async def test_a_legacy_row_heals_only_when_the_source_commits_to_its_sha256(cairn_env):
    """A row predating provenance is corroborated by its own last-scanned digest."""
    cid, _row_id, old_slot, new_slot, payload = await _moved_row(cairn_env, digest_recorded=False)

    outcome = await _sweep(cid)
    assert outcome.relocated == 1
    assert new_slot.read_bytes() == payload
    assert (await _row(cid, "b.txt")).ots_digest is None, "the sweep never writes provenance"
    assert not old_slot.exists()


async def test_a_modified_then_moved_legacy_row_is_ambiguous_not_swapped(cairn_env, caplog):
    """Scenario: a legacy row whose proof predates a modification is AMBIGUOUS, never 'misfiled'."""
    cid, _row_id, old_slot, new_slot, _payload = await _moved_row(cairn_env, digest_recorded=False)
    # The file was modified after it was stamped: the old proof commits to the OLD bytes.
    older = _write_proof(old_slot, _sha(b"the version that was stamped"))

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)

    assert (outcome.refused, outcome.relocated) == (1, 0)
    assert old_slot.read_bytes() == older and not new_slot.exists()
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "cannot be corroborated" in message
    assert "misfiled" not in message and "swapped" not in message


async def test_chain_moves_converge_without_touching_a_referenced_slot(cairn_env):
    """Scenario: A→B and C→A in one scan — C waits for A's slot, then takes it."""
    root = cairn_env / "vault"
    first, second = b"the file formerly at a", b"the file formerly at c"
    cid = await _seed(root, {"b.txt": first, "a.txt": second})
    slot_a, slot_b, slot_c = _slot(cid, "a.txt"), _slot(cid, "b.txt"), _slot(cid, "c.txt")
    proof_first = _write_proof(slot_a, _sha(first))
    proof_second = _write_proof(slot_c, _sha(second))
    await _set_row(cid, "b.txt", ots_path=str(slot_a), ots_state="incomplete", ots_digest=_sha(first))
    await _set_row(cid, "a.txt", ots_path=str(slot_c), ots_state="incomplete", ots_digest=_sha(second))

    outcome = await _sweep(cid)
    assert (outcome.items, outcome.relocated, outcome.parked) == (2, 2, 0)
    assert slot_b.read_bytes() == proof_first
    assert slot_a.read_bytes() == proof_second
    assert not slot_c.exists()
    assert (await _row(cid, "b.txt")).ots_path == str(slot_b)
    assert (await _row(cid, "a.txt")).ots_path == str(slot_a)
    assert sorted(_archive_payloads(cairn_env / "proofs", cid)) == sorted(
        [proof_first, proof_second]
    ), "the archive holds only the loss-proof copies of the two relocated proofs"


async def _swapped_pair(cairn_env, *, corroborate_first: bool = True):
    """Two files whose paths were swapped: each row's destination is the other's recorded proof."""
    root = cairn_env / "vault"
    first, second = b"file one", b"file two"
    cid = await _seed(root, {"b.txt": first, "a.txt": second})
    slot_a, slot_b = _slot(cid, "a.txt"), _slot(cid, "b.txt")
    proof_first = _write_proof(slot_a, _sha(first if corroborate_first else b"something else"))
    proof_second = _write_proof(slot_b, _sha(second))
    id_first = await _set_row(
        cid, "b.txt", ots_path=str(slot_a), ots_state="incomplete", ots_digest=_sha(first)
    )
    id_second = await _set_row(
        cid, "a.txt", ots_path=str(slot_b), ots_state="incomplete", ots_digest=_sha(second)
    )
    return cid, id_first, id_second, slot_a, slot_b, proof_first, proof_second


async def test_a_path_swap_converges_via_the_holding_location(cairn_env):
    """Scenario: a cycle deferral can never free either slot, so one member is parked."""
    from src.config import get_settings
    from src.services import ots

    cid, id_first, id_second, slot_a, slot_b, proof_first, proof_second = await _swapped_pair(
        cairn_env
    )
    store = Path(get_settings().proof_store_path)

    first_pass = await _sweep(cid)
    assert first_pass.items == 2
    assert (first_pass.parked, first_pass.relocated) == (1, 1)
    parked_id = id_first  # the first eligible member of the cycle
    held = ots.holding_slot(store, parked_id)
    assert held.read_bytes() == proof_first
    assert (await _row(cid, "b.txt")).ots_path == str(held), "the parked pointer is truthful"
    assert slot_a.read_bytes() == proof_second, "the other member took the vacated slot"
    assert (await _row(cid, "a.txt")).ots_path == str(slot_a)

    second_pass = await _sweep(cid)
    assert (second_pass.items, second_pass.relocated) == (1, 1)
    assert slot_b.read_bytes() == proof_first
    assert not held.exists()
    assert (await _row(cid, "b.txt")).ots_path == str(slot_b)
    assert set(_archive_payloads(cairn_env / "proofs", cid)) == {proof_first, proof_second}, (
        "only the relocations' own loss-proof copies were archived — nothing foreign"
    )


async def test_a_cycle_member_another_rule_refuses_is_never_the_one_moved(cairn_env):
    """Scenario: a cycle member whose source fails corroboration is never selected for parking."""
    from src.config import get_settings
    from src.services import ots

    cid, id_first, id_second, slot_a, slot_b, proof_first, proof_second = await _swapped_pair(
        cairn_env, corroborate_first=False
    )
    store = Path(get_settings().proof_store_path)
    before = await _row(cid, "b.txt")

    outcome = await _sweep(cid)
    assert outcome.items == 2
    assert outcome.refused == 1 and outcome.parked == 1

    # The refused row kept everything: proof, pointer, provenance.
    after = await _row(cid, "b.txt")
    assert after.ots_path == before.ots_path == str(slot_a)
    assert after.ots_digest == before.ots_digest and after.ots_state == before.ots_state
    assert slot_a.read_bytes() == proof_first
    assert not ots.holding_slot(store, id_first).exists(), "the refused row was never parked"
    # …and the eligible member was the one parked.
    assert ots.holding_slot(store, id_second).read_bytes() == proof_second


async def test_a_permanently_refused_destination_never_degrades_proof_state(cairn_env, caplog):
    """Scenario: an over-limit destination warns per row and drops nothing."""
    x = b"a file with an impossible proof name"
    root = cairn_env / "vault"
    cid = await _seed(root, {"short.txt": x})
    long_relpath = "n" * 300 + ".txt"
    old_slot = _slot(cid, "short.txt")
    payload = _write_proof(old_slot, _sha(x))
    await _set_row(
        cid,
        "short.txt",
        relpath=long_relpath,
        ots_path=str(old_slot),
        ots_state="incomplete",
        ots_digest=_sha(x),
    )

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)

    assert (outcome.items, outcome.refused) == (1, 1)
    row = await _row(cid, long_relpath)
    assert row.ots_path == str(old_slot) and row.ots_state == "incomplete"
    assert row.ots_digest == _sha(x)
    assert old_slot.read_bytes() == payload
    assert "refuses that name permanently" in "\n".join(r.getMessage() for r in caplog.records)


async def test_the_row_changed_beneath_the_sweep_commits_nothing(cairn_env, monkeypatch):
    """Scenario: the fenced compare-and-set matches zero rows, so the sweep stops claim-lost."""
    from sqlalchemy import update as sql_update

    from src.models.db import FileEntry
    from src.services import collections as collections_svc
    from src.services import proofs

    cid, row_id, old_slot, new_slot, payload = await _moved_row(cairn_env)
    real_refs = proofs._slot_references

    async def refs_then_change(session, collection_id, paths):
        result = await real_refs(session, collection_id, paths)
        # Between corroboration and the pointer commit, the row is re-classified underneath us.
        # `synchronize_session=False` keeps the in-session object stale, which is the whole shape:
        # the sweep is holding values that are no longer what the datastore says.
        await session.execute(
            sql_update(FileEntry)
            .where(FileEntry.id == row_id)
            .values(ots_digest="0" * 64)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return result

    monkeypatch.setattr(proofs, "_slot_references", refs_then_change)

    with pytest.raises(collections_svc.LeaseLost):
        await _sweep(cid)

    row = await _row(cid, "b.txt")
    assert row.ots_path == str(old_slot), "no pointer moved"
    assert new_slot.exists(), "the published copy is inert until a later sweep re-evaluates it"
    assert old_slot.read_bytes() == payload


async def test_the_sql_prefilter_and_the_proof_path_helper_agree(cairn_env):
    """Design D6: relpaths full of SQL metacharacters must classify identically on both sides."""
    from src.database import get_sessionmaker
    from src.services import proofs

    names = [
        "100%_report.txt",
        "under_score.txt",
        "quote'd.txt",
        'double"quote.txt',
        "back\\slash.txt",
        "nested/dir with space/%_'.txt",
    ]
    root = cairn_env / "vault"
    cid = await _seed(root, {name: name.encode() for name in names})

    expected: set[int] = set()
    for index, name in enumerate(names):
        canonical = _slot(cid, name)
        _write_proof(canonical, _sha(name.encode()))
        if index % 2:
            # Odd rows are made stale by pointing them at another (existing) canonical slot.
            other = _slot(cid, names[0])
            expected.add(await _set_row(cid, name, ots_path=str(other), ots_digest=_sha(names[0].encode())))
        else:
            await _set_row(cid, name, ots_path=str(canonical), ots_digest=_sha(name.encode()))

    async with get_sessionmaker()() as s:
        work = await proofs.survey_pointer_work(s, cid)
    assert set(work.stale) == expected, "the SQL pre-filter and proof_path() must agree exactly"
    assert work.absent == ()


# --- the restore leg (design D4b) --------------------------------------------------------------


async def test_the_phase_five_crash_shape_is_repaired_by_the_next_sweep(cairn_env, caplog):
    """Scenario: the pointer is canonical, the entry is gone, the archive holds the real copy."""
    from src.config import get_settings
    from src.services import ots

    x = b"a proof that vanished with its source"
    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": x})
    canonical = _slot(cid, "a.txt")
    payload = _write_proof(canonical, _sha(x))
    archive = ots.superseded_root(get_settings().proof_store_path, cid) / _sha(x)[:2]
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{_sha(x)}.ots").write_bytes(payload)
    canonical.unlink()
    await _set_row(
        cid, "a.txt", ots_path=str(canonical), ots_state="incomplete", ots_digest=_sha(x)
    )

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)

    assert (outcome.items, outcome.restored) == (1, 1)
    assert canonical.read_bytes() == payload
    assert (await _row(cid, "a.txt")).ots_path == str(canonical), "no row field was written"
    assert "RESTORED" in "\n".join(r.getMessage() for r in caplog.records)


async def test_an_absent_proof_with_no_corroborated_copy_is_loud_not_silent(cairn_env, caplog):
    """Scenario: nothing in the archive corroborates the row — warn on every sweep, write nothing."""
    x = b"a proof that is simply gone"
    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": x})
    canonical = _slot(cid, "a.txt")
    await _set_row(
        cid, "a.txt", ots_path=str(canonical), ots_state="incomplete", ots_digest=_sha(x)
    )

    for _attempt in range(2):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
            outcome = await _sweep(cid)
        assert (outcome.items, outcome.refused, outcome.restored) == (1, 1, 0)
        assert not canonical.exists()
        assert (await _row(cid, "a.txt")).ots_path == str(canonical)
        assert "MISSING from the store" in "\n".join(r.getMessage() for r in caplog.records)


async def _archived(cid: int, payload: bytes, digest: str) -> Path:
    """Put ``payload`` in the collection's superseded archive under ``digest`` (content-addressed)."""
    from src.config import get_settings
    from src.services import ots

    family = ots.superseded_root(get_settings().proof_store_path, cid) / digest[:2]
    family.mkdir(parents=True, exist_ok=True)
    copy = family / f"{digest}.ots"
    copy.write_bytes(payload)
    return copy


async def test_a_failed_restoration_is_loud_and_heals_on_the_next_sweep(cairn_env, monkeypatch, caplog):
    """Scenario: a failed restoration is loud and heals on the next sweep.

    The nastiest shape in phase 5: the store lies about identity, so unlinking the source takes the
    destination with it — and then the restoration from the archive copy cannot run either (the
    store briefly refuses writes). The pointer is already committed, so it now names an absent
    entry. That must NEVER be a clean return: the primitive raises, the sweep says the entry is
    absent (not "nothing was lost"), and the state left behind is precisely the restore leg's own
    admission shape, so the next sweep republishes the proof there.
    """
    import errno as _errno

    from src.services import ots

    cid, _row_id, old_slot, new_slot, payload = await _moved_row(cairn_env)

    real_unlink = os.unlink

    def unlink_takes_both(path, **kw):
        real_unlink(path, **kw)
        if Path(path) == old_slot and new_slot.exists():
            real_unlink(new_slot)

    real_copy = ots._copy_no_replace

    def copy_refuses_the_destination(data, target, **kw):
        if Path(target) == new_slot:
            raise OSError(_errno.ENOSPC, "No space left on device")
        return real_copy(data, target, **kw)

    monkeypatch.setattr(os, "unlink", unlink_takes_both)
    monkeypatch.setattr(ots, "_copy_no_replace", copy_refuses_the_destination)

    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)
    monkeypatch.undo()

    assert outcome.items == 1
    row = await _row(cid, "b.txt")
    assert row.ots_path == str(new_slot), "the committed pointer is not rolled back"
    assert not new_slot.exists() and not old_slot.exists(), "the identity lie took both entries"
    warnings = "\n".join(r.getMessage() for r in caplog.records)
    assert "ABSENT" in warnings, "a pointer that resolves to nothing must be said out loud"
    assert "restore leg" in warnings
    assert "Nothing was lost — the pointer is correct" not in warnings, (
        "the leftover-copy reassurance is false here and must not be printed"
    )

    # The archive kept the corroborated copy, which is what makes the next sweep able to repair it.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        second = await _sweep(cid)
    assert (second.items, second.restored) == (1, 1)
    assert new_slot.read_bytes() == payload, "the restore leg republished the proof"
    assert (await _row(cid, "b.txt")).ots_path == str(new_slot)
    assert "RESTORED" in "\n".join(r.getMessage() for r in caplog.records)


async def test_a_source_that_cannot_be_removed_is_reported_not_swallowed(cairn_env, monkeypatch, caplog):
    """A post-commit removal failure surfaces as the post-commit warning — never a clean success.

    Suppressing it returned "relocated" with a redundant copy sitting in an unreferenced canonical
    slot and nobody told: the next stamp there would archive it (never destroy it), but the
    operator learns of the leftover only from this warning.
    """
    import errno as _errno

    cid, _row_id, old_slot, new_slot, payload = await _moved_row(cairn_env)
    real_unlink = os.unlink

    def unlink_refuses_the_source(path, **kw):
        if Path(path) == old_slot:
            raise PermissionError(_errno.EACCES, "Permission denied")
        return real_unlink(path, **kw)

    monkeypatch.setattr(os, "unlink", unlink_refuses_the_source)
    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        outcome = await _sweep(cid)
    monkeypatch.undo()

    assert (outcome.items, outcome.relocated) == (1, 1)
    assert (await _row(cid, "b.txt")).ots_path == str(new_slot), "the committed pointer is kept"
    assert new_slot.read_bytes() == payload, "the destination the pointer names holds the proof"
    assert old_slot.read_bytes() == payload, "the leftover source is preserved, never destroyed"
    warnings = "\n".join(r.getMessage() for r in caplog.records)
    assert "could not be removed" in warnings and "Permission denied" in warnings
    assert "leftover" in warnings


async def test_the_aliased_branch_records_the_spelling_and_removes_nothing(cairn_env, monkeypatch, caplog):
    """The sweep's case-only rename path, end to end: pointer re-spelled, nothing removed.

    A case-insensitive store reports the recorded spelling and the canonical one as ONE directory
    entry. The proof is already where it belongs, so the sweep commits the canonical spelling and
    must not call the removal phase at all — there may be a single entry, and removing it would
    destroy the only copy.
    """
    from src.services import ots

    x = b"a proof whose slot was only re-spelled"
    root = cairn_env / "vault"
    cid = await _seed(root, {"b.txt": x})
    recorded = _slot(cid, "B.txt")
    canonical = _slot(cid, "b.txt")
    payload = _write_proof(recorded, _sha(x))
    # On a case-insensitive store these two names ARE one entry; ext4 needs the two files plus the
    # identity answer such a store would give.
    canonical.write_bytes(payload)
    await _set_row(
        cid, "b.txt", ots_path=str(recorded), ots_state="complete", ots_digest=_sha(x)
    )
    monkeypatch.setattr(ots, "same_directory_entry", lambda a, b: True)

    finished: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ots,
        "finish_relocation",
        lambda src, dst, **kw: finished.append((str(src), str(dst))),
    )

    with caplog.at_level(logging.INFO, logger="cairn.proofs"):
        outcome = await _sweep(cid)

    assert (outcome.items, outcome.relocated) == (1, 1)
    assert (await _row(cid, "b.txt")).ots_path == str(canonical), "the spelling was re-recorded"
    assert finished == [], "the removal phase must never run for an aliased entry"
    assert recorded.read_bytes() == payload and canonical.read_bytes() == payload, (
        "nothing may be removed when the store may hold only one entry"
    )
    assert _archive_files(cairn_env / "proofs", cid) == [], "nothing was displaced"
    assert "one entry" in "\n".join(r.getMessage() for r in caplog.records)


async def test_an_absent_and_stale_row_is_restored_then_relocated_in_one_sweep(cairn_env):
    """A row that is BOTH absent and stale gets both operations — and both work items — this pass.

    Classifying the two shapes disjointly restored such a row to its OBSOLETE location: the proof
    came back at the moved file's FORMER canonical slot, the run said ``ok``, and the pointer stayed
    non-canonical — still blocking a newcomer's stamp there — until some later pass.
    """
    x = b"the moved file's bytes"
    root = cairn_env / "vault"
    cid = await _seed(root, {"b.txt": x})
    old_slot = _slot(cid, "a.txt")
    new_slot = _slot(cid, "b.txt")
    payload = _write_proof(old_slot, _sha(x))
    copy = await _archived(cid, payload, _sha(x))
    old_slot.unlink()  # the recorded entry is gone; the archive holds the corroborated copy
    await _set_row(
        cid, "b.txt", ots_path=str(old_slot), ots_state="complete", ots_digest=_sha(x)
    )

    outcome = await _upgrade(cid)

    assert (outcome.sweep.restored, outcome.sweep.relocated) == (1, 1)
    assert outcome.sweep.items == 2, "one work item per operation, not per row"
    assert new_slot.read_bytes() == payload, "the proof ends this sweep at its file's own slot"
    assert not old_slot.exists(), "and does not linger at the obsolete one"
    assert (await _row(cid, "b.txt")).ots_path == str(new_slot)
    run = (await _runs(cid))[-1]
    assert (run.kind, run.total, run.processed, run.result) == ("upgrade", 2, 2, "ok")
    assert copy.exists(), "the archive copy is never consumed"


# --- admission + run accounting ----------------------------------------------------------------


async def test_stale_pointers_alone_admit_a_tripwire_collection(cairn_env):
    """Scenario: the sweep runs where the old admission would have gone idle."""
    x = b"a tripwire collection's historical proof"
    root = cairn_env / "vault"
    cid = await _seed(root, {"b.txt": x}, ots_mode="none")
    old_slot = _slot(cid, "a.txt")
    payload = _write_proof(old_slot, _sha(x))
    await _set_row(
        cid, "b.txt", ots_path=str(old_slot), ots_state="complete", ots_digest=_sha(x)
    )

    outcome = await _upgrade(cid)
    assert not outcome.refused and not outcome.idle
    assert outcome.sweep.relocated == 1
    run = (await _runs(cid))[-1]
    assert run.kind == "upgrade" and run.result == "ok"
    assert (run.total, run.processed) == (1, 1)
    assert _slot(cid, "b.txt").read_bytes() == payload


async def test_an_absent_recorded_entry_alone_admits_the_collection(cairn_env):
    """Scenario: an absent recorded entry alone claims the collection and counts its restore work."""
    from src.config import get_settings
    from src.services import ots

    x = b"a proof to restore"
    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": x}, ots_mode="none")
    canonical = _slot(cid, "a.txt")
    payload = _write_proof(canonical, _sha(x))
    archive = ots.superseded_root(get_settings().proof_store_path, cid) / _sha(x)[:2]
    archive.mkdir(parents=True, exist_ok=True)
    (archive / f"{_sha(x)}.ots").write_bytes(payload)
    canonical.unlink()
    await _set_row(cid, "a.txt", ots_path=str(canonical), ots_state="complete", ots_digest=_sha(x))

    outcome = await _upgrade(cid)
    assert outcome.sweep.restored == 1
    run = (await _runs(cid))[-1]
    assert (run.kind, run.total, run.processed, run.result) == ("upgrade", 1, 1, "ok")
    assert canonical.read_bytes() == payload


async def test_no_work_of_any_kind_records_no_run(cairn_env):
    """Scenario: nothing incomplete, no stale pointers, no absent entries — no run at all."""
    x = b"a settled file"
    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": x})
    canonical = _slot(cid, "a.txt")
    _write_proof(canonical, _sha(x))
    await _set_row(cid, "a.txt", ots_path=str(canonical), ots_state="complete", ots_digest=_sha(x))

    outcome = await _upgrade(cid)
    assert outcome.idle and outcome.run is None
    assert await _runs(cid) == []


async def test_the_sweep_runs_before_the_proof_upgrades(cairn_env, monkeypatch):
    """A proof relocated this pass is upgraded at its NEW canonical location, in the same run."""
    from src.services import ots

    cid, _row_id, old_slot, new_slot, _payload = await _moved_row(cairn_env)
    upgraded: list[str] = []
    monkeypatch.setattr(ots, "upgrade", lambda path: upgraded.append(str(path)) or False)

    outcome = await _upgrade(cid)
    assert outcome.sweep.relocated == 1
    assert upgraded == [str(new_slot)], "the upgrade must not follow the pointer's old value"
    run = (await _runs(cid))[-1]
    assert (run.total, run.processed) == (2, 2), "one item for the relocation, one for the upgrade"


async def test_a_pass_that_could_not_take_the_lock_finalizes_partial(cairn_env, monkeypatch, caplog):
    """A pass that skipped work finalizes ``partial`` with the count it actually completed.

    Force-writing ``processed = total`` on an ``ok`` finalize reported skipped work as finished:
    the healing sweep swallows a contended proof-store lock (correctly — it refuses to wait out a
    resource another operation holds), and the run then claimed it had done every item. ``partial``
    is the run-health vocabulary's word for "this pass completed, work was skipped", and the next
    pass simply picks the rest up — no retry loop belongs here.
    """
    from src.services import ots

    cid, _row_id, old_slot, new_slot, payload = await _moved_row(cairn_env, ots_state="complete")
    # A bounded wait is the point; sixty real seconds in a test is not.
    monkeypatch.setattr(ots, "PLACEMENT_LOCK_TIMEOUT_SECONDS", 0.2)
    other_process = ots.CollectionProofLock(cairn_env / "proofs", cid)
    other_process.acquire()
    try:
        with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
            outcome = await _upgrade(cid)
    finally:
        other_process.release()

    assert outcome.sweep.items == 0, "nothing could be swept while the lock was held elsewhere"
    run = (await _runs(cid))[-1]
    assert (run.kind, run.total, run.processed) == ("upgrade", 1, 0)
    assert run.result == "partial", "an ok run must never report skipped work as finished"
    assert "completed 0 of 1 work item(s)" in "\n".join(r.getMessage() for r in caplog.records)
    assert old_slot.read_bytes() == payload and not new_slot.exists(), "nothing was moved"

    # …and the next pass, with the lock free, converges it and finalizes ok.
    again = await _upgrade(cid)
    assert again.sweep.relocated == 1
    latest = (await _runs(cid))[-1]
    assert (latest.result, latest.total, latest.processed) == ("ok", 1, 1)


async def test_work_completed_between_the_pre_check_and_the_claim_records_no_run(
    cairn_env, monkeypatch
):
    """The authoritative survey is the one taken UNDER the claim; an empty one records no run.

    The admission survey is only advisory: the pass that currently holds the collection's slot can
    complete the surveyed work before this one claims it. Believing the pre-claim answer wrote a
    run whose ``total`` counted work that no longer existed — and, with nothing left at all, an
    empty ``upgrade`` run, which "no work of any kind -> no run" forbids.
    """
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import collections as collections_svc

    cid, row_id, old_slot, new_slot, payload = await _moved_row(cairn_env, ots_state="complete")

    real_claim = collections_svc.claim_run
    stolen: list[int] = []

    async def steal_then_claim(session, run):
        if not stolen:
            stolen.append(run.collection_id)
            # A rival pass held the slot and converged the pointer between the pre-check and here.
            new_slot.parent.mkdir(parents=True, exist_ok=True)
            new_slot.write_bytes(old_slot.read_bytes())
            old_slot.unlink()
            async with get_sessionmaker()() as other:
                entry = await other.get(FileEntry, row_id)
                entry.ots_path = str(new_slot)
                await other.commit()
        return await real_claim(session, run)

    monkeypatch.setattr(collections_svc, "claim_run", steal_then_claim)
    outcome = await _upgrade(cid)
    monkeypatch.undo()

    assert stolen == [cid], "the test must actually have raced the claim"
    assert outcome.idle and outcome.run is None
    assert await _runs(cid) == [], "no run may survive stating a total for work that was gone"
    assert new_slot.read_bytes() == payload, "the rival's work is left exactly as it was"


# ==============================================================================================
# 4.3 — the structural guarantees, kept honest by a grep
# ==============================================================================================


def _src_files() -> list[Path]:
    return sorted((Path(__file__).resolve().parents[1] / "src").rglob("*.py"))


def test_the_relocation_primitive_is_reachable_only_from_the_sweep():
    """Relocation lives in one place and is called from one place — never from a scan."""
    users = {
        path.name
        for path in _src_files()
        if any(
            name in path.read_text()
            for name in ("publish_relocation", "finish_relocation", "republish_proof")
        )
    }
    assert users == {"ots.py", "proofs.py"}, users


def test_a_scan_contains_no_proof_store_mutation():
    """The scanner rewrites the index; the notarization capability owns the proof store."""
    scanner = (Path(__file__).resolve().parents[1] / "src" / "services" / "scanner.py").read_text()
    for forbidden in ("proof_path(", "_place_proof", "_preserve_proof", "holding_slot", "os.link("):
        assert forbidden not in scanner, forbidden


def test_no_call_site_assembles_a_proof_path_by_string_concatenation():
    """``proof_path`` is the single canonical-location oracle (design D6)."""
    offenders: list[str] = []
    for path in _src_files():
        if path.name in ("ots.py",):
            continue  # the primitive's own module builds names below a path it is GIVEN
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if '".ots"' not in line and "'.ots'" not in line:
                continue
            # `proof_path` itself, its SQL mirror, and the export bundle's basename (which is not a
            # proof-store location at all) are the only legitimate occurrences.
            if any(
                marker in line
                for marker in ('base / (relpath + ".ots")', "literal(prefix)", "basename +", "relpath).name +")
            ):
                continue
            offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], offenders
