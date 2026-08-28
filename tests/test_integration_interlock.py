"""Where slice A (#15, proof preservation) and slice B (#21, restored-changed) meet.

Three things live here because they belong to neither slice:

* **The interlock (task 4.2).** A `perfile` file goes missing, comes back with DIFFERENT bytes, and
  the scan that notices queues a re-stamp. Without #21 the wrong bytes are adopted as "restored"
  and no re-stamp is ever queued; without #15 the re-stamp that #21 queues is precisely what
  destroys the original bytes' proof. Only together do both proofs survive — and only the merged
  tree can be asked. Nothing on the placement path is stubbed: the only fake is the `ots` binary
  itself.
* **The accepted #39 limitation (task 4.2a).** A moved file keeps `ots_path` pointing at its OLD
  relpath's canonical proof, so a later file stamped at that path takes it over. #15 means the
  moved file's proof is PRESERVED rather than destroyed, and the provenance ladder means `/verify`
  now SAYS so — but every consumer still resolves the recorded path, i.e. the other file's proof.
  The limitation is asserted here rather than assumed.
* **The refusal path (task 1a).** ``claim_run``'s rollback expires every ORM object in the session,
  so a `collection.id` read after a lost claim raised ``MissingGreenlet`` from the very log line
  meant to report the refusal — turning "another operation is in progress" into a crash.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_integration_interlock.py``
"""

from __future__ import annotations

import asyncio
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
from tests.test_proof_serialization import _dispose_engine, _hold_the_slot


@pytest.fixture(autouse=True)
def _reset_dir_sync_degrade():
    """The unsupported-directory-`fsync` degrade is process memory, per proof store — reset it.

    Also undoes ``alembic``'s ``disable_existing_loggers``, which the ``cairn_env`` migration
    applies as a side effect and which would silently mute any ``caplog`` assertion here.
    """
    from src.services import ots

    _unmute_cairn_loggers()
    ots._BEST_EFFORT_DIR_SYNC.clear()
    yield
    ots._BEST_EFFORT_DIR_SYNC.clear()


async def _scan(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    async with get_sessionmaker()() as s:
        return await scan_collection(s, await s.get(Collection, cid))


async def _entry(cid: int, relpath: str):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(FileEntry).where(
                FileEntry.collection_id == cid, FileEntry.relpath == relpath
            )
        )


async def _events(cid: int, kind: str):
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        return list(
            await s.scalars(
                select(Event).where(Event.collection_id == cid, Event.kind == kind).order_by(Event.id)
            )
        )


# ==============================================================================================
# 4.2 — the cross-slice interlock
# ==============================================================================================


async def test_a_changed_restore_restamps_and_the_original_proof_survives(cairn_env, monkeypatch):
    """The end-to-end scenario neither slice can produce alone.

    v1 is stamped and its proof anchored. The file disappears, then something else takes its place.
    #21 refuses to call that a restore and queues a re-stamp for the new bytes; #15 makes that
    re-stamp preserve the anchored proof for v1 instead of writing over it. Afterwards BOTH proofs
    exist: v2's at the canonical path, v1's in its digest's archive family, byte-identical.

    The `ots` binary is the only fake. `stamp_pending`, `stamp_batch_via_symlink`, `_place_proof`
    and `_preserve_proof` all run for real — a stub anywhere on that path would test the test.
    """
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import ots, proofs

    v1, v2 = b"the original contract", b"a different document entirely"
    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    doc = root / "contract.pdf"
    doc.write_bytes(v1)
    cid = await seed_collection(root)

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    # 1. First scan: the file is added and stamped for real.
    assert (await _scan(cid)).added == 1
    canonical = proofs.proof_path(get_settings(), cid, "contract.pdf")
    fe = await _entry(cid, "contract.pdf")
    assert fe.ots_path == str(canonical) and fe.ots_digest == _sha(v1)

    # 2. Time passes and the daily upgrade lands the Bitcoin anchor. This is what makes the loss
    #    expensive: the proof at the canonical path is now a years-old confirmed attestation, and
    #    nothing about the file itself records that date.
    original_proof = _write_proof(canonical, _sha(v1), height=800_000)
    async with get_sessionmaker()() as s:
        row = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        row.ots_state = "complete"
        await s.commit()

    # 3. The file goes missing.
    doc.unlink()
    assert (await _scan(cid)).missing == 1
    assert (await _entry(cid, "contract.pdf")).status == "missing"

    # 4. Something comes back at the path — but not what left. (#21)
    doc.write_bytes(v2)
    summary = await _scan(cid)
    assert summary.restored_changed == 1
    assert summary.restored == 0, "the wrong bytes must never be adopted as a clean restore"
    assert ("restored_changed", "contract.pdf") in summary.alarming

    events = await _events(cid, "restored_changed")
    assert len(events) == 1
    assert events[0].acknowledged_at is None, "a wrong restore must nag"
    assert _sha(v1) in events[0].detail and _sha(v2) in events[0].detail

    # 5. …and the re-stamp that queued has already run, inside the same scan. (#15)
    fe = await _entry(cid, "contract.pdf")
    assert fe.status == "modified"
    assert fe.sha256 == _sha(v2)
    assert fe.ots_state == "incomplete", "the new bytes got their own fresh proof"
    assert fe.ots_digest == _sha(v2)
    assert fe.ots_path == str(canonical)

    # The canonical path serves the NEW bytes' proof…
    assert ots.read_proof_facts(canonical).digest == _sha(v2)
    # …and v1's anchored proof is intact in its digest's archive family. This is the assertion the
    # whole change exists for: before #15 these bytes were gone.
    archived = _archive_files(cairn_env / "proofs", cid)
    assert [p.name for p in archived] == [f"{_sha(v1)}.ots"]
    assert archived[0].read_bytes() == original_proof
    assert archived[0].parent.name == _sha(v1)[:2]
    assert ots.read_proof_facts(archived[0]).anchored, "the preserved proof kept its attestation"


# ==============================================================================================
# 4.2a — the accepted #39 limitation, pinned
# ==============================================================================================


def test_a_moved_file_keeps_pointing_at_its_old_paths_proof(cairn_env, monkeypatch, capsys):
    """#39: `ots_path` is not repointed by a move, so a later file at the old path takes it over.

    What this change fixes is that the takeover no longer DESTROYS the moved file's proof (#15
    preserves it) and is no longer invisible (`/verify` now blames the proof, with provenance,
    instead of passing green or reading as harmless staleness). What it does NOT fix — deliberately,
    it is Phase-2 work filed as #39 — is the pointer: verification, download, export and the upgrade
    pass all still resolve `ots_path`, which now names the OTHER file's proof.
    """
    from fastapi.testclient import TestClient

    from src import database
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.main import app
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs

    x, y = b"the moved file's bytes", b"an unrelated newcomer at the vacated path"
    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    (root / "a.txt").write_bytes(x)

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    async def setup() -> tuple[int, Path, bytes]:
        cid = await seed_collection(root)
        await _scan(cid)  # a.txt added + stamped
        canonical = proofs.proof_path(get_settings(), cid, "a.txt")
        proof_for_x = canonical.read_bytes()
        assert ots.read_proof_facts(canonical).digest == _sha(x)

        # The move. `_reconcile_moves` repoints the row's relpath and keeps its identity — including
        # `ots_path`, which still names `a.txt.ots`.
        (root / "a.txt").rename(root / "b.txt")
        assert (await _scan(cid)).moved == 1
        moved = await _entry(cid, "b.txt")
        assert moved.ots_path == str(canonical), "the move left the proof pointer on the old path"

        # A brand-new, unrelated file takes the vacated path and is stamped there.
        (root / "a.txt").write_bytes(y)
        assert (await _scan(cid)).added == 1
        return cid, canonical, proof_for_x

    cid, canonical, proof_for_x = asyncio.run(setup())
    _dispose_engine()

    async def inspect():
        moved = await _entry(cid, "b.txt")
        newcomer = await _entry(cid, "a.txt")
        return moved, newcomer

    moved, newcomer = asyncio.run(inspect())
    _dispose_engine()

    # 1. The moved file's proof was PRESERVED, not destroyed — #15 doing its job under #39.
    archived = _archive_files(cairn_env / "proofs", cid)
    assert [p.name for p in archived] == [f"{_sha(x)}.ots"]
    assert archived[0].read_bytes() == proof_for_x
    # …at exactly the address design D8 tells an operator to recover it from by hand:
    # `.superseded/<collection_id>/<dd>/<digest>.ots`.
    assert archived[0].relative_to(cairn_env / "proofs").parts == (
        ".superseded",
        str(cid),
        _sha(x)[:2],
        f"{_sha(x)}.ots",
    )

    # 2. …but the moved row still resolves the old path, which now holds the newcomer's proof.
    assert moved.ots_path == str(canonical)
    assert moved.ots_digest == _sha(x), "provenance still records what Cairn placed for these bytes"
    assert newcomer.ots_path == str(canonical), "two rows, one canonical proof path (#39)"
    assert ots.read_proof_facts(canonical).digest == _sha(y)

    # 3. `/verify` on the moved row now says so. Not green, and not `proof-stale`: the on-disk proof
    #    is not the proof Cairn recorded placing, which is an established finding, not a guess.
    #    (`ots.verify` is NOT stubbed — a digest disagreement is settled from the local parse alone,
    #    before any explorer lookup.)
    database.reset_engine()
    with TestClient(app) as client:
        import re

        token = re.search(r'name="csrf-token" content="([^"]+)"', client.get("/").text).group(1)
        r = client.post("/verify", data={"csrf_token": token, "file_id": moved.id})
        assert r.status_code == 200, r.text
        html = r.text
        assert "not the proof Cairn placed" in html
        assert "predates" not in html, "a staleness reading here would be a false reassurance"
        assert "verdict--danger" in html, "a takeover must not render as a pass"

        # 4. Download serves the recorded path's bytes — the newcomer's proof, under the moved
        #    file's name. This is the limitation, asserted rather than assumed.
        dl = client.get(f"/verify/export/{moved.id}")
        assert dl.status_code == 200
        assert dl.content == canonical.read_bytes()
        assert dl.headers["content-disposition"].endswith('filename="b.txt.ots"')
    database.reset_engine()

    # 5. `cairn export`'s bundle resolves the same path.
    async def export():
        async with get_sessionmaker()() as s:
            fe = await s.get(FileEntry, moved.id)
            return proofs.export_bundle(fe, cairn_env / "out", root)

    dest = asyncio.run(export())
    _dispose_engine()
    assert dest.name == "b.txt"  # export_bundle returns the copied FILE
    assert dest.with_name("b.txt.ots").read_bytes() == canonical.read_bytes()

    # 6. …and so does the upgrade pass.
    upgraded_paths: list[str] = []

    async def upgrade():
        async with get_sessionmaker()() as s:
            fe = await s.get(FileEntry, moved.id)
            fe.ots_state = "incomplete"
            await s.commit()
            monkeypatch.setattr(
                ots, "upgrade", lambda path: upgraded_paths.append(str(path)) or False
            )
            await proofs.upgrade_incomplete(s, await s.get(Collection, cid))

    asyncio.run(upgrade())
    _dispose_engine()
    assert str(canonical) in upgraded_paths
    capsys.readouterr()


# ==============================================================================================
# 1a — a lost claim is a refusal, not a crash
# ==============================================================================================


async def test_a_scan_whose_claim_is_lost_returns_skipped_without_raising(cairn_env):
    """``claim_run``'s rollback expires the session's ORM objects — including ``collection``.

    The refusal path then read ``collection.id`` to log the refusal, which triggers a lazy refresh
    and raises ``MissingGreenlet`` out of ``scan_collection``. A concurrency guard whose refusal
    path crashes is worse than no guard: the scheduler tick dies where it should have skipped, and
    the CLI reports an error where it should have reported a busy collection.
    """
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services.scanner import scan_collection

    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_bytes(b"payload")
    cid = await seed_collection(root)
    await _hold_the_slot(cid)

    async with get_sessionmaker()() as s:
        collection = await s.get(Collection, cid)
        summary = await scan_collection(s, collection)
        assert summary.result == "skipped"
        assert summary.collection_id == cid
        # Nothing was walked, and the holder's run is the only one on record.
        assert (summary.added, summary.modified, summary.missing) == (0, 0, 0)
        runs = list(await s.scalars(select(Run).where(Run.collection_id == cid)))
        assert len(runs) == 1 and runs[0].result == "running"
        # The expired instance is usable again for the caller that has to report the skip —
        # `run_due_scans` reads `hash_cadence_seconds` off it in a `finally` the instant
        # this returns, so a refusal that leaves it expired kills the whole tick.
        assert collection.id == cid and collection.name == "vault"
        assert collection.hash_cadence_seconds > 0


# ==============================================================================================
# The CLI scan line names a wrong restore (task 3.6's "for the CLI scan line")
# ==============================================================================================


def test_the_cli_scan_line_names_a_file_that_came_back_changed(cairn_env, capsys, monkeypatch):
    """`modified=1` alone reads as an ordinary edit — the one reading #21 exists to prevent.

    A restored-changed file IS counted in `modified` (its status is `modified`), so without this
    the command line reports the most alarming classification Cairn has exactly like the most
    routine one. The segment appears only when non-zero, so ordinary output is unchanged.
    """
    import src.cli as cli
    from src.services import ots

    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    doc = root / "doc.txt"
    doc.write_bytes(b"the original")

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    async def setup() -> int:
        cid = await seed_collection(root)
        await _scan(cid)
        doc.unlink()
        await _scan(cid)
        doc.write_bytes(b"something else entirely")
        return cid

    asyncio.run(setup())
    _dispose_engine()

    rc = cli.main(["scan"])
    out = capsys.readouterr()
    _dispose_engine()

    assert rc == 0
    assert "restored_changed=1" in out.out
    assert "modified=1" in out.out


def test_an_ordinary_scan_line_is_unchanged(cairn_env, capsys, monkeypatch):
    """The counterpart: with nothing to report the line must not grow a `restored_changed=0`."""
    import src.cli as cli
    from src.services import ots

    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_bytes(b"hello")

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    asyncio.run(seed_collection(root))
    _dispose_engine()

    rc = cli.main(["scan"])
    out = capsys.readouterr()
    _dispose_engine()

    assert rc == 0
    assert "restored_changed" not in out.out
    assert "[vault] added=1 modified=0 missing=0 restored=0 baselined=0" in out.out


# ==============================================================================================
# A refused backfill changes nothing (design D10 — the claim covers the WHOLE sequence)
# ==============================================================================================


def test_a_refused_stamp_all_does_not_queue_the_baseline(cairn_env, capsys, monkeypatch):
    """`run_stamp_backfill` queued the baseline BEFORE taking the claim.

    So a refused `cairn stamp --all` printed "nothing was stamped" while every `ots_state='none'`
    row in the collection had already been committed to `pending` — a write to the collection made
    outside its single-operation claim, which the operation actually holding the slot would then
    pick up in its own stamp pass. A refusal must leave the collection exactly as it found it.
    """
    import src.cli as cli
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import ots

    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_bytes(b"an unstamped baseline file")

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    async def setup() -> int:
        cid = await seed_collection(root)
        await _scan(cid)
        # Put the row back into the pre-existing, never-stamped state a backfill exists to fix.
        async with get_sessionmaker()() as s:
            fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
            fe.ots_state = "none"
            fe.ots_path = None
            fe.ots_digest = None
            await s.commit()
        await _hold_the_slot(cid)
        return cid

    cid = asyncio.run(setup())
    _dispose_engine()

    rc = cli.main(["stamp", "--collection", "vault", "--all"])
    out = capsys.readouterr()
    _dispose_engine()

    assert rc == 1
    assert "SKIPPED" in out.err

    async def check():
        async with get_sessionmaker()() as s:
            return await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))

    fe = asyncio.run(check())
    _dispose_engine()
    assert fe.ots_state == "none", "a refused backfill queued the baseline anyway"
