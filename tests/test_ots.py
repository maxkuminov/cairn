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

    def fake_batch(pairs, calendars, staging):
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
