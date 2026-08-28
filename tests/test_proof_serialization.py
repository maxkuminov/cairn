"""Proof mutation is single-writer per collection, by DB claim — and a refusal reads as one (D10).

Placement is check-then-act: inspect the canonical path, decide, preserve, place, record. Under two
concurrent writers that is a lost-update machine — both find the path free, both `os.replace`, and
the loser's proof is gone. Cairn already has the right primitive and it is not a lock: the
DB-enforced one-run-per-collection claim (`claim_run` + the partial unique index), which serializes
across processes and hosts sharing the datastore, as no in-process lock does.

The second half of the story is the CLI's honesty about a refusal. A blocked `cairn scan` used to
print an ordinary all-zeroes result line, which reads as a clean integrity pass over a collection
the run never looked at — so a cron job could record success for work it did not do. The rule
asserted below: name the skip, never wait, and exit non-zero when EVERY requested collection was
refused (one busy collection among several is a success that names the skip).

Run from the repo root: ``PYTHONPATH=. pytest tests/test_proof_serialization.py``
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import seed_collection

# --- harness --------------------------------------------------------------------------------


async def _seed_collection_with_pending_file(root: Path, data: bytes = b"payload") -> int:
    """A collection with one on-disk file whose row is queued for stamping."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_bytes(data)
    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(
            FileEntry(
                collection_id=cid,
                relpath="doc.txt",
                size=len(data),
                status="new",
                ots_state="pending",
                first_seen=now,
                last_checked=now,
            )
        )
        await s.commit()
    return cid


async def _hold_the_slot(cid: int) -> None:
    """Claim ``cid``'s single in-progress slot, exactly as another process's operation would.

    This is not a test double: it is the same committed ``running`` row that the scheduler, the panel
    routes and every CLI entry point contend for, guarded by the partial unique index. Holding it is
    what "another process is working on this collection" IS.
    """
    from src.database import get_sessionmaker
    from src.models.db import Run
    from src.services.collections import claim_run
    from src.services.scanner import _utcnow

    async with get_sessionmaker()() as s:
        held = await claim_run(
            s, Run(collection_id=cid, kind="scan", started=_utcnow(), result="running")
        )
        assert held is not None, "the slot could not be claimed for the test"


async def _seed_incomplete_proof(cid: int, store: Path) -> None:
    """Give ``cid`` one `incomplete` proof so `cairn upgrade` has work to do for it."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    proof = store / str(cid) / "doc.txt.ots"
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_bytes(b"an unparseable but present proof")
    async with get_sessionmaker()() as s:
        fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        fe.ots_state = "incomplete"
        fe.ots_path = str(proof)
        await s.commit()


def _dispose_engine() -> None:
    """Close the abandoned aiosqlite connections a finished ``asyncio.run`` leaves behind.

    Each ``cli.main`` call (and each seeding ``asyncio.run``) builds an engine on a loop that is
    then closed, leaving aiosqlite worker threads alive on a dead loop. Enough of them and the
    interpreter dies writing to stderr at shutdown, which fails the whole pytest run for reasons
    that have nothing to do with what is being tested. Disposing from a live loop closes them.
    """
    from src import database

    engine = database._engine
    database.reset_engine()
    if engine is not None:
        asyncio.run(engine.dispose())


def _cli(argv: list[str], capsys) -> tuple[int, str]:
    import src.cli as cli

    _dispose_engine()
    rc = cli.main(argv)
    captured = capsys.readouterr()
    _dispose_engine()
    return rc, captured.out + captured.err


@pytest.fixture
def no_calendar(monkeypatch):
    """Never touch a real calendar or a real `ots` binary from these tests."""
    from src.services import ots

    monkeypatch.setattr(ots, "_run_ots", lambda args, timeout=None: (0, "", ""))
    monkeypatch.setattr(ots, "upgrade", lambda path: False)
    return monkeypatch


# ==============================================================================================
# 2.36 — a second writer is refused, places nothing, and does not block
# ==============================================================================================


def test_cairn_stamp_refuses_a_collection_whose_slot_is_held(cairn_env, capsys, no_calendar):
    """The refusal names the collection, stamps nothing, returns immediately, and exits non-zero.

    Waiting would turn a cron `cairn stamp` into an unbounded stall behind a multi-hour deep scan,
    and the work is idempotent — the next invocation picks it up.
    """
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import ots

    root = cairn_env / "vault"

    async def setup():
        cid = await _seed_collection_with_pending_file(root)
        await _hold_the_slot(cid)

    asyncio.run(setup())
    _dispose_engine()

    def boom(*a, **k):
        raise AssertionError("a refused stamp must not place or adopt a single proof")

    no_calendar.setattr(ots, "stamp_batch_via_symlink", boom)
    no_calendar.setattr(ots, "stamp_via_symlink", boom)

    rc, out = _cli(["stamp", "--collection", "vault"], capsys)

    assert rc == 1
    assert "SKIPPED" in out and "vault" in out
    assert "already in progress" in out

    async def check():
        async with get_sessionmaker()() as s:
            fe = await s.scalar(select(FileEntry))
            assert fe.ots_state == "pending" and fe.ots_path is None

    asyncio.run(check())
    _dispose_engine()


def test_cairn_stamp_all_refuses_a_collection_whose_slot_is_held(cairn_env, capsys, no_calendar):
    """`--all` goes through `run_stamp_backfill`, which takes the same claim — and must refuse too."""
    root = cairn_env / "vault"

    async def setup():
        cid = await _seed_collection_with_pending_file(root)
        await _hold_the_slot(cid)

    asyncio.run(setup())
    _dispose_engine()
    rc, out = _cli(["stamp", "--collection", "vault", "--all"], capsys)
    assert rc == 1 and "SKIPPED" in out


def test_cairn_upgrade_refuses_every_held_collection_and_exits_non_zero(
    cairn_env, capsys, no_calendar
):
    """2.36 for `cairn upgrade`: it used to run fleet-wide with no run row and no claim at all."""
    root = cairn_env / "vault"

    async def setup():
        cid = await _seed_collection_with_pending_file(root)
        await _seed_incomplete_proof(cid, cairn_env / "proofs")
        await _hold_the_slot(cid)

    asyncio.run(setup())
    _dispose_engine()
    rc, out = _cli(["upgrade"], capsys)

    assert rc == 1
    assert "SKIPPED" in out and "vault" in out
    assert "no collection was upgraded" in out


# ==============================================================================================
# 2.36a — a fleet run that did some work exits zero
# ==============================================================================================


def _seed_two(cairn_env) -> tuple[Path, Path]:
    busy, free = cairn_env / "busy", cairn_env / "free"

    async def setup():
        busy_id = await _seed_collection_with_pending_file(busy, b"busy bytes")
        free_id = await _seed_collection_with_pending_file(free, b"free bytes")
        await _seed_incomplete_proof(busy_id, cairn_env / "proofs")
        await _seed_incomplete_proof(free_id, cairn_env / "proofs")
        await _hold_the_slot(busy_id)

    asyncio.run(setup())
    _dispose_engine()
    return busy, free


def test_scan_over_two_collections_names_the_skip_and_still_exits_zero(cairn_env, capsys, no_calendar):
    """One busy collection must not fail a run that genuinely scanned another."""
    _seed_two(cairn_env)
    rc, out = _cli(["scan"], capsys)
    assert rc == 0
    assert "[busy] SKIPPED" in out
    assert "[free] added=" in out


def test_stamp_and_upgrade_over_two_collections_exit_zero_when_one_was_processed(
    cairn_env, capsys, no_calendar
):
    """Same rule for the two proof-mutating commands."""
    _seed_two(cairn_env)

    rc, out = _cli(["stamp", "--collection", "free"], capsys)
    assert rc == 0 and "stamped" in out

    rc, out = _cli(["upgrade"], capsys)
    assert rc == 0, out
    assert "[busy] SKIPPED" in out
    assert "no collection was upgraded" not in out


# ==============================================================================================
# 2.37 — `cairn scan`'s refusal reads as a refusal
# ==============================================================================================


def test_a_refused_scan_never_prints_an_all_zeroes_result_line(cairn_env, capsys, no_calendar):
    """The false negative this closes: `added=0 … missing=0 -> skipped` reads as a clean pass.

    A cron `cairn scan` that examined nothing must not be able to record a successful integrity
    check, so the line is replaced by an explicit refusal and the exit status is non-zero.
    """
    root = cairn_env / "vault"

    async def setup():
        cid = await _seed_collection_with_pending_file(root)
        await _hold_the_slot(cid)

    asyncio.run(setup())
    _dispose_engine()
    rc, out = _cli(["scan"], capsys)

    assert rc == 1
    assert "SKIPPED" in out and "already in progress" in out
    assert "added=0" not in out, "a refused scan printed an ordinary result line"
    assert "no collection was scanned" in out


def test_a_scan_that_examined_a_collection_exits_zero(cairn_env, capsys, no_calendar):
    """The control: an unclaimed collection scans normally and still exits 0."""
    root = cairn_env / "vault"
    asyncio.run(_seed_collection_with_pending_file(root))
    _dispose_engine()
    rc, out = _cli(["scan"], capsys)
    assert rc == 0
    assert "SKIPPED" not in out and "-> ok" in out


# ==============================================================================================
# 5.3o — the lease's other two limbs: a keepalive that does not ride on work, and a fence
# ==============================================================================================
#
# The claim is a LEASE, and a lease needs three things: something that takes it, something that
# revokes an abandoned one, and — the part that was missing — a holder that keeps proving it is
# alive independently of the work, plus a check before every mutation that it still holds it.
#
# Every heartbeat used to be written by the completion of a unit of work: a scan batch, a stamp
# batch, one upgraded proof. Hashing a single multi-terabyte file, or one stalled NAS batch, takes
# longer than the abandonment interval — so a scan that was working perfectly starved its own lease,
# was legitimately reclaimed, and then carried straight on into its stamp tail as a SECOND writer
# over a collection something else now owned. Two writers, one canonical proof path, one `os.replace`
# each: a submission destroyed with no trace, which is evidence loss (design D10).


def _run_row(cid: int):
    """Read this collection's newest run row in a session of its own (never the operation's)."""
    from sqlalchemy import desc

    from src.database import get_sessionmaker
    from src.models.db import Run

    async def go():
        async with get_sessionmaker()() as s:
            return await s.scalar(
                select(Run).where(Run.collection_id == cid).order_by(desc(Run.started)).limit(1)
            )

    return go()


def _aware(dt):
    return dt if dt is None or dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def test_the_keepalive_refreshes_the_lease_while_a_single_hash_runs_long(cairn_env, monkeypatch):
    """The heartbeat advances during one long hash, with no batch completing — the starvation fix.

    The scan is held inside `_hash` for the whole test, so not one batch drains and every
    work-completion heartbeat the old design relied on is unreachable. The lease must still move,
    or the reaper would revoke a claim this scan is actively working under.
    """
    root = cairn_env / "slow"
    root.mkdir()
    (root / "big.bin").write_bytes(b"pretend this is a terabyte")

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import collections as coll
        from src.services import scanner

        cid = await seed_collection(root, ots_mode="none")

        # Shrink the keepalive interval; the production default is five minutes, which no test can
        # wait for. The mechanism under test — a timer in its own session — is unchanged.
        real_keepalive = coll.run_keepalive
        monkeypatch.setattr(
            coll,
            "run_keepalive",
            lambda run_id, *, interval=0.05: real_keepalive(run_id, interval=interval),
        )

        released = asyncio.Event()

        async def blocking_hash(path):
            await asyncio.wait_for(released.wait(), timeout=15)
            return "0" * 64

        monkeypatch.setattr(scanner, "_hash", blocking_hash)

        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            task = asyncio.create_task(scanner.scan_collection(s, collection))
            try:
                claimed = None
                for _ in range(300):
                    await asyncio.sleep(0.02)
                    row = await _run_row(cid)
                    if row is not None and row.result == "running":
                        claimed = row
                        break
                assert claimed is not None, "the scan never claimed the collection"

                advanced = None
                for _ in range(300):
                    await asyncio.sleep(0.02)
                    row = await _run_row(cid)
                    if row is not None and _aware(row.heartbeat_at) > _aware(claimed.heartbeat_at):
                        advanced = row
                        break
                assert advanced is not None, (
                    "the lease was never refreshed while the scan sat inside one hash — a long "
                    "hash still starves its own claim"
                )
                assert not advanced.processed, (
                    "a batch completed, so this proves nothing about a keepalive independent of work"
                )
                assert advanced.result == "running"
            finally:
                released.set()
                await task

    asyncio.run(go())
    _dispose_engine()


def test_a_reclaimed_scan_stamps_nothing_and_does_not_overwrite_the_reclamation(
    cairn_env, monkeypatch, caplog
):
    """The fence: a scan whose lease is revoked mid-run stops, stamps nothing, and stays reclaimed.

    Reclamation is simulated exactly as it happens in production — another session marks the run
    `interrupted` (what `reclaim_stale_claim` and the reaper both write) while the scan is between
    files. The scan must then: roll back its in-flight batch, skip the stamp pass entirely (that is
    the proof-mutating half, and a second writer there is the loss), and leave the run row as the
    reclamation wrote it rather than relabelling it `ok`, which would refresh the dead-man's switch
    for a pass that never finished.
    """
    import logging

    # `alembic`'s `fileConfig` (run by the `cairn_env` migration) applies `disable_existing_loggers`,
    # which silently marks an already-created `cairn.*` logger disabled — a caplog assertion would
    # then see nothing and pass or fail for the wrong reason.
    scanner_log = logging.getLogger("cairn.scanner")
    scanner_log.disabled = False
    scanner_log.propagate = True

    root = cairn_env / "fenced"
    root.mkdir()
    for i in range(4):
        (root / f"f{i}.txt").write_bytes(f"payload {i}".encode())

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Collection, Run
        from src.services import proofs, scanner
        from src.services.scanner import _utcnow

        cid = await seed_collection(root, ots_mode="perfile")
        monkeypatch.setattr(scanner, "BATCH", 1)  # drain (and so fence) after every file

        stamp_calls: list[int] = []

        async def recording_stamp(*args, **kwargs):
            stamp_calls.append(1)
            return 0

        monkeypatch.setattr(proofs, "stamp_pending", recording_stamp)

        real_hash = scanner._hash
        hashed: list[str] = []

        async def hash_then_reclaim(path):
            hashed.append(str(path))
            if len(hashed) == 2:
                # Something decided this claim was abandoned and took it — the write the reaper
                # and the in-band reclamation both make.
                async with get_sessionmaker()() as other:
                    await other.execute(
                        sql_update(Run)
                        .where(Run.collection_id == cid, Run.result == "running")
                        .values(result="interrupted", finished=_utcnow())
                    )
                    await other.commit()
            return await real_hash(path)

        monkeypatch.setattr(scanner, "_hash", hash_then_reclaim)

        with caplog.at_level(logging.WARNING, logger="cairn.scanner"):
            async with get_sessionmaker()() as s:
                collection = await s.get(Collection, cid)
                summary = await scanner.scan_collection(s, collection)

        assert summary.result == "skipped", (
            "a scan that lost its claim reported a completed pass"
        )
        assert stamp_calls == [], "the reclaimed scan went on to mutate the proof store"

        row = await _run_row(cid)
        assert row.result == "interrupted", (
            "the reclaimed run was relabelled by the scan that lost it"
        )
        assert not row.stamped
        assert any("RECLAIMED" in r.getMessage() for r in caplog.records), (
            "the reclamation was not reported"
        )

    asyncio.run(go())
    _dispose_engine()


def test_a_stamp_pass_stops_at_the_batch_boundary_when_its_claim_is_reclaimed(
    cairn_env, monkeypatch, no_calendar
):
    """`stamp_pending` fences per batch: the reclaimed pass places no further proof, and says so.

    Proofs already placed STAND. They were placed while the lease was valid, and unwinding a proof
    that exists on disk to tidy up bookkeeping would destroy evidence — the one thing this product
    must never do. What must stop is everything after the fence.
    """
    root = cairn_env / "batched"
    root.mkdir()
    for i in range(4):
        (root / f"f{i}.txt").write_bytes(f"body {i}".encode())

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Collection, FileEntry, Run
        from src.services import collections as coll
        from src.services import ots, proofs
        from src.services.scanner import _utcnow

        cid = await seed_collection(root, ots_mode="perfile")
        now = _utcnow()
        async with get_sessionmaker()() as s:
            for i in range(4):
                s.add(
                    FileEntry(
                        collection_id=cid,
                        relpath=f"f{i}.txt",
                        size=7,
                        status="new",
                        ots_state="pending",
                        first_seen=now,
                        last_checked=now,
                    )
                )
            await s.commit()

        monkeypatch.setattr(
            proofs.get_settings(), "ots_stamp_batch_size", 1, raising=False
        )

        batches: list[int] = []

        def fake_batch(pairs, calendars, staging, *, store_root=None, verdicts=None):
            batches.append(len(pairs))
            outcomes = []
            for _real, out in pairs:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"proof")
                outcomes.append(ots.StampOutcome(kind="placed", digest="d" * 64, state="incomplete"))
            return outcomes

        monkeypatch.setattr(ots, "stamp_batch_via_symlink", fake_batch)

        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            run = Run(collection_id=cid, kind="stamp", started=now, result="running")
            assert await coll.claim_run(s, run) is not None
            run_id = run.id

            async def reclaim_after_first(done: int) -> None:
                # Progress landed for batch 1; now the claim is taken away, exactly as a reaper
                # that judged this operation abandoned would take it.
                async with get_sessionmaker()() as other:
                    await other.execute(
                        sql_update(Run)
                        .where(Run.id == run_id)
                        .values(result="interrupted", finished=_utcnow())
                    )
                    await other.commit()

            with pytest.raises(coll.LeaseLost):
                await proofs.stamp_pending(
                    s,
                    collection,
                    progress=reclaim_after_first,
                    run_id=run_id,
                )

        assert len(batches) == 1, (
            f"the reclaimed stamp kept placing proofs: {len(batches)} batches ran"
        )
        # The proof placed under the valid lease is still on disk — reclamation stops further work,
        # it does not retract completed work.
        assert (Path(cairn_env / "proofs") / str(cid) / "f0.txt.ots").exists()

    asyncio.run(go())
    _dispose_engine()


def test_an_unreclaimed_scan_is_completely_unaffected_by_the_fence(cairn_env, no_calendar):
    """The control: with the lease intact, counts and the finalized run row are exactly as before."""
    root = cairn_env / "normal"
    root.mkdir()
    (root / "a.txt").write_bytes(b"aaa")
    (root / "b.txt").write_bytes(b"bbbb")

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import scanner

        cid = await seed_collection(root, ots_mode="none")
        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            summary = await scanner.scan_collection(s, collection)

        assert (summary.result, summary.added, summary.missing) == ("ok", 2, 0)
        row = await _run_row(cid)
        assert row.result == "ok"
        assert row.added == 2 and row.processed == 2 and row.finished is not None
        return cid

    cid = asyncio.run(go())

    async def rescan():
        from src.database import get_sessionmaker
        from src.models.db import Collection
        from src.services import scanner

        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            return await scanner.scan_collection(s, collection)

    second = asyncio.run(rescan())
    assert (second.result, second.added, second.ok, second.missing) == ("ok", 0, 2, 0)
    _dispose_engine()


def test_a_reclaimed_upgrade_pass_stops_and_leaves_the_reclamation_state(cairn_env, monkeypatch):
    """`ots upgrade` REWRITES the `.ots` in place, so a reclaimed upgrade is a second proof writer.

    The pass must stop at the next proof, keep what it already upgraded, and return without raising
    — including through the session rollback the fence performs, which expires every ORM object the
    finalizing write would otherwise read.
    """
    root = cairn_env / "upg"
    root.mkdir()

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Collection, FileEntry, Run
        from src.services import ots, proofs
        from src.services.scanner import _utcnow

        cid = await seed_collection(root, ots_mode="perfile")
        store = cairn_env / "proofs" / str(cid)
        store.mkdir(parents=True, exist_ok=True)
        now = _utcnow()
        async with get_sessionmaker()() as s:
            for i in range(3):
                proof = store / f"f{i}.txt.ots"
                proof.write_bytes(b"proof")
                s.add(
                    FileEntry(
                        collection_id=cid,
                        relpath=f"f{i}.txt",
                        size=3,
                        status="ok",
                        ots_state="incomplete",
                        ots_path=str(proof),
                        first_seen=now,
                        last_checked=now,
                    )
                )
            await s.commit()

        upgraded: list[str] = []

        def fake_upgrade(path):
            upgraded.append(path)
            if len(upgraded) == 1:
                # Between proof 1 and proof 2 the claim is judged abandoned and taken away.
                async def reclaim():
                    async with get_sessionmaker()() as other:
                        await other.execute(
                            sql_update(Run)
                            .where(Run.collection_id == cid, Run.result == "running")
                            .values(result="interrupted", finished=_utcnow())
                        )
                        await other.commit()

                asyncio.run_coroutine_threadsafe(reclaim(), loop).result(timeout=10)
            return True

        loop = asyncio.get_running_loop()
        monkeypatch.setattr(ots, "upgrade", fake_upgrade)

        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            outcome = await proofs.upgrade_collection(s, collection)

        assert len(upgraded) == 1, (
            f"the reclaimed upgrade kept rewriting proofs: {len(upgraded)} touched"
        )
        assert not outcome.refused
        row = await _run_row(cid)
        assert row.result == "interrupted", "the loser of the reclamation relabelled the run"

    asyncio.run(go())
    _dispose_engine()


# ==============================================================================================
# 5.3p — the fence is a check-then-act, so the last rung is a lock AT the resource
# ==============================================================================================
#
# `lease_held()` reads the claim and returns; the `os.replace` happens some milliseconds later,
# after a calendar round-trip. A reclamation landing in that window puts TWO placers on one
# canonical proof path with both of them believing they hold the collection: each finds the path
# free, each replaces, and the first submission is gone with no trace it ever existed. No amount of
# re-reading the datastore closes that window — the check and the act are different operations.
#
# So the last rung of the ladder is a lock on the resource itself: an advisory `flock` on
# `<proof_store>/<collection_id>/.lock`, held across the batch's placement, with the lease re-read
# AFTER it is taken. The winner of the lock does its turn alone; the loser discovers on the re-read
# that its claim is gone and stops without writing.


def _placing_batch(marker: str, calls: list[str] | None = None):
    """A `stamp_batch_via_symlink` stand-in that goes through the REAL placement rules.

    Only the calendar is faked: each member gets a staged file of recognisable bytes, which
    `ots._place_proof` then inspects, preserves-if-occupied and places. So an assertion about the
    `.superseded` archive family is an assertion about what production would have preserved.
    """
    from src.services import ots

    def fake_batch(pairs, calendars, staging, *, store_root=None, verdicts=None):
        outcomes = []
        staging = Path(staging)
        staging.mkdir(parents=True, exist_ok=True)
        for i, (_real, out) in enumerate(pairs):
            if calls is not None:
                calls.append(marker)
            staged = staging / f"{marker}-{i}-{Path(out).name}"
            staged.write_bytes(f"proof placed by {marker}".encode())
            outcomes.append(ots._place_proof(staged, Path(out), store_root=store_root))
        return outcomes

    return fake_batch


def _archive_families(store: Path, cid: int) -> list[Path]:
    """Every preserved proof under `<proof_store>/.superseded/<cid>/` — empty means nothing was
    ever superseded, which is the only acceptable answer when one placer never got to write."""
    from src.services import ots

    root = ots.superseded_root(store, cid)
    return sorted(f for f in root.rglob("*") if f.is_file()) if root.exists() else []


def test_a_reclamation_after_the_fence_cannot_put_two_placers_on_one_proof_path(
    cairn_env, monkeypatch, no_calendar
):
    """The race the DB fence alone cannot see: reclaimed AFTER `lease_held` answered, BEFORE placing.

    `lease_held` is patched to reclaim the collection at the exact instant it is about to return
    ``True`` — the window between the check and the act — and to let the replacement claimant run a
    complete stamp of the same file under its own, valid claim. The original pass then walks into
    placement believing it still owns the collection, which is precisely the two-writer state the
    claim exists to exclude.

    What must hold afterwards: exactly ONE canonical proof, and it is the one placed by the operation
    that actually held the claim; the loser raised `LeaseLost` instead of writing; and nothing was
    superseded, because the loser never got as far as needing to preserve anything.
    """
    root = cairn_env / "raced"

    async def go():
        from sqlalchemy import update as sql_update

        from src.database import get_sessionmaker
        from src.models.db import Collection, FileEntry, Run
        from src.services import collections as coll
        from src.services import proofs
        from src.services.scanner import _utcnow

        cid = await _seed_collection_with_pending_file(root, b"the only copy")
        store = cairn_env / "proofs"
        canonical = store / str(cid) / "doc.txt.ots"

        placed: list[str] = []
        monkeypatch.setattr(
            proofs.ots, "stamp_batch_via_symlink", _placing_batch("A", placed)
        )

        ids: dict[str, int] = {}
        real_lease_held = coll.lease_held

        async def racing_lease_held(run_id):
            held = await real_lease_held(run_id)
            if run_id != ids.get("a") or ids.get("raced"):
                return held
            ids["raced"] = 1
            # The answer for A is computed and about to be returned as True. NOW the reaper judges
            # A abandoned and revokes its claim — the write `reclaim_stale_claim` and the sweep both
            # make — and the replacement claimant takes the freed slot and stamps the same file.
            async with get_sessionmaker()() as other:
                await other.execute(
                    sql_update(Run)
                    .where(Run.id == ids["a"])
                    .values(result="interrupted", finished=_utcnow())
                )
                await other.commit()
            async with get_sessionmaker()() as b_session:
                monkeypatch.setattr(
                    proofs.ots, "stamp_batch_via_symlink", _placing_batch("B", placed)
                )
                collection_b = await b_session.get(Collection, cid)
                run_b = Run(collection_id=cid, kind="stamp", started=_utcnow(), result="running")
                assert await coll.claim_run(b_session, run_b) is not None
                assert await proofs.stamp_pending(b_session, collection_b, run_id=run_b.id) == 1
                monkeypatch.setattr(
                    proofs.ots, "stamp_batch_via_symlink", _placing_batch("A", placed)
                )
            return held

        monkeypatch.setattr(coll, "lease_held", racing_lease_held)

        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            run_a = Run(collection_id=cid, kind="stamp", started=_utcnow(), result="running")
            assert await coll.claim_run(s, run_a) is not None
            ids["a"] = run_a.id
            with pytest.raises(coll.LeaseLost):
                await proofs.stamp_pending(s, collection, run_id=run_a.id)

        assert ids.get("raced"), "the race seam never fired; this test proved nothing"
        assert placed == ["B"], (
            f"the pass that lost its claim still placed a proof: {placed}"
        )
        assert canonical.read_bytes() == b"proof placed by B", (
            "the loser's proof is canonical — a submission was destroyed by the second writer"
        )
        assert _archive_families(store, cid) == [], (
            "something had to be superseded, so two placers reached one canonical path"
        )

        async with get_sessionmaker()() as s:
            fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
            assert fe.ots_state == "incomplete" and fe.ots_path == str(canonical)

    asyncio.run(go())
    _dispose_engine()


def test_a_second_placer_is_excluded_by_the_lock_and_gives_up_transiently(
    cairn_env, monkeypatch, no_calendar
):
    """The lock is real: while another placer holds it, this pass places nothing and stays pending.

    The holder here is a plain second file descriptor on the same lock file — which is exactly what
    the host `cairn stamp` process is to the container's scheduler: a different opener of one file
    on one shared filesystem. The wait is bounded, and running out of it is TRANSIENT: nothing is
    placed, nothing is dropped to `none`, and the next pass takes the file.
    """
    root = cairn_env / "excluded"

    async def go():
        from src.database import get_sessionmaker
        from src.models.db import Collection, FileEntry
        from src.services import ots, proofs

        cid = await _seed_collection_with_pending_file(root, b"contended")
        store = cairn_env / "proofs"

        placed: list[str] = []
        monkeypatch.setattr(ots, "stamp_batch_via_symlink", _placing_batch("A", placed))
        # A bounded wait is the point; sixty real seconds in a test is not.
        monkeypatch.setattr(ots, "PLACEMENT_LOCK_TIMEOUT_SECONDS", 0.2)

        other_process = ots.CollectionProofLock(store, cid)
        other_process.acquire()
        try:
            async with get_sessionmaker()() as s:
                collection = await s.get(Collection, cid)
                assert await proofs.stamp_pending(s, collection) == 0
        finally:
            other_process.release()

        assert placed == [], "the pass placed a proof while another placer held the lock"
        assert not (store / str(cid) / "doc.txt.ots").exists()

        async with get_sessionmaker()() as s:
            fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
            assert fe.ots_state == "pending", "a contended lock must never drop a file to `none`"

        # And once the holder is gone the very same pass succeeds — the refusal was transient.
        async with get_sessionmaker()() as s:
            collection = await s.get(Collection, cid)
            assert await proofs.stamp_pending(s, collection) == 1
        assert placed == ["A"]

    asyncio.run(go())
    _dispose_engine()


def test_a_store_that_cannot_lock_warns_once_and_keeps_the_datastore_fence(
    cairn_env, monkeypatch, no_calendar, caplog
):
    """Accepted limitation: a proof store whose filesystem has no `flock` degrades, it does not stop.

    Some network stores (CIFS/SMB, some FUSE mounts, NFS without a lock daemon) accept every write
    Cairn makes and refuse advisory locking outright. That answer is DETERMINISTIC — the same errno
    forever — so treating it as transient would wedge notarization permanently on such a store: a
    concurrency nicety costing the notary its ability to notarize. Same errno discipline as the
    directory-sync degrade: one WARNING per proof store, then proceed on the datastore fence alone.
    """
    import logging

    # `alembic`'s `fileConfig` disables already-created `cairn.*` loggers, so a caplog assertion
    # would otherwise see nothing and pass for the wrong reason.
    ots_log = logging.getLogger("cairn.ots")
    ots_log.disabled = False
    ots_log.propagate = True

    root = cairn_env / "nolocks"

    async def go():
        import errno

        from src.database import get_sessionmaker
        from src.models.db import Collection, FileEntry
        from src.services import ots, proofs
        from src.services.scanner import _utcnow

        cid = await _seed_collection_with_pending_file(root, b"unlockable")
        store = cairn_env / "proofs"
        # A second pending file, so the pass takes the lock twice and the warning can repeat.
        (root / "two.txt").write_bytes(b"unlockable too")
        now = _utcnow()
        async with get_sessionmaker()() as s:
            s.add(
                FileEntry(
                    collection_id=cid,
                    relpath="two.txt",
                    size=14,
                    status="new",
                    ots_state="pending",
                    first_seen=now,
                    last_checked=now,
                )
            )
            await s.commit()

        # Process memory, so give the test its own set rather than poisoning the next test's store.
        seen: set[str] = set()
        monkeypatch.setattr(ots, "_BEST_EFFORT_PLACEMENT_LOCK", seen)
        monkeypatch.setattr(proofs.get_settings(), "ots_stamp_batch_size", 1, raising=False)

        def no_locks(fd, op):
            raise OSError(errno.ENOLCK, "No locks available")

        monkeypatch.setattr(ots.fcntl, "flock", no_locks)

        placed: list[str] = []
        monkeypatch.setattr(ots, "stamp_batch_via_symlink", _placing_batch("A", placed))

        with caplog.at_level(logging.WARNING, logger="cairn.ots"):
            async with get_sessionmaker()() as s:
                collection = await s.get(Collection, cid)
                assert await proofs.stamp_pending(s, collection) == 2

        assert placed == ["A", "A"], "a store that cannot lock stopped notarizing"
        warnings = [
            r for r in caplog.records if "does not support advisory locking" in r.getMessage()
        ]
        assert len(warnings) == 1, (
            f"the degrade warned {len(warnings)} times; it must warn once per proof store"
        )
        assert seen == {str(store)}, "the store was not recorded as unlockable"

        async with get_sessionmaker()() as s:
            states = set(
                await s.scalars(
                    select(FileEntry.ots_state).where(FileEntry.collection_id == cid)
                )
            )
            assert states == {"incomplete"}

    asyncio.run(go())
    _dispose_engine()


# --- 5.3o.1 coverage completion: the keepalive's failure branch --------------------------------
#
# The keepalive is a liveness signal, not part of the work, so a broken datastore must cost the
# operation nothing: no exception into the block, and no unbounded WARNING loop for the life of a
# multi-hour pass. Giving up is deliberately safe — the lease then ages out and the fence stops the
# operation before it mutates anything — which is only true if giving up is also *quiet*.


def test_a_failing_keepalive_gives_up_after_three_tries_and_never_touches_the_operation(
    monkeypatch, caplog
):
    """A keepalive whose write always raises: the block completes, it is logged, and it stops at 3.

    Three properties, all of which a naive implementation gets wrong in a different direction:

    * **Nothing surfaces into the operation.** `run_keepalive` awaits its task on exit, so an
      exception escaping `_keepalive_loop` would be re-raised there — a failed heartbeat would fail
      the scan it was only supposed to be describing.
    * **The counter is consecutive, not cumulative.** The scripted success in the middle must reset
      it; otherwise a datastore that hiccups twice an hour apart retires the keepalive on an
      operation that is perfectly healthy.
    * **It stops.** After the third failure in a row there are no further attempts at all — not a
      slower loop, not a quieter one.
    """
    import logging
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from src.services import collections as coll

    # `alembic`'s `fileConfig` (run by other modules' `cairn_env`) applies
    # `disable_existing_loggers`, which leaves an already-created `cairn.*` logger disabled — the
    # caplog assertions below would then pass or fail for reasons unrelated to the keepalive.
    coll_log = logging.getLogger("cairn.collections")
    coll_log.disabled = False
    coll_log.propagate = True

    calls: list[int] = []
    # fail, fail, SUCCEED (the counter resets here), then fail three in a row -> give up at six.
    script = [False, False, True, False, False, False]

    async def flaky_touch(run_id):
        calls.append(run_id)
        i = len(calls) - 1
        if i < len(script) and script[i]:
            return True
        raise OperationalError(
            "UPDATE runs SET heartbeat_at", {}, sqlite3.OperationalError("database is locked")
        )

    monkeypatch.setattr(coll, "touch_heartbeat", flaky_touch)

    body_finished: list[str] = []

    async def go():
        # The production interval is five minutes; only the timer is shrunk, not the mechanism.
        async with coll.run_keepalive(4242, interval=0.01):
            for _ in range(500):
                await asyncio.sleep(0.01)
                if len(calls) >= len(script):
                    break
            # Room for a seventh attempt, if the loop is going to make one.
            await asyncio.sleep(0.25)
            body_finished.append("the operation body ran to completion")

    with caplog.at_level(logging.WARNING, logger="cairn.collections"):
        asyncio.run(go())  # a keepalive failure re-raised on exit would fail HERE

    assert body_finished == ["the operation body ran to completion"], (
        "the operation did not complete: a keepalive failure reached the block it was wrapping"
    )
    assert calls == [4242] * 6, (
        f"expected exactly 6 heartbeat attempts (2 fail, 1 succeed and reset, 3 fail and give up), "
        f"got {len(calls)} — the keepalive either kept retrying a broken datastore or counted "
        f"failures cumulatively"
    )

    messages = [r.getMessage() for r in caplog.records if r.name == "cairn.collections"]
    failures = [m for m in messages if "keepalive heartbeat failed" in m]
    assert len(failures) == 5, f"every failed attempt is reported once: {messages}"
    assert "(1 in a row)" in failures[0] and "(1 in a row)" in failures[2], (
        "the run of failures restarts after the successful heartbeat"
    )
    assert any("giving up after 3 consecutive failures" in m for m in messages), (
        "the operator is told the lease is no longer being refreshed, and why"
    )
    assert any(r.exc_info for r in caplog.records if "keepalive heartbeat failed" in r.getMessage()), (
        "the first failure of a run carries its traceback — a bare message cannot be diagnosed"
    )
