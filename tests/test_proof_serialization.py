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
