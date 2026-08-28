"""OTS notary: proof-state parsing, symlink stamping, scanner queueing, export, staleness.

The ``ots`` subprocess is always MOCKED (``monkeypatch`` of ``ots._run_ots``) so the suite needs
no network. Mirrors ``tests/test_scanner.py``'s temp-DB fixture.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ots.py``
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

# --- Canned ``ots`` CLI output -------------------------------------------------------------

INFO_PENDING = """\
File sha256 hash: c27c7cda5e69001821354acb7757348d58b4b2044302e7a8817b3d04335b8cbb
Timestamp:
append b1556834dd0d7801f19bd7c6943f48b9
sha256
 -> append 30fb229878754020aedb299a3f30bd0b
    verify PendingAttestation('https://a.pool.opentimestamps.org')
 -> append 902906dc607c7be90b36ecd54297b123
    verify PendingAttestation('https://b.pool.eternitywall.com')
"""

INFO_COMPLETE = """\
File sha256 hash: c27c7cda5e69001821354acb7757348d58b4b2044302e7a8817b3d04335b8cbb
Timestamp:
append b1556834dd0d7801f19bd7c6943f48b9
sha256
 -> append 30fb229878754020aedb299a3f30bd0b
    verify BitcoinBlockHeaderAttestation(800000)
"""

VERIFY_SUCCESS = "Success! Bitcoin block 800000 attests existence as of 2024-01-01 UTC\n"
VERIFY_PENDING = "Calendar https://a.pool.opentimestamps.org: Pending confirmation in Bitcoin blockchain\n"  # noqa: E501


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


async def _make_collection(root: Path, *, mode: str = "worm", ots_mode: str = "none") -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s, user_id=uid, name="c", root=str(root), mode=mode, ots_mode=ots_mode
        )
        return collection.id


# --- info(): offline proof-state parsing ----------------------------------------------------


def test_info_classifies_pending(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")
    monkeypatch.setattr(ots, "_run_ots", lambda args, timeout=ots.DEFAULT_TIMEOUT: (0, INFO_PENDING, ""))

    result = ots.info(proof)
    assert result.state == "incomplete"
    assert result.block_height is None
    assert result.calendars == [
        "https://a.pool.opentimestamps.org",
        "https://b.pool.eternitywall.com",
    ]


def test_info_classifies_complete(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")
    monkeypatch.setattr(ots, "_run_ots", lambda args, timeout=ots.DEFAULT_TIMEOUT: (0, INFO_COMPLETE, ""))

    result = ots.info(proof)
    assert result.state == "complete"
    assert result.block_height == 800000


def test_info_missing_file_is_none(tmp_path, monkeypatch):
    from src.services import ots

    def _boom(args, timeout=ots.DEFAULT_TIMEOUT):  # pragma: no cover - must not be reached
        raise AssertionError("ots must not be invoked for a missing proof")

    monkeypatch.setattr(ots, "_run_ots", _boom)
    assert ots.info(tmp_path / "nope.ots").state == "none"


# --- stamp_via_symlink(): writes only to the proof store ------------------------------------


def test_stamp_via_symlink_writes_proof_store_not_collection(tmp_path, monkeypatch):
    from src.services import ots

    collection_root = tmp_path / "collection"
    collection_root.mkdir()
    real = collection_root / "photo.jpg"
    real.write_bytes(b"jpeg-bytes")

    store = tmp_path / "proofs"
    staging = store / ".staging"
    out = store / "1" / "photo.jpg.ots"

    captured: dict[str, list[str]] = {}

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        captured["args"] = args
        # Real ``ots stamp`` writes ``<symlink>.ots`` beside the staged symlink.
        link = Path(args[-1])
        link.with_name(link.name + ".ots").write_bytes(b"proof")
        return 0, "", ""

    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.stamp_via_symlink(real, out, ["https://cal.example"], staging)

    assert result == out
    assert out.exists() and out.read_bytes() == b"proof"
    # Nothing written under the collection root, and the staging symlink is cleaned up.
    assert list(collection_root.iterdir()) == [real]
    assert not any(staging.iterdir())
    # Calendars passed as repeated -c flags; the input is a staging symlink, not the real file.
    assert "-c" in captured["args"] and "https://cal.example" in captured["args"]
    assert str(staging) in captured["args"][-1]


def test_stamp_via_symlink_raises_when_no_proof(tmp_path, monkeypatch):
    from src.services import ots

    real = tmp_path / "f.bin"
    real.write_bytes(b"data")
    monkeypatch.setattr(ots, "_run_ots", lambda args, timeout=ots.DEFAULT_TIMEOUT: (1, "", "boom"))

    with pytest.raises(ots.OtsError):
        ots.stamp_via_symlink(real, tmp_path / "out.ots", [], tmp_path / ".staging")


# --- verify(): node backend (CLI), by digest ------------------------------------------------


def test_verify_complete_proof(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 0, "", VERIFY_SUCCESS  # CLI logs to stderr
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(
        proof, "c27c7cda5e69001821354acb7757348d58b4b2044302e7a8817b3d04335b8cbb",
        backend="node",
    )
    assert result.verified is True
    assert result.block_height == 800000
    assert result.existed_by == "2024-01-01 UTC"


def test_verify_node_backend_passes_bitcoin_node(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    seen: dict[str, list[str]] = {}

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        seen["args"] = args
        return 0, "", VERIFY_SUCCESS
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    ots.verify(
        proof, "c27c7cda5e69001821354acb7757348d58b4b2044302e7a8817b3d04335b8cbb",
        backend="node", node_rpc_url="http://user:pw@127.0.0.1:8332",
    )
    # `--bitcoin-node` must precede the `verify` subcommand (it is a global option).
    assert seen["args"][:2] == ["--bitcoin-node", "http://user:pw@127.0.0.1:8332"]
    assert "verify" in seen["args"]


def test_verify_pending_proof_not_verified(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_PENDING, ""
        return 1, "", VERIFY_PENDING
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "deadbeef", backend="node")
    assert result.verified is False
    assert result.state == "incomplete"


# --- verify(): explorer backend (default; no Bitcoin node needed) ---------------------------


def _write_btc_proof(path, file_digest: bytes, height: int):
    """Serialize a minimal .ots committing ``file_digest`` to a Bitcoin block at ``height``.

    The single attestation hangs off the root timestamp, so its commitment (the value that must
    equal the block merkle root) is ``file_digest`` itself — which lets a test fake the explorer.
    """
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(file_digest)
    ts.attestations.add(BitcoinBlockHeaderAttestation(height))
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    path.write_bytes(ctx.getbytes())


def test_verify_explorer_complete(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, file_digest, height=811111)

    # The explorer reports the merkle root the attestation commits to (== file_digest) and a time.
    def fake_fetch(api, height, timeout):
        assert height == 811111
        return file_digest, 1707935720  # 2024-02-14 18:35 UTC
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)  # explorer is the default backend
    assert result.verified is True
    assert result.state == "complete"
    assert result.block_height == 811111
    assert result.existed_by == "2024-02-14 18:35 UTC"


def test_verify_explorer_merkle_mismatch_not_verified(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex(digest_hex), height=811111)

    def fake_fetch(api, height, timeout):
        return bytes.fromhex("cd" * 32), 1707935720  # wrong merkle root → altered file/proof
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.verified is False
    assert "merkle root" in result.message


def test_verify_explorer_digest_mismatch_not_verified(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex("ab" * 32), height=811111)

    # The file now hashes to something else → the proof no longer covers it; never hits the network.
    def boom(*a, **k):
        raise AssertionError("explorer must not be queried on a digest mismatch")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", boom)

    result = ots.verify(proof, "cd" * 32)
    assert result.verified is False
    assert "does not match" in result.message


def test_verify_explorer_unreachable_is_not_verified(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex(digest_hex), height=811111)

    def fake_fetch(api, height, timeout):
        raise ots.OtsError("block explorer request failed")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.verified is False  # an unreachable explorer must never read as verified
    assert result.state == "complete"


# --- upgrade() ------------------------------------------------------------------------------


def test_upgrade_completes_and_removes_bak(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")
    bak = tmp_path / "x.ots.bak"
    bak.write_bytes(b"old")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 0, "Success! Timestamp complete", ""
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    assert ots.upgrade(proof) is True
    assert not bak.exists()


def test_upgrade_pending_stays_incomplete(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_PENDING, ""
        return 1, "", "Pending confirmation in Bitcoin blockchain"
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    assert ots.upgrade(proof) is False  # no raise


# --- Scanner integration --------------------------------------------------------------------


async def test_scanner_perfile_marks_pending_and_stamps(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import scan_collection

    root = cairn_env / "perfile"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    # Mock stamp: write the .ots wherever the (symlinked) input lives, like the real CLI.
    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        link = Path(args[-1])
        link.with_name(link.name + ".ots").write_bytes(b"proof")
        return 0, "", ""
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 2
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        # All files stamped at end of scan → incomplete.
        assert {f.ots_state for f in files} == {"incomplete"}
        for f in files:
            assert f.ots_path is not None
            assert f.ots_stamped_at is not None
            # Proof lives under <proof_store>/<collection_id>/, NOT under the collection root.
            assert str(cairn_env / "proofs" / str(cid)) in f.ots_path
            assert str(root) not in f.ots_path
            assert Path(f.ots_path).exists()
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 2

    # Nothing was written under the collection root.
    assert sorted(p.name for p in root.iterdir()) == ["a.txt", "b.txt"]

    # Modify a.txt → re-queued and re-stamped.
    (root / "a.txt").write_text("ALPHA changed and longer")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 1
        a = await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == "a.txt")
        )
        assert a.ots_state == "incomplete"


async def test_moved_file_reuses_proof_and_is_not_restamped(cairn_env, monkeypatch):
    """A move in a perfile collection carries the existing proof to the new path: nothing is
    re-stamped, and `ots verify` still passes against the carried-forward `.ots`."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import scan_collection

    root = cairn_env / "movestamp"
    root.mkdir()
    (root / "photo.jpg").write_bytes(b"jpeg-content-bytes")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    calls: list = []

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        calls.append(list(args))
        if args[0] == "stamp":
            link = Path(args[-1])
            link.with_name(link.name + ".ots").write_bytes(b"proof")
            return 0, "", ""
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 0, "", VERIFY_SUCCESS  # verify

    monkeypatch.setattr(ots, "_run_ots", fake_run)

    # Scan 1: stamp the new file.
    async with sm() as s:
        assert (await scan_collection(s, await s.get(Collection, cid))).added == 1
        row = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        orig_ots_path, orig_state = row.ots_path, row.ots_state
        orig_stamped_at, orig_sha = row.ots_stamped_at, row.sha256
        assert orig_ots_path is not None and orig_state == "incomplete"
        assert Path(orig_ots_path).exists()

    # Move the stamped file; no `stamp` call should fire (nothing is pending after reconciliation).
    calls.clear()
    (root / "archive").mkdir()
    (root / "photo.jpg").rename(root / "archive" / "photo.jpg")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.moved == 1 and summ.added == 0 and summ.missing == 0
        run = await s.scalar(
            select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()).limit(1)
        )
        assert run.stamped == 0 and run.moved == 1

        survivor = await s.scalar(
            select(FileEntry).where(
                FileEntry.collection_id == cid, FileEntry.relpath == "archive/photo.jpg"
            )
        )
        # Proof carried forward verbatim — same path/state/timestamp, never re-queued.
        assert survivor.ots_path == orig_ots_path
        assert survivor.ots_state == orig_state == "incomplete"
        assert survivor.ots_stamped_at == orig_stamped_at
        assert survivor.status == "ok"
        carried_sha = survivor.sha256

    # No `ots stamp` was invoked by the move.
    assert not any(c and c[0] == "stamp" for c in calls)

    # The carried-forward proof still verifies against the file's (unchanged) digest.
    # (`_run_ots` is mocked here, so exercise the node/CLI backend.)
    vr = ots.verify(Path(orig_ots_path), carried_sha, backend="node")
    assert vr.verified is True
    assert carried_sha == orig_sha


async def test_scanner_none_collection_never_stamps(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import scan_collection

    root = cairn_env / "tripwire"
    root.mkdir()
    (root / "doc.txt").write_text("data")
    cid = await _make_collection(root, mode="worm", ots_mode="none")
    sm = get_sessionmaker()

    def _boom(args, timeout=ots.DEFAULT_TIMEOUT):  # pragma: no cover - must not be reached
        raise AssertionError("a 'none' collection must never invoke ots")
    monkeypatch.setattr(ots, "_run_ots", _boom)

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 1
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.ots_state for f in files} == {"none"}
        assert all(f.ots_path is None for f in files)
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 0


async def test_scanner_stamp_failure_does_not_fail_scan(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import scan_collection

    root = cairn_env / "flaky"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    # Stamp always fails (e.g. calendars unreachable) but produces no .ots → OtsError.
    monkeypatch.setattr(ots, "_run_ots", lambda args, timeout=ots.DEFAULT_TIMEOUT: (1, "", "unreachable"))

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.result == "ok"  # scan still finishes cleanly
        f = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        assert f.ots_state == "pending"  # left for retry
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 0  # nothing stamped, run recorded


# --- proofs: batched stamping, failure isolation, stamp-all scope ---------------------------


def _batch_fake(invocations: list, *, unstampable: set[str] | None = None):
    """Build a fake ``_run_ots`` for ``ots stamp`` that records calls and honors per-file failure.

    The real CLI writes ``<input>.ots`` beside each (symlinked) input. We mirror that, identifying
    inputs as the args that are actual symlinks, and skip writing a proof for any whose symlink
    target basename is in ``unstampable`` (simulating one bad/unreachable file in a batch).
    """
    from src.services import ots

    unstampable = unstampable or set()

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):  # noqa: ANN001
        invocations.append(list(args))
        for a in args:
            p = Path(a)
            if p.is_symlink():
                if Path(os.readlink(p)).name in unstampable:
                    continue
                p.with_name(p.name + ".ots").write_bytes(b"proof")
        return 0, "", ""

    return fake_run


async def test_stamp_pending_batches_into_one_call(cairn_env, monkeypatch):
    """N pending files (≤ batch size) stamp in ONE invocation, each getting its own proof."""
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "batch"
    root.mkdir()
    n = 5
    for i in range(n):
        (root / f"f{i}.txt").write_text(f"content-{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()
    settings = get_settings()

    async with sm() as s:
        for i in range(n):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=9, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))
        assert count == n
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.ots_state for f in files} == {"incomplete"}
        for f in files:
            expected = proofs.proof_path(settings, cid, f.relpath)
            assert Path(f.ots_path) == expected and expected.exists()

    # Default batch size (256) ≥ 5 ⇒ exactly one ``ots stamp`` call covering all five.
    stamp_calls = [a for a in invocations if a and a[0] == "stamp"]
    assert len(stamp_calls) == 1
    assert sum(1 for x in stamp_calls[0] if str(proofs.staging_dir(settings)) in x) == n


async def test_stamp_pending_spans_multiple_batches(cairn_env, monkeypatch):
    """More pending files than the batch size ⇒ multiple invocations, each ≤ batch size."""
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    monkeypatch.setenv("CAIRN_OTS_STAMP_BATCH_SIZE", "2")
    get_settings.cache_clear()

    root = cairn_env / "multibatch"
    root.mkdir()
    n = 5  # ceil(5 / 2) == 3 invocations
    for i in range(n):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for i in range(n):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))
        assert count == n

    stamp_calls = [a for a in invocations if a and a[0] == "stamp"]
    settings = get_settings()
    staging = str(proofs.staging_dir(settings))
    per_call_links = [sum(1 for x in call if staging in x) for call in stamp_calls]
    assert len(stamp_calls) == 3
    assert sorted(per_call_links) == [1, 2, 2]  # 2 + 2 + 1, none exceeding the batch size
    assert sum(per_call_links) == n


async def test_stamp_pending_failure_isolation(cairn_env, monkeypatch):
    """A batch member with no proof falls back individually and never drops the rest."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import scan_collection

    root = cairn_env / "isolation"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")   # the unstampable one
    (root / "c.txt").write_text("gamma")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    invocations: list = []
    # b.txt never produces a proof — neither in the batch nor in the single-file fallback.
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations, unstampable={"b.txt"}))

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.result == "ok"  # scan still completes cleanly
        files = {
            f.relpath: f
            for f in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid))
        }
        assert files["a.txt"].ots_state == "incomplete" and files["a.txt"].ots_path
        assert files["c.txt"].ots_state == "incomplete" and files["c.txt"].ots_path
        # The unstampable file is left pending (and logged) for retry; its peers kept their proofs.
        assert files["b.txt"].ots_state == "pending" and files["b.txt"].ots_path is None
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 2

    # b.txt was retried on its own → a single-file ``stamp`` call with exactly one link in addition
    # to the batch call. So at least one stamp invocation carried a lone link (the fallback).
    stamp_calls = [a for a in invocations if a and a[0] == "stamp"]
    staging = str(cairn_env / "proofs" / ".staging")
    lone = [c for c in stamp_calls if sum(1 for x in c if staging in x) == 1]
    assert lone, "expected an individual fallback stamp for the failed member"


# --- proofs/ots: an unwritable proof path (ENAMETOOLONG) is skipped, never fatal ---------------


def test_stamp_via_symlink_raises_path_error_on_overlong_name(tmp_path, monkeypatch):
    """A proof output name past the filesystem byte limit fails fast with OtsPathError — before any
    symlink or calendar round-trip — and OtsPathError is an OtsError so existing callers still catch
    it."""
    from src.services import ots

    real = tmp_path / "f.bin"
    real.write_bytes(b"data")
    long_base = "д" * 126  # 252 bytes; + ".ots" = 256 bytes > NAME_MAX
    out = tmp_path / "store" / "1" / (long_base + ".ots")
    assert len(os.fsencode(out.name)) > ots._NAME_MAX_BYTES

    def _boom(args, timeout=ots.DEFAULT_TIMEOUT):  # pragma: no cover - must not be reached
        raise AssertionError("ots must not be invoked for an unwritable-output file")

    monkeypatch.setattr(ots, "_run_ots", _boom)

    assert issubclass(ots.OtsPathError, ots.OtsError)
    with pytest.raises(ots.OtsPathError):
        ots.stamp_via_symlink(real, out, [], tmp_path / "store" / ".staging")


def test_place_proof_wraps_filesystem_enametoolong(tmp_path, monkeypatch):
    """The os.replace backstop converts a genuine filesystem ENAMETOOLONG into OtsPathError even if
    the byte pre-check would have let the name through (e.g. a smaller real NAME_MAX)."""
    from src.services import ots

    monkeypatch.setattr(ots, "_NAME_MAX_BYTES", 100_000)  # make the pre-check permissive
    staged = tmp_path / "staged.ots"
    staged.write_bytes(b"proof")
    out = tmp_path / ("д" * 130 + ".ots")  # 264 bytes → real ENAMETOOLONG on ext4

    with pytest.raises(ots.OtsPathError):
        ots._place_proof(staged, out)


def test_place_proof_non_enametoolong_oserror_is_transient(tmp_path, monkeypatch):
    """A non-ENAMETOOLONG write failure (a full or read-only proof store) is a *transient* OtsError,
    NOT a permanent OtsPathError — so the caller retries instead of dropping the proof to `none`."""
    import errno as _errno

    from src.services import ots

    staged = tmp_path / "staged.ots"
    staged.write_bytes(b"proof")
    out = tmp_path / "1" / "f.txt.ots"

    def boom_replace(src, dst):
        raise OSError(_errno.EROFS, "Read-only file system")

    monkeypatch.setattr(ots.os, "replace", boom_replace)

    with pytest.raises(ots.OtsError) as excinfo:
        ots._place_proof(staged, out)
    # Crucially NOT the permanent subclass — a transient error must stay retryable.
    assert not isinstance(excinfo.value, ots.OtsPathError)


async def test_stamp_pending_transient_write_error_stays_pending(cairn_env, monkeypatch):
    """A transient placement failure (read-only/full store) leaves files `pending` for retry — it
    must never be misread as a permanent skip and dropped to `none`."""
    import errno as _errno

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "rofs"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for i in range(3):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    # The `ots stamp` subprocess still "produces" proofs, but placing them always fails EROFS.
    monkeypatch.setattr(ots, "_run_ots", _batch_fake([]))

    def boom_replace(src, dst):
        raise OSError(_errno.EROFS, "Read-only file system")

    monkeypatch.setattr(ots.os, "replace", boom_replace)

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))
        assert count == 0  # nothing stamped
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        # Every file stays PENDING (retryable) — a transient store error is never a `none` skip.
        assert {f.ots_state for f in files} == {"pending"}


def test_stamp_batch_skips_overlong_proof_name_and_keeps_the_rest(tmp_path, monkeypatch):
    """A batch member whose proof name is too long is skipped (never symlinked or submitted); the
    other members are still stamped and the whole call completes without raising."""
    from src.services import ots

    root = tmp_path / "src"
    root.mkdir()
    store = tmp_path / "proofs"
    staging = store / ".staging"

    good = root / "a.txt"
    good.write_bytes(b"a")
    long_base = "д" * 126  # source 252 bytes (creatable); proof 256 bytes (> NAME_MAX)
    longf = root / long_base
    longf.write_bytes(b"b")
    assert len(os.fsencode(long_base + ".ots")) > ots._NAME_MAX_BYTES

    items = [
        (good, store / "1" / "a.txt.ots"),
        (longf, store / "1" / (long_base + ".ots")),
    ]
    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    results = ots.stamp_batch_via_symlink(items, [], staging)

    assert results == [True, False]
    assert items[0][1].exists()          # the good file is stamped
    # The overlong proof is never written (its name can't even be stat()'d, so list the dir instead).
    assert [p.name for p in (store / "1").iterdir()] == ["a.txt.ots"]
    assert not any(staging.iterdir())    # links + stray .ots cleaned up
    # The overlong member was never sent to the calendar: exactly one link in the stamp invocation.
    stamp_calls = [a for a in invocations if a and a[0] == "stamp"]
    assert len(stamp_calls) == 1
    assert sum(1 for x in stamp_calls[0] if str(staging) in x) == 1


async def test_stamp_pending_skips_overlong_name_and_stamps_rest(cairn_env, monkeypatch):
    """The real crash-loop regression: a pending file whose `.ots` name exceeds the filesystem byte
    limit is skipped-and-counted (dropped to `ots_state='none'`) instead of aborting the batch, and
    the remaining files are stamped normally."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "toolong"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "c.txt").write_text("gamma")
    long_base = "д" * 126  # source 252 bytes (≤ NAME_MAX, creatable); proof 256 bytes (> NAME_MAX)
    assert len(os.fsencode(long_base)) <= ots._NAME_MAX_BYTES
    assert len(os.fsencode(long_base + ".ots")) > ots._NAME_MAX_BYTES
    (root / long_base).write_text("too-long")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for rp, sha in [("a.txt", "1"), ("c.txt", "3"), (long_base, "2")]:
            s.add(FileEntry(
                collection_id=cid, relpath=rp, size=5, sha256=sha * 64,
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))
        assert count == 2  # only the two normal files
        files = {
            f.relpath: f
            for f in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid))
        }
        assert files["a.txt"].ots_state == "incomplete" and files["a.txt"].ots_path
        assert files["c.txt"].ots_state == "incomplete" and files["c.txt"].ots_path
        # The overlong-proof file is skipped and dropped to `none` (no proof, no crash), so a normal
        # scan will not re-queue and re-fail it every pass.
        assert files[long_base].ots_state == "none"
        assert files[long_base].ots_path is None


# --- regression: a filesystem failure while STAGING never escapes as a raw OSError -------------


async def test_stamp_pending_symlink_failure_leaves_files_pending(cairn_env, monkeypatch):
    """An un-writable staging dir (symlink creation refused) must not escape as a raw OSError.

    Regression: `link.symlink_to(...)` was unguarded in both the batch and the single-file path, so a
    `PermissionError` blew past `stamp_pending`'s per-file handling and aborted the whole pass —
    including every later chunk. Now it is classified as a *transient* OtsError: nothing raises,
    nothing is dropped to `none`, every file stays `pending` for the next pass.
    """
    import errno as _errno

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "nolink"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for i in range(3):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    def boom_symlink(*args, **kwargs):  # noqa: ANN002, ANN003
        raise PermissionError(_errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "symlink", boom_symlink)

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))  # must not raise
        assert count == 0
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        # Transient ⇒ retryable: still `pending`, no proof pointer, no phantom stamp time.
        assert {f.ots_state for f in files} == {"pending"}
        assert all(f.ots_path is None and f.ots_stamped_at is None for f in files)

    # Nothing was ever submitted to a calendar — the failure happened before the `ots` call.
    assert not [a for a in invocations if a and a[0] == "stamp"]


async def test_stamp_pending_staging_dir_failure_does_not_abort_later_chunks(cairn_env, monkeypatch):
    """A batch-level OtsError (the shared staging dir cannot be created) degrades to the per-file
    fallback and still walks EVERY chunk — it never propagates out of `stamp_pending`."""
    import errno as _errno

    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    monkeypatch.setenv("CAIRN_OTS_STAMP_BATCH_SIZE", "2")
    get_settings.cache_clear()

    root = cairn_env / "nostaging"
    root.mkdir()
    n = 5  # ceil(5 / 2) == 3 chunks
    for i in range(n):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for i in range(n):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    assert not proofs.staging_dir(get_settings()).exists()  # so mkdir really is attempted

    # Count the single-file fallbacks: one per file proves all three chunks were processed.
    attempts: list[str] = []
    original_single = ots.stamp_via_symlink

    def counting_single(real, out, calendars, staging, **kwargs):  # noqa: ANN001, ANN003
        attempts.append(Path(real).name)
        return original_single(real, out, calendars, staging, **kwargs)

    monkeypatch.setattr(ots, "stamp_via_symlink", counting_single)
    monkeypatch.setattr(ots, "_run_ots", _batch_fake([]))

    def boom_mkdir(*args, **kwargs):  # noqa: ANN002, ANN003
        raise PermissionError(_errno.EACCES, "Permission denied")

    monkeypatch.setattr(os, "mkdir", boom_mkdir)

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))  # must not raise
        assert count == 0
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        assert {f.ots_state for f in files} == {"pending"}  # transient — never `none`

    assert sorted(attempts) == [f"f{i}.txt" for i in range(n)]


async def test_permanent_skip_clears_stamp_time_and_keeps_status(cairn_env, monkeypatch):
    """The permanent `none` skip must leave NO trust metadata behind — and must not re-baseline.

    A file stamped under an old name, then renamed to an un-writable (overlong) one and modified,
    kept its previous `ots_stamped_at` while losing its proof: the panel then showed a stamp date
    for content nothing attests. `none` must mean nothing is claimed. The monitored `status` is a
    scanner concern and is left exactly as found.
    """
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "stale-stamp"
    root.mkdir()
    long_base = "д" * 126  # source 252 bytes (creatable); proof 256 bytes (> NAME_MAX)
    (root / long_base).write_text("renamed-then-modified")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    stamped_before = _utcnow() - timedelta(days=30)
    async with sm() as s:
        s.add(FileEntry(
            collection_id=cid, relpath=long_base, size=21, sha256="7" * 64,
            status="modified", first_seen=_utcnow(), ots_state="pending",
            ots_path="/proofs/1/old-name.txt.ots", ots_stamped_at=stamped_before,
        ))
        await s.commit()

    monkeypatch.setattr(ots, "_run_ots", _batch_fake([]))

    async with sm() as s:
        assert await proofs.stamp_pending(s, await s.get(Collection, cid)) == 0
        entry = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        assert entry.ots_state == "none"
        assert entry.ots_path is None
        assert entry.ots_stamped_at is None  # no stamp time without a proof to back it
        assert entry.status == "modified"    # a notarization skip never re-baselines the file


def test_proof_output_writable_rejects_overlong_parent_component(tmp_path):
    """The pre-check covers every component Cairn CREATES below the store root, not just the final
    `.ots` name — an overlong relpath directory is just as un-writable (mkdir refuses it with
    ENAMETOOLONG)."""
    from src.services import ots

    store = tmp_path / "proofs"
    long_dir = "д" * 130  # 260 bytes > NAME_MAX
    assert len(os.fsencode(long_dir)) > ots._NAME_MAX_BYTES

    assert ots._proof_output_writable(store / "1" / "sub" / "a.txt.ots", below=store)
    # The overlong component is one Cairn creates (it comes from the file's relpath) → permanent.
    assert not ots._proof_output_writable(store / "1" / long_dir / "a.txt.ots", below=store)


def test_proof_output_writable_ignores_store_root_components(tmp_path):
    """A store root whose OWN components are overlong must not make every descendant proof look
    permanently unwritable.

    Regression (mass false negative): the pre-check measured every component of the absolute output
    path, so a proof store living under a directory the filesystem already accepted at >255 bytes
    would fail the check for every file — silently dropping a whole collection to
    `ots_state='none'`. Only `<collection_id>/<relpath>.ots` is ours to validate.
    """
    from src.services import ots

    long_dir = "д" * 130  # 260 bytes > NAME_MAX, but it is the OPERATOR's path, not ours
    store = tmp_path / long_dir / "proofs"
    assert len(os.fsencode(long_dir)) > ots._NAME_MAX_BYTES

    assert ots._proof_output_writable(store / "1" / "a.txt.ots", below=store)
    assert ots._proof_output_writable(store / "1" / "sub" / "a.txt.ots", below=store)
    # …while an overlong component below the root is still rejected.
    assert not ots._proof_output_writable(store / "1" / (long_dir + ".ots"), below=store)


def test_stamp_batch_skips_overlong_parent_dir_before_any_symlink(tmp_path, monkeypatch):
    """A member whose proof PARENT directory is overlong (its own name short) is skipped up front —
    no staging symlink, no calendar submission — instead of burning a batch round-trip plus a
    single-file retry before `_place_proof` classifies it."""
    from src.services import ots

    root = tmp_path / "src"
    root.mkdir()
    store = tmp_path / "proofs"
    staging = store / ".staging"

    good = root / "a.txt"
    good.write_bytes(b"a")
    deep = root / "b.txt"
    deep.write_bytes(b"b")
    long_dir = "д" * 130  # 260 bytes > NAME_MAX, though "b.txt.ots" itself is short

    items = [
        (good, store / "1" / "a.txt.ots"),
        (deep, store / "1" / long_dir / "b.txt.ots"),
    ]

    symlinked: list[str] = []
    real_symlink = os.symlink

    def counting_symlink(target, link, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        symlinked.append(str(link))
        return real_symlink(target, link, *args, **kwargs)

    monkeypatch.setattr(os, "symlink", counting_symlink)
    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    results = ots.stamp_batch_via_symlink(items, [], staging, store_root=store)

    assert results == [True, False]
    assert len(symlinked) == 1  # the overlong-parent member was never staged
    assert [p.name for p in (store / "1").iterdir()] == ["a.txt.ots"]
    stamp_calls = [a for a in invocations if a and a[0] == "stamp"]
    assert len(stamp_calls) == 1
    assert sum(1 for x in stamp_calls[0] if str(staging) in x) == 1
    assert not any(staging.iterdir())  # links + stray .ots cleaned up


def test_staging_symlink_enametoolong_is_transient(tmp_path, monkeypatch):
    """A staging-side ENAMETOOLONG is TRANSIENT (plain OtsError), never a permanent OtsPathError.

    The overlong operand there is the *staging* pathname — `<store>/.staging/<uuid>` on a store path
    already near PATH_MAX — which is a property of the deployment, not of this file. Classifying it
    permanent dropped the file to `ots_state='none'` and abandoned its notarization forever.
    Permanence is decided only by the output-path pre-check and by `_place_proof`.
    """
    import errno as _errno

    from src.services import ots

    real = tmp_path / "f.bin"
    real.write_bytes(b"data")
    staging = tmp_path / "store" / ".staging"

    def boom_symlink(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(_errno.ENAMETOOLONG, "File name too long")

    # The cleanup unlink of that same overlong staging path used to raise a RAW OSError out of
    # `finally`, replacing the classified exception and aborting the caller's whole stamp pass.
    def boom_unlink(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(_errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(os, "symlink", boom_symlink)
    monkeypatch.setattr(os, "unlink", boom_unlink)

    with pytest.raises(ots.OtsError) as excinfo:
        ots.stamp_via_symlink(real, tmp_path / "store" / "1" / "f.bin.ots", [], staging,
                              store_root=tmp_path / "store")
    assert not isinstance(excinfo.value, ots.OtsPathError)


async def test_stamp_pending_staging_link_enametoolong_stays_pending(cairn_env, monkeypatch):
    """End-to-end: a staging-symlink ENAMETOOLONG leaves every member `pending` (never `none`), does
    not abort the pass, and lets no raw OSError escape — including through the cleanup unlink."""
    import errno as _errno

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "longlink"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        for i in range(3):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError(_errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(os, "symlink", boom)
    monkeypatch.setattr(os, "unlink", boom)  # cleaning an overlong staging path fails the same way

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))  # must not raise
        assert count == 0
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))
        # Transient ⇒ retryable: still `pending`, and no trust metadata invented or discarded.
        assert {f.ots_state for f in files} == {"pending"}
        assert all(f.ots_path is None and f.ots_stamped_at is None for f in files)

    assert not [a for a in invocations if a and a[0] == "stamp"]  # nothing reached a calendar


def test_stamp_ignores_overlong_components_of_the_store_root(tmp_path, monkeypatch):
    """A store root carrying an over-limit component still stamps normally: only the components
    Cairn creates below it (`<collection_id>/<relpath>.ots`) are pre-checked.

    Regression (mass false negative): the all-components pre-check judged the operator's store path
    too, so every proof under such a root read "permanently unwritable" and the whole collection was
    silently dropped to `ots_state='none'`. The limit is shrunk here rather than creating a real
    >255-byte directory (ext4 refuses one), which is the same shape of failure.
    """
    from src.services import ots

    monkeypatch.setattr(ots, "_NAME_MAX_BYTES", 12)
    store = tmp_path / ("s" * 40) / "proofs"  # a store-root component past the (shrunk) limit
    staging = store / ".staging"
    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_bytes(b"x")
    assert len(os.fsencode(store.parent.name)) > ots._NAME_MAX_BYTES

    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))

    results = ots.stamp_batch_via_symlink(
        [(root / "a.txt", store / "1" / "a.txt.ots")], [], staging, store_root=store
    )
    assert results == [True]
    assert (store / "1" / "a.txt.ots").exists()

    # …and the single-file path agrees (it is the fallback that decides permanent vs. transient).
    out = ots.stamp_via_symlink(
        root / "b.txt", store / "1" / "b.txt.ots", [], staging, store_root=store
    )
    assert out.exists()
    assert len([a for a in invocations if a and a[0] == "stamp"]) == 2


async def test_mark_unstamped_pending_scopes_to_none_and_present(cairn_env, monkeypatch):
    """Backfill marks only ots_state='none' non-missing files; never re-stamps existing proofs."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "backfill"
    root.mkdir()
    (root / "n1.txt").write_text("one")
    (root / "n2.txt").write_text("two")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    sentinel = "/proofs/sentinel.ots"
    async with sm() as s:
        s.add_all([
            # eligible: present + unstamped
            FileEntry(collection_id=cid, relpath="n1.txt", size=3, sha256="1" * 64,
                      status="ok", first_seen=_utcnow(), ots_state="none"),
            FileEntry(collection_id=cid, relpath="n2.txt", size=3, sha256="2" * 64,
                      status="new", first_seen=_utcnow(), ots_state="none"),
            # ineligible: missing (cannot be stamped)
            FileEntry(collection_id=cid, relpath="gone.txt", size=3, sha256="3" * 64,
                      status="missing", first_seen=_utcnow(), ots_state="none"),
            # ineligible: already has a proof — must NOT be re-stamped
            FileEntry(collection_id=cid, relpath="done.txt", size=3, sha256="4" * 64,
                      status="ok", first_seen=_utcnow(), ots_state="incomplete",
                      ots_path=sentinel, ots_stamped_at=_utcnow()),
        ])
        await s.commit()

    async with sm() as s:
        marked = await proofs.mark_unstamped_pending(s, await s.get(Collection, cid))
        assert marked == 2  # only n1.txt + n2.txt

    async with sm() as s:
        states = {
            f.relpath: (f.ots_state, f.ots_path)
            for f in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid))
        }
        assert states["n1.txt"][0] == "pending" and states["n2.txt"][0] == "pending"
        assert states["gone.txt"][0] == "none"               # missing left alone
        assert states["done.txt"] == ("incomplete", sentinel)  # existing proof untouched

    # Now stamp the queued backfill: only the two newly-queued files get proofs.
    invocations: list = []
    monkeypatch.setattr(ots, "_run_ots", _batch_fake(invocations))
    async with sm() as s:
        stamped = await proofs.stamp_pending(s, await s.get(Collection, cid))
        assert stamped == 2
        done = await s.scalar(
            select(FileEntry).where(FileEntry.collection_id == cid, FileEntry.relpath == "done.txt")
        )
        # The pre-existing proof was never touched (still the sentinel, not a real store path).
        assert done.ots_state == "incomplete" and done.ots_path == sentinel


async def test_scan_with_no_changes_leaves_none_baseline(cairn_env, monkeypatch):
    """A perfile collection whose baseline is ots_state='none' is NOT auto-stamped by a no-op scan."""
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import ots
    from src.services.scanner import _utcnow, scan_collection

    root = cairn_env / "baseline"
    root.mkdir()
    real = root / "archive.bin"
    real.write_bytes(b"pre-existing baseline bytes")
    st = real.stat()
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    # Seed a tracked baseline file matching the on-disk size/mtime/sha so the scan fast-paths it as
    # unchanged (an imported, deliberately-unstamped baseline). ots must never be invoked.
    from src.services.scanner import sha256_file
    async with sm() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="archive.bin", size=st.st_size, mtime=st.st_mtime,
            sha256=sha256_file(real), status="ok", first_seen=_utcnow(), last_checked=_utcnow(),
            ots_state="none",
        ))
        await s.commit()

    def _boom(args, timeout=ots.DEFAULT_TIMEOUT):  # pragma: no cover - must not be reached
        raise AssertionError("a no-op scan must not stamp the unstamped baseline")
    monkeypatch.setattr(ots, "_run_ots", _boom)

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 0 and summ.modified == 0
        f = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        assert f.ots_state == "none" and f.ots_path is None  # baseline untouched
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 0


# --- proofs: stamp_pending path discipline, export, staleness -------------------------------


async def test_stamp_pending_writes_under_proof_store(cairn_env, monkeypatch):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs

    root = cairn_env / "ps"
    root.mkdir()
    (root / "f.txt").write_text("data")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()
    settings = get_settings()

    # Pre-create a pending file row (skip the scanner) to test stamp_pending in isolation.
    async with sm() as s:
        from src.services.scanner import _utcnow

        s.add(
            FileEntry(
                collection_id=cid, relpath="f.txt", size=4, sha256="x" * 64,
                status="new", first_seen=_utcnow(), ots_state="pending",
            )
        )
        await s.commit()

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        link = Path(args[-1])
        link.with_name(link.name + ".ots").write_bytes(b"proof")
        return 0, "", ""
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    async with sm() as s:
        collection = await s.get(Collection, cid)
        count = await proofs.stamp_pending(s, collection)
        assert count == 1
        f = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        expected = proofs.proof_path(settings, cid, "f.txt")
        assert Path(f.ots_path) == expected
        assert expected.exists()
        assert str(root) not in f.ots_path  # never under the collection root


async def test_export_bundle_writes_file_and_proof(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "exp"
    root.mkdir()
    (root / "report.pdf").write_bytes(b"PDF-BYTES")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    ots_file = cairn_env / "proofs" / str(cid) / "report.pdf.ots"
    ots_file.parent.mkdir(parents=True, exist_ok=True)
    ots_file.write_bytes(b"OTS-PROOF")

    async with sm() as s:
        entry = FileEntry(
            collection_id=cid, relpath="report.pdf", size=9, sha256="a" * 64,
            status="new", first_seen=_utcnow(), ots_state="incomplete",
            ots_path=str(ots_file),
        )
        s.add(entry)
        await s.commit()
        await s.refresh(entry)

        dest = cairn_env / "export-out"
        result = proofs.export_bundle(entry, dest, root)
        assert result == dest / "report.pdf"
        assert (dest / "report.pdf").read_bytes() == b"PDF-BYTES"
        assert (dest / "report.pdf.ots").read_bytes() == b"OTS-PROOF"


async def test_export_bundle_errors_without_proof(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "exp2"
    root.mkdir()
    (root / "x.txt").write_text("hi")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        entry = FileEntry(
            collection_id=cid, relpath="x.txt", size=2, sha256="b" * 64,
            status="new", first_seen=_utcnow(), ots_state="none",
        )
        s.add(entry)
        await s.commit()
        await s.refresh(entry)
        with pytest.raises(FileNotFoundError):
            proofs.export_bundle(entry, cairn_env / "out", root)


async def test_stale_incomplete_honors_threshold(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry
    from src.services import proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "stale"
    root.mkdir()
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()
    now = _utcnow()

    async with sm() as s:
        # Old incomplete (10 days) → stale; recent incomplete (1 day) → fresh; complete → ignored.
        s.add_all([
            FileEntry(
                collection_id=cid, relpath="old.txt", size=1, status="ok", first_seen=now,
                ots_state="incomplete", ots_stamped_at=now - timedelta(days=10),
            ),
            FileEntry(
                collection_id=cid, relpath="fresh.txt", size=1, status="ok", first_seen=now,
                ots_state="incomplete", ots_stamped_at=now - timedelta(days=1),
            ),
            FileEntry(
                collection_id=cid, relpath="done.txt", size=1, status="ok", first_seen=now,
                ots_state="complete", ots_stamped_at=now - timedelta(days=30),
            ),
        ])
        await s.commit()

    async with sm() as s:
        stale = await proofs.stale_incomplete(s, days=7)
        assert [f.relpath for f in stale] == ["old.txt"]


# --- proofs.upgrade_incomplete(): the incomplete→complete DB transition ----------------------


async def test_upgrade_incomplete_transitions_file_to_complete(cairn_env, monkeypatch):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "up"
    root.mkdir()
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    settings = get_settings()
    sm = get_sessionmaker()

    # A stored incomplete proof on disk.
    ots_path = proofs.proof_path(settings, cid, "f.txt")
    ots_path.parent.mkdir(parents=True, exist_ok=True)
    ots_path.write_bytes(b"proof")
    async with sm() as s:
        s.add(
            FileEntry(
                collection_id=cid, relpath="f.txt", size=1, sha256="c" * 64, status="ok",
                first_seen=_utcnow(), ots_state="incomplete", ots_path=str(ots_path),
                ots_stamped_at=_utcnow(),
            )
        )
        await s.commit()

    # Bitcoin has now confirmed: upgrade succeeds and info reports complete.
    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 0, "Success! Timestamp complete", ""
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    async with sm() as s:
        result = await proofs.upgrade_incomplete(s, await s.get(Collection, cid))
        assert result == {"upgraded": 1, "still_incomplete": 0}
        f = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        assert f.ots_state == "complete"


async def test_upgrade_incomplete_leaves_unconfirmed(cairn_env, monkeypatch):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "up2"
    root.mkdir()
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    settings = get_settings()
    sm = get_sessionmaker()

    ots_path = proofs.proof_path(settings, cid, "g.txt")
    ots_path.parent.mkdir(parents=True, exist_ok=True)
    ots_path.write_bytes(b"proof")
    async with sm() as s:
        s.add(
            FileEntry(
                collection_id=cid, relpath="g.txt", size=1, sha256="d" * 64, status="ok",
                first_seen=_utcnow(), ots_state="incomplete", ots_path=str(ots_path),
                ots_stamped_at=_utcnow(),
            )
        )
        await s.commit()

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_PENDING, ""
        return 1, "", "Pending confirmation in Bitcoin blockchain"
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    async with sm() as s:
        result = await proofs.upgrade_incomplete(s, await s.get(Collection, cid))
        assert result == {"upgraded": 0, "still_incomplete": 1}
        f = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        assert f.ots_state == "incomplete"


# --- verify(): digest mismatch against a COMPLETE proof -------------------------------------


def test_verify_complete_proof_digest_mismatch(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    # Proof is complete, but the supplied digest does not match it.
    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 1, "", "Error! Expected digest ... but got a different file\n"
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "0" * 64)
    assert result.verified is False  # no success line ⇒ not verified, even for a complete proof


# --- live network smoke (skipped unless CAIRN_OTS_LIVE=1; documents task 5.2) ----------------


@pytest.mark.skipif(
    os.environ.get("CAIRN_OTS_LIVE") != "1",
    reason="live OTS network test; set CAIRN_OTS_LIVE=1 to run",
)
def test_live_stamp_smoke(tmp_path):
    from src.services import ots

    real = tmp_path / "real.txt"
    real.write_text("cairn live stamp smoke")
    out = tmp_path / "store" / "1" / "real.txt.ots"
    staging = tmp_path / "store" / ".staging"

    result = ots.stamp_via_symlink(real, out, [], staging)  # [] → ots default calendars
    assert result == out and out.exists()
    assert ots.info(out).state in ("incomplete", "complete")
    assert not any(staging.iterdir())  # staging cleaned


@pytest.mark.skipif(
    os.environ.get("CAIRN_OTS_LIVE") != "1",
    reason="live OTS network test; set CAIRN_OTS_LIVE=1 to run",
)
def test_live_batch_stamp_smoke(tmp_path):
    """One real ``ots stamp`` over 3 inputs yields 3 independent, individually-verifiable proofs."""
    import hashlib

    from src.services import ots

    staging = tmp_path / "store" / ".staging"
    items = []
    digests = []
    for i in range(3):
        real = tmp_path / f"real{i}.txt"
        data = f"cairn live batch stamp smoke {i}".encode()
        real.write_bytes(data)
        digests.append(hashlib.sha256(data).hexdigest())
        items.append((real, tmp_path / "store" / "1" / f"real{i}.txt.ots"))

    results = ots.stamp_batch_via_symlink(items, [], staging)  # [] → ots default calendars
    assert results == [True, True, True]
    for (_real, out), digest in zip(items, digests):
        assert out.exists()
        # Each proof is independent: it verifies against its OWN file's digest.
        info = ots.info(out)
        assert info.state in ("incomplete", "complete")
        vr = ots.verify(out, digest)
        assert vr.state in ("incomplete", "complete")
    assert not any(staging.iterdir())  # links + stray .ots cleaned up


# --- event-loop offloading: OTS subprocess work must run on a worker thread ------------------


async def test_upgrade_incomplete_runs_off_the_event_loop(cairn_env, monkeypatch):
    """`ots upgrade` must run on a worker thread, not the event loop, so the panel stays live."""
    import threading

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "offload-up"
    root.mkdir()
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    settings = __import__("src.config", fromlist=["get_settings"]).get_settings()
    sm = get_sessionmaker()

    ots_path = proofs.proof_path(settings, cid, "f.txt")
    ots_path.parent.mkdir(parents=True, exist_ok=True)
    ots_path.write_bytes(b"proof")
    async with sm() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="f.txt", size=1, sha256="c" * 64, status="ok",
            first_seen=_utcnow(), ots_state="incomplete", ots_path=str(ots_path),
            ots_stamped_at=_utcnow(),
        ))
        await s.commit()

    seen_threads: list = []

    def fake_upgrade(path, timeout=ots.DEFAULT_TIMEOUT):
        seen_threads.append(threading.current_thread())
        return True  # Bitcoin confirmed

    monkeypatch.setattr(ots, "upgrade", fake_upgrade)

    async with sm() as s:
        result = await proofs.upgrade_incomplete(s, await s.get(Collection, cid))

    assert result == {"upgraded": 1, "still_incomplete": 0}  # functional path intact
    assert seen_threads, "ots.upgrade was never called"
    assert all(t is not threading.main_thread() for t in seen_threads), (
        "ots.upgrade ran on the event-loop thread — it must be offloaded via asyncio.to_thread"
    )


async def test_stamp_pending_runs_off_the_event_loop(cairn_env, monkeypatch):
    """The batched stamp subprocess must run on a worker thread, not the event loop."""
    import threading

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry
    from src.services import ots, proofs
    from src.services.scanner import _utcnow

    root = cairn_env / "offload-stamp"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text(f"c{i}")
    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()
    async with sm() as s:
        for i in range(3):
            s.add(FileEntry(
                collection_id=cid, relpath=f"f{i}.txt", size=2, sha256=f"{i:064d}",
                status="new", first_seen=_utcnow(), ots_state="pending",
            ))
        await s.commit()

    seen_threads: list = []

    def fake_batch(pairs, calendars, staging, **kwargs):  # noqa: ANN003
        seen_threads.append(threading.current_thread())
        return [True] * len(pairs)  # every file stamped

    monkeypatch.setattr(ots, "stamp_batch_via_symlink", fake_batch)

    async with sm() as s:
        count = await proofs.stamp_pending(s, await s.get(Collection, cid))
        files = list(await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))

    assert count == 3
    assert {f.ots_state for f in files} == {"incomplete"}  # functional path intact
    assert seen_threads, "stamp_batch_via_symlink was never called"
    assert all(t is not threading.main_thread() for t in seen_threads), (
        "stamping ran on the event-loop thread — it must be offloaded via asyncio.to_thread"
    )


# --- verify(): the typed outcome flags (fix-ux-audit-sprint1, design D1/D2) -----------------
#
# `VerifyResult` must say *why* verification did not succeed, and whom that blames. The panel and
# `cairn verify` both branch on these flags, so a wrong one is a false alarm (or a false
# reassurance) on the product's core signal.


def _write_btc_proof_multi(path, file_digest: bytes, heights):
    """Like ``_write_btc_proof`` but with several Bitcoin attestations on the same root timestamp.

    Every attestation therefore commits to ``file_digest``, so a stubbed explorer can make one
    height match its block's merkle root and another not.
    """
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(file_digest)
    for height in heights:
        ts.attestations.add(BitcoinBlockHeaderAttestation(height))
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    path.write_bytes(ctx.getbytes())


def test_explorer_digest_mismatch_sets_only_digest_mismatch(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex("ab" * 32), height=811111)

    def boom(*a, **k):
        raise AssertionError("explorer must not be queried on a digest mismatch")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", boom)

    result = ots.verify(proof, "cd" * 32)
    assert result.verified is False
    # The neutral "the live digest and the proof's committed digest disagree" signal. Which of the
    # two moved is NOT established here — the callers decide that from the recorded baseline.
    assert result.digest_mismatch is True
    assert result.proof_mismatch is False   # ...and it says nothing about the attestations
    assert result.transport_error is None
    assert result.inconclusive is False


def test_explorer_merkle_mismatch_sets_proof_mismatch_not_digest_mismatch(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex(digest_hex), height=811111)

    def fake_fetch(api, height, timeout):
        return bytes.fromhex("cd" * 32), 1707935720  # not the commitment → the proof is wrong
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.verified is False
    # The live digest matched, so this blames the proof / the explorer's block data — never the
    # file. Copy derived from `digest_mismatch` here would be a false "your file changed" alarm.
    assert result.proof_mismatch is True
    assert result.digest_mismatch is False
    assert result.transport_error is None


def test_explorer_verified_proof_has_every_flag_clear(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, file_digest, height=811111)
    monkeypatch.setattr(
        ots, "_fetch_block_merkleroot", lambda api, h, t: (file_digest, 1707935720)
    )

    result = ots.verify(proof, digest_hex)
    assert result.verified is True
    assert (result.digest_mismatch, result.proof_mismatch, result.inconclusive) == (
        False, False, False,
    )
    assert result.transport_error is None


def test_explorer_one_good_attestation_outranks_a_mismatched_sibling(tmp_path, monkeypatch):
    """OTS verification is existential: one attestation confirmed against its real block IS proof.

    A bad sibling must stay diagnostic detail in ``message`` — rendering it as a verdict puts a red
    "this proof does not check out" over a genuinely anchored proof.
    """
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof_multi(proof, file_digest, heights=(811111, 822222))

    def fake_fetch(api, height, timeout):
        if height == 811111:
            return file_digest, 1707935720          # matches its block
        return bytes.fromhex("cd" * 32), 1717935720  # sibling does not
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.verified is True
    assert result.proof_mismatch is False
    assert result.block_height == 811111
    assert "sibling" in result.message  # kept as detail, never as the verdict
    assert result.transport_error is None


def test_explorer_all_fetches_failing_sets_transport_error_and_is_not_verified(
    tmp_path, monkeypatch
):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof_multi(proof, bytes.fromhex(digest_hex), heights=(811111, 822222))

    def fake_fetch(api, height, timeout):
        raise ots.OtsError(f"explorer request failed at {height}")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.verified is False
    assert result.transport_error is not None
    assert ots.failed_lookup_count(result) == 2
    assert result.transport_failures == 2  # carried structurally, not re-split out of the text
    assert result.digest_mismatch is False and result.proof_mismatch is False


def test_explorer_verified_result_still_carries_a_swallowed_fetch_error(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof_multi(proof, file_digest, heights=(811111, 822222))

    def fake_fetch(api, height, timeout):
        if height == 811111:
            return file_digest, 1707935720
        raise ots.OtsError("explorer request failed at 822222")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    # A transport failure never downgrades a verified result, and is never dropped either: the
    # operator is told the verdict rests on the attestations that could be reached.
    assert result.verified is True
    assert result.transport_error == "explorer request failed at 822222"
    assert ots.failed_lookup_count(result) == 1
    assert result.transport_failures == 1


def test_explorer_proof_mismatch_still_carries_a_swallowed_fetch_error(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof_multi(proof, bytes.fromhex(digest_hex), heights=(811111, 822222))

    def fake_fetch(api, height, timeout):
        if height == 811111:
            return bytes.fromhex("cd" * 32), 1707935720  # the one we could fetch, and it is wrong
        raise ots.OtsError("explorer request failed at 822222")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", fake_fetch)

    result = ots.verify(proof, digest_hex)
    assert result.proof_mismatch is True
    # "this proof is bad" vs "this proof is bad as far as the half of it I could see".
    assert result.transport_error == "explorer request failed at 822222"


def test_node_backend_non_success_exit_is_inconclusive_not_pending(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_PENDING, ""
        return 1, "", VERIFY_PENDING
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "deadbeef", backend="node")
    assert result.verified is False
    assert result.inconclusive is True
    # `ots verify -d` reports an unanchored proof, a changed file and a dead node identically, so
    # this backend must never guess a mismatch from the CLI's wording.
    assert result.digest_mismatch is False and result.proof_mismatch is False


def test_node_backend_unrunnable_binary_is_a_transport_error(tmp_path, monkeypatch):
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        raise ots.OtsError("ots binary not found")
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "ab" * 32, backend="node")
    assert result.verified is False
    assert result.transport_error == "ots binary not found"
    assert result.state == "none"       # never the proof's own state (that read as "pending")
    assert result.inconclusive is False


def test_failed_lookup_count_reads_the_structural_counter():
    from src.services.ots import VerifyResult, failed_lookup_count

    assert failed_lookup_count(None) == 0
    assert failed_lookup_count(VerifyResult(verified=True, state="complete")) == 0
    assert failed_lookup_count(
        VerifyResult(verified=False, state="complete", transport_error="one failure",
                     transport_failures=1)
    ) == 1
    assert failed_lookup_count(
        VerifyResult(verified=False, state="complete", transport_error="a; b",
                     transport_failures=2)
    ) == 2


def test_failed_lookup_count_does_not_split_one_error_containing_the_separator():
    """MINOR 7: the count is structural, so a separator INSIDE one error cannot inflate it.

    The joined form uses "; ", and a perfectly ordinary explorer error contains that sequence
    (``HTTP Error 503: retry later; overloaded``). Recovering the count by splitting the display
    text reported one failed lookup as two — telling the operator that twice as much of their
    proof went unchecked as actually did.
    """
    from src.services.ots import VerifyResult, failed_lookup_count

    one = VerifyResult(
        verified=False,
        state="complete",
        transport_error="HTTP Error 503: retry later; overloaded",
        transport_failures=1,
    )
    assert failed_lookup_count(one) == 1


def test_failed_lookup_count_never_drops_a_reason_that_carries_no_count():
    """A hand-built fallback result (route/CLI) sets the reason but no count: report one, not zero.

    Zero would silently drop the disclosure the count exists to make.
    """
    from src.services.ots import VerifyResult, failed_lookup_count

    assert failed_lookup_count(
        VerifyResult(verified=False, state="none", transport_error="ots binary not found")
    ) == 1


# --- post-audit hardening of the verify path (fix-ux-audit-sprint1, §8) ---------------------
#
# The adversarial pass found three ways this module made a claim stronger than its evidence:
# a proof whose own digest was corrupted was reported as a changed FILE; a malformed explorer
# response was reported as a proof mismatch; and the node backend read success out of a regex
# rather than out of the process exit status. Each one manufactures a false verdict on the signal
# the product exists to make trustworthy, so each gets a regression here.


def test_explorer_tampered_proof_digest_is_a_neutral_disagreement(tmp_path, monkeypatch):
    """A flipped byte INSIDE a structurally valid `.ots` must not be blamed on the file.

    The proof still deserializes, so the only thing established is that the live digest and the
    digest the proof commits to DISAGREE. `ots.verify` therefore reports the disagreement and
    nothing else: no attestation was validated at this point either, so any claim that "the proof
    is intact and still attests the earlier version" would be invented. Blame is the callers' job,
    using the file's recorded baseline (design D1).
    """
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, file_digest, height=811111)

    raw = proof.read_bytes()
    assert raw.count(file_digest) == 1, "expected exactly one serialized copy of the file digest"
    at = raw.index(file_digest)
    proof.write_bytes(raw[:at] + b"\xcd" + raw[at + 1:])  # one flipped byte, still parseable

    def boom(*a, **k):
        raise AssertionError("explorer must not be queried on a digest disagreement")
    monkeypatch.setattr(ots, "_fetch_block_merkleroot", boom)

    result = ots.verify(proof, digest_hex)  # the FILE is untouched: this is the original digest
    assert result.verified is False
    assert result.digest_mismatch is True        # the neutral "these two disagree" signal
    assert result.unreadable_proof is False      # it parsed fine — that is the whole problem
    assert result.proof_mismatch is False
    # The message must not assign blame this comparison cannot establish.
    low = result.message.lower()
    assert "file changed" not in low and "changed since" not in low
    assert "cannot say which of the two changed" in low


def test_explorer_unreadable_proof_sets_the_typed_flag(tmp_path):
    """A proof that will not parse is its own outcome: nothing about the file was established."""
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"not a timestamp file at all")

    result = ots.verify(proof, "ab" * 32)
    assert result.verified is False
    assert result.unreadable_proof is True
    assert result.digest_mismatch is False and result.proof_mismatch is False
    assert result.message.startswith("unreadable proof:")


def _stub_explorer(monkeypatch, block_json, *, block_hash="ee" * 32):
    """Point `_fetch_block_merkleroot`'s HTTP helpers at canned responses (the real parser runs)."""
    from src.services import ots

    monkeypatch.setattr(ots, "_http_get_text", lambda url, timeout: block_hash)
    monkeypatch.setattr(ots, "_http_get_json", lambda url, timeout: block_json)


def test_explorer_malformed_merkle_root_is_a_transport_error_not_a_mismatch(tmp_path, monkeypatch):
    """`{"merkle_root": "00"}` is an explorer answering badly, not a proof that disagrees.

    A short hex string used to be accepted, compared unequal to the attestation's commitment and
    turned into `proof_mismatch=True` — a red "this proof does not check out" over intact evidence,
    manufactured out of a malformed response.
    """
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex(digest_hex), height=811111)
    _stub_explorer(monkeypatch, {"merkle_root": "00", "timestamp": 1707935720})

    result = ots.verify(proof, digest_hex)
    assert result.proof_mismatch is False, "malformed block data must never become a mismatch"
    assert result.verified is False
    assert result.transport_error is not None and "malformed merkle root" in result.transport_error
    assert result.transport_failures == 1


def test_explorer_malformed_block_timestamp_is_a_transport_error(tmp_path, monkeypatch):
    """An out-of-range block time cannot become an "existed by" date the operator relies on."""
    from src.services import ots

    digest_hex = "ab" * 32
    file_digest = bytes.fromhex(digest_hex)
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, file_digest, height=811111)
    # The merkle root MATCHES the commitment, so only the timestamp check stands between a bogus
    # response and a verified result carrying a year-33658 date.
    _stub_explorer(
        monkeypatch, {"merkle_root": file_digest[::-1].hex(), "timestamp": 999999999999}
    )

    result = ots.verify(proof, digest_hex)
    assert result.verified is False
    assert result.existed_by is None
    assert result.transport_error is not None and "malformed timestamp" in result.transport_error


def test_explorer_non_hash_at_height_is_a_transport_error(tmp_path, monkeypatch):
    from src.services import ots

    digest_hex = "ab" * 32
    proof = tmp_path / "x.ots"
    _write_btc_proof(proof, bytes.fromhex(digest_hex), height=811111)
    _stub_explorer(monkeypatch, {}, block_hash="00")

    result = ots.verify(proof, digest_hex)
    assert result.verified is False and result.proof_mismatch is False
    assert result.transport_error is not None and "no block at height" in result.transport_error


def test_node_backend_rc_zero_verifies_even_without_parseable_metadata(tmp_path, monkeypatch):
    """The exit status is the success contract; the block/date line is optional metadata.

    A successful `ots verify -d` whose wording the regex does not recognise used to be swallowed
    into an inconclusive verdict — throwing away a verification that actually happened.
    """
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 0, "", ""  # success, no recognisable line
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "ab" * 32, backend="node")
    assert result.verified is True
    assert result.block_height is None and result.existed_by is None
    assert result.inconclusive is False


def test_node_backend_never_verifies_on_a_nonzero_exit(tmp_path, monkeypatch):
    """The mirror direction, and the dangerous one: success-looking text under a failed exit."""
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 0, INFO_COMPLETE, ""
        return 1, "", VERIFY_SUCCESS  # the tool FAILED while echoing a success line
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "ab" * 32, backend="node")
    assert result.verified is False, "a non-zero exit must never read as verified"
    assert result.inconclusive is True


def test_node_backend_flags_an_existing_unparseable_proof_as_unreadable(tmp_path, monkeypatch):
    """An existing `.ots` that `ots info` cannot deserialize is `unreadable_proof`, not "no proof".

    `info()` collapses "no such file" and "exists but unparseable" into `state="none"`, and the
    node path used to forward that untyped, so the panel fell through to copy offering file-change
    possibilities this check never examined — the explorer backend has reported it typed since
    round 1. Nothing was established here about the file OR the proof's content.
    """
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"not an OpenTimestamps proof")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        if args[0] == "info":
            return 1, "", "Error! Unknown file type"
        raise AssertionError("verify must not be attempted against an unparseable proof")
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "ab" * 32, backend="node")
    assert result.unreadable_proof is True
    assert result.verified is False and result.state == "none"
    # Not a transport failure and not a mismatch: nothing was reached, nothing was compared.
    assert result.transport_error is None
    assert result.digest_mismatch is False and result.proof_mismatch is False
    assert "unreadable proof" in result.message


def test_node_backend_missing_proof_is_not_reported_as_unreadable(tmp_path, monkeypatch):
    """The other side of the same split: a file that was never written is an absence, not damage."""
    from src.services import ots

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        raise AssertionError("nothing should be shelled out for a proof that does not exist")
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(tmp_path / "never-written.ots", "ab" * 32, backend="node")
    assert result.unreadable_proof is False
    assert result.verified is False and result.state == "none"
    assert "no usable proof" in result.message


def test_node_backend_info_failure_is_a_transport_error_not_an_exception(tmp_path, monkeypatch):
    """`info` shells out too, so it must sit inside the same transport boundary.

    Outside it, a missing binary raised out of `verify()` — which `cairn verify` does not catch, so
    the command aborted with a traceback instead of printing COULD NOT CHECK.
    """
    from src.services import ots

    proof = tmp_path / "x.ots"
    proof.write_bytes(b"stub")

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        raise ots.OtsError("ots binary not found")
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    result = ots.verify(proof, "ab" * 32, backend="node")  # must not raise
    assert result.verified is False
    assert result.transport_error == "ots binary not found"
    assert result.transport_failures == 1
    assert result.state == "none" and result.inconclusive is False
