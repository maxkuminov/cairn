"""A stamp never destroys a proof, and each placed proof records its digest (#15, design D1/D1a/D2/D3/D7).

The product is a trust claim — *these bytes existed, unaltered, by this date* — so the expensive
failure here is not an error message, it is a **silent loss of evidence**. `_place_proof` used to end
in a bare ``os.replace``, so any second stamp of the same relpath landed on top of whatever was
there: a multi-year Bitcoin anchor could be overwritten by a proof made this morning, through the
product's own recommended accept -> restore -> rescan workflow, and the panel stayed green.

Every test below pins one way that loss could come back:

* preservation and its ORDERING (archive first, unlink last, directory entries flushed);
* the archive never discarding and never overwriting, not even for a duplicate digest;
* adoption requiring a LIVE anchor confirmation, never a bare digest match and never the row's own
  recorded provenance (which the swap itself would satisfy);
* an unreachable backend recording nothing at all rather than buying a `complete` notarization;
* `files.ots_digest` written only where the file's own bytes corroborate it;
* and the verify blame ladder's order, which is the difference between "this is not your proof" and
  a false "just an older proof of your file".

Run from the repo root: ``PYTHONPATH=. pytest tests/test_proof_preservation.py``
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import seed_collection

# --- proof fixtures --------------------------------------------------------------------------


def _write_proof(
    path: Path,
    digest_hex: str,
    *,
    height: int | None = None,
    calendar: str = "https://a.pool.opentimestamps.org",
) -> bytes:
    """Serialize a minimal, REAL ``.ots`` committing to ``digest_hex``; return its bytes.

    ``height`` given -> a ``BitcoinBlockHeaderAttestation`` (syntactically "complete"); otherwise a
    ``PendingAttestation`` (``incomplete``). The attestation hangs off the root timestamp, so its
    commitment is the file digest itself, which lets a test stub the explorer trivially.
    """
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    ts = Timestamp(bytes.fromhex(digest_hex))
    ts.attestations.add(
        BitcoinBlockHeaderAttestation(height) if height is not None else PendingAttestation(calendar)
    )
    ctx = BytesSerializationContext()
    DetachedTimestampFile(OpSHA256(), ts).serialize(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ctx.getbytes())
    return path.read_bytes()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive_files(store: Path, collection_id: int = 1) -> list[Path]:
    root = store / ".superseded" / str(collection_id)
    return sorted(p for p in root.rglob("*.ots")) if root.exists() else []


def _unmute_cairn_loggers() -> None:
    """Undo ``alembic``'s ``disable_existing_loggers``.

    The ``cairn_env`` fixture runs the migrations, and ``alembic``'s ``fileConfig`` marks every
    already-created ``cairn.*`` logger disabled as a side effect — so a ``caplog`` assertion made
    afterwards would silently see nothing and pass or fail for the wrong reason. Call this INSIDE
    the test body (an autouse fixture can run before ``cairn_env`` and be undone by it).
    """
    for name in ("cairn.ots", "cairn.proofs"):
        logger = logging.getLogger(name)
        logger.disabled = False
        logger.propagate = True


@pytest.fixture(autouse=True)
def _reset_dir_sync_degrade():
    """The unsupported-directory-`fsync` degrade is remembered in process memory, per proof store.

    Tests share a process, so a store recorded best-effort by one test must not silence the WARNING
    (or skip the syncs) another test asserts on.

    The same fixture re-enables Cairn's loggers: ``alembic``'s ``fileConfig`` (run by the
    ``cairn_env`` fixture's migration) applies ``disable_existing_loggers``, which silently marks
    every already-created ``cairn.*`` logger disabled — so a later ``caplog`` assertion in this
    module would see nothing and pass or fail for the wrong reason.
    """
    from src.services import ots

    _unmute_cairn_loggers()
    ots._BEST_EFFORT_DIR_SYNC.clear()
    yield
    ots._BEST_EFFORT_DIR_SYNC.clear()


# --- DB fixtures -----------------------------------------------------------------------------


async def _seed(root: Path, files: dict[str, bytes], **entry_kw) -> int:
    """Create a collection at ``root`` holding ``files``; add one pending row per file."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root.mkdir(parents=True, exist_ok=True)
    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for relpath, data in files.items():
            target = root / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            kw = dict(
                collection_id=cid,
                relpath=relpath,
                size=len(data),
                sha256=_sha(data),
                status="new",
                ots_state="pending",
                first_seen=now,
                last_checked=now,
            )
            kw.update(entry_kw)
            s.add(FileEntry(**kw))
        await s.commit()
    return cid


def _stamp_fake(calls: list | None = None):
    """A fake ``_run_ots`` that writes a REAL, freshly-pending ``.ots`` per staged symlink.

    The suite's older fake writes the literal bytes ``b"proof"``, which cannot be parsed — fine for
    path-handling tests, useless here, where every branch turns on what the staged proof COMMITS TO.
    """
    from src.services import ots

    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):  # noqa: ANN001
        if calls is not None:
            calls.append(list(args))
        for a in args:
            p = Path(a)
            if p.is_symlink():
                target = Path(os.readlink(p))
                _write_proof(p.with_name(p.name + ".ots"), _sha(target.read_bytes()))
        return 0, "", ""

    return fake_run


def _verify_result(**kw):
    from src.services.ots import VerifyResult

    kw.setdefault("verified", False)
    kw.setdefault("state", "complete")
    return VerifyResult(**kw)


async def _entry(cid: int, relpath: str):
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(FileEntry).where(
                FileEntry.collection_id == cid, FileEntry.relpath == relpath
            )
        )


async def _stamp(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    async with get_sessionmaker()() as s:
        return await proofs.stamp_pending(s, await s.get(Collection, cid))


# ==============================================================================================
# 2.16 / 2.31 — an anchored proof survives a same-digest re-stamp
# ==============================================================================================


async def test_confirmed_anchored_proof_survives_a_same_digest_restamp(cairn_env, monkeypatch):
    """#15's headline case: a confirmed anchor is kept, not replaced by a proof made today.

    The row comes out `complete` with `ots_stamped_at` **unmoved** — stamping a three-year-old
    anchor with today's date is the same class of lie as labelling a download "existed by
    <ots_stamped_at>". The proof's own attestation carries the real date.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"a contract"
    root = cairn_env / "vault"
    cid = await _seed(root, {"contract.pdf": data})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "contract.pdf")
    original = _write_proof(canonical, _sha(data), height=800_000)

    old_stamp = datetime.now(timezone.utc) - timedelta(days=900)
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        fe.ots_stamped_at = old_stamp
        await s.commit()

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    monkeypatch.setattr(ots, "verify", lambda *a, **k: _verify_result(verified=True, block_height=800_000))

    assert await _stamp(cid) == 1
    fe = await _entry(cid, "contract.pdf")
    assert fe.ots_state == "complete"
    assert fe.ots_digest == _sha(data)
    assert canonical.read_bytes() == original, "the anchored proof's bytes were replaced"
    assert fe.ots_stamped_at.replace(tzinfo=timezone.utc) == old_stamp

    # …and the placement backstop agrees, on its own, when handed the same verdict. `_place_proof`
    # is offline, so it may only keep an existing proof on the CALLER's confirmation.
    staged = cairn_env / "staged.ots"
    _write_proof(staged, _sha(data))
    outcome = ots._place_proof(
        staged, canonical, store_root=Path(settings.proof_store_path), verdict="confirmed"
    )
    assert outcome.kind == "kept" and outcome.state == "complete"
    assert canonical.read_bytes() == original


async def test_adoption_takes_no_calendar_round_trip(cairn_env, monkeypatch):
    """2.31: an adoptable proof is recorded WITHOUT stamping — the stamp helper is never called."""
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"already notarized"
    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": data})
    canonical = proofs.proof_path(get_settings(), cid, "doc.txt")
    _write_proof(canonical, _sha(data), height=811_111)

    def boom(*a, **k):
        raise AssertionError("an adoptable proof must not be re-stamped")

    monkeypatch.setattr(ots, "stamp_batch_via_symlink", boom)
    monkeypatch.setattr(ots, "stamp_via_symlink", boom)
    monkeypatch.setattr(ots, "verify", lambda *a, **k: _verify_result(verified=True, block_height=811_111))

    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    assert (fe.ots_state, fe.ots_digest, fe.ots_stamped_at) == ("complete", _sha(data), None)


# ==============================================================================================
# 2.17 / 2.19 / 2.28 / 2.28a — the archive keeps everything
# ==============================================================================================


async def test_a_different_digest_stamp_keeps_both_proofs(cairn_env, monkeypatch):
    """2.17: the canonical path serves the NEW digest; the old proof survives, byte-identical."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"version two"})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    old_digest = _sha(b"version one")
    old_bytes = _write_proof(canonical, old_digest, height=700_000)

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    assert await _stamp(cid) == 1

    store = Path(settings.proof_store_path)
    archived = store / ".superseded" / str(cid) / old_digest[:2] / f"{old_digest}.ots"
    assert archived.read_bytes() == old_bytes, "the superseded proof was not preserved intact"
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "incomplete" and fe.ots_digest == _sha(b"version two")
    assert ots.read_proof_facts(canonical).digest == _sha(b"version two")


def test_an_unreadable_existing_proof_is_archived_not_deleted(tmp_path):
    """2.19: a proof this build cannot parse may still be a valid proof. Deleting it is the bug."""
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"not a timestamp file at all")
    staged = tmp_path / "staged.ots"
    _write_proof(staged, "ab" * 32)

    outcome = ots._place_proof(staged, canonical, store_root=store)

    assert outcome.kind == "placed" and outcome.digest == "ab" * 32
    opaque = list((store / ".superseded" / "1" / "unknown").iterdir())
    assert len(opaque) == 1
    assert opaque[0].read_bytes() == b"not a timestamp file at all"
    assert ots.read_proof_facts(canonical).digest == "ab" * 32


def test_archive_collision_keeps_both_proofs(tmp_path):
    """2.28: two proofs for one digest are not interchangeable — one may carry an anchor the other
    lacks — and deciding which is stronger is a judgement an archive must not make."""
    from src.services import ots

    store = tmp_path / "proofs"
    digest = "cd" * 32
    archive_root = ots.superseded_root(store, 1)
    facts = ots.StoredProofFacts(readable=True, digest=digest, anchored=True)

    first = tmp_path / "first.ots"
    first_bytes = _write_proof(first, digest, height=500_000)
    second = tmp_path / "second.ots"
    second_bytes = _write_proof(second, digest, height=600_000)
    assert first_bytes != second_bytes

    a = ots._preserve_proof(first, archive_root, facts, store_key=str(store))
    b = ots._preserve_proof(second, archive_root, facts, store_key=str(store))

    assert a.name == f"{digest}.ots" and b.name == f"{digest}.1.ots"
    assert a.read_bytes() == first_bytes, "the first archived proof was overwritten"
    assert b.read_bytes() == second_bytes, "the second proof was discarded as a duplicate"


def test_preservation_never_uses_hard_links(tmp_path, monkeypatch):
    """2.28a: the proof store's contract is 'writable', not 'supports hard links'.

    On a CIFS/FAT/FUSE store ``os.link`` returns EPERM, and because an archive failure is classified
    transient, a link-based implementation would turn every occupied-path placement into a permanent
    retry loop reported as transient — no proof on that collection could ever be refreshed.
    """
    from src.services import ots

    linked: list = []

    def refuse_link(*a, **k):
        linked.append(a)
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", refuse_link)

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "11" * 32, height=400_000)
    staged = tmp_path / "staged.ots"
    _write_proof(staged, "22" * 32)

    outcome = ots._place_proof(staged, canonical, store_root=store)

    assert outcome.kind == "placed"
    assert not linked, "preservation must not depend on hard-link support"
    archived = store / ".superseded" / "1" / "11" / f"{'11' * 32}.ots"
    assert archived.read_bytes() == old_bytes


# ==============================================================================================
# 2.5 / 2.20 / 2.21 — a failure to preserve refuses the placement
# ==============================================================================================


def _block_archive(store: Path) -> None:
    """Make the archive subtree un-creatable by putting a FILE where its root must be a directory."""
    (store / ".superseded").parent.mkdir(parents=True, exist_ok=True)
    (store / ".superseded").write_bytes(b"in the way")


def test_archive_failure_refuses_the_placement_transiently(tmp_path):
    """2.20 (unit): the existing proof stays intact, nothing is placed, and the error is TRANSIENT.

    Permanent would drop the file to `ots_state='none'` and stop retrying — a silent loss of
    notarization over a fixable store problem. The module's governing rule: only a failure on the
    FINAL output path may be permanent, and the archive is not that path.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "33" * 32, height=300_000)
    staged = tmp_path / "staged.ots"
    staged_bytes = _write_proof(staged, "44" * 32)
    _block_archive(store)

    with pytest.raises(ots.OtsError) as excinfo:
        ots._place_proof(staged, canonical, store_root=store)

    assert not isinstance(excinfo.value, ots.OtsPathError), "an archive failure must stay retryable"
    assert canonical.read_bytes() == old_bytes
    assert staged.read_bytes() == staged_bytes


async def test_archive_failure_leaves_the_file_pending(cairn_env, monkeypatch):
    """2.20 (row level): the member is left `pending` for retry, never dropped to `none`."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"new bytes"})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    old_bytes = _write_proof(canonical, _sha(b"old bytes"), height=200_000)
    _block_archive(Path(settings.proof_store_path))

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    assert await _stamp(cid) == 0

    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "pending" and fe.ots_digest is None and fe.ots_path is None
    assert canonical.read_bytes() == old_bytes


async def test_one_unarchivable_member_does_not_stop_the_batch(cairn_env, monkeypatch):
    """2.21: per-member isolation survives preservation — one bad old proof, one good placement."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"a.txt": b"aaa", "b.txt": b"bbb"})
    settings = get_settings()
    a_canonical = proofs.proof_path(settings, cid, "a.txt")
    b_canonical = proofs.proof_path(settings, cid, "b.txt")
    a_old = _write_proof(a_canonical, _sha(b"a-old"), height=100_000)
    _write_proof(b_canonical, _sha(b"b-old"), height=100_001)

    real_preserve = ots._preserve_proof

    def selective(source, archive_root, facts, *, store_key, sync_root=None):
        if Path(source).name == "a.txt.ots":
            raise OSError(errno.EACCES, "Permission denied")
        return real_preserve(
            source, archive_root, facts, store_key=store_key, sync_root=sync_root
        )

    monkeypatch.setattr(ots, "_preserve_proof", selective)
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1

    a = await _entry(cid, "a.txt")
    b = await _entry(cid, "b.txt")
    assert a.ots_state == "pending" and a_canonical.read_bytes() == a_old
    assert b.ots_state == "incomplete" and b.ots_digest == _sha(b"bbb")


# ==============================================================================================
# 2.22 — the unwritable-path skip clears provenance too
# ==============================================================================================


async def test_unwritable_proof_path_skip_clears_ots_digest(cairn_env, monkeypatch):
    """2.22: `ots_digest` records the digest of a proof AT `ots_path`. No proof, no provenance."""
    from src.services import ots

    root = cairn_env / "vault"
    long_name = "д" * 126  # 252 bytes on disk; + ".ots" pushes the proof name past NAME_MAX
    cid = await _seed(
        root,
        {long_name: b"x"},
        ots_path="/stale/pointer.ots",
        ots_digest="ee" * 32,
        ots_stamped_at=datetime.now(timezone.utc),
    )
    assert len(os.fsencode(long_name + ".ots")) > ots._NAME_MAX_BYTES

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    assert await _stamp(cid) == 0

    fe = await _entry(cid, long_name)
    assert fe.ots_state == "none"
    assert (fe.ots_path, fe.ots_digest, fe.ots_stamped_at) == (None, None, None)


# ==============================================================================================
# 2.29 / 2.29a / 2.29b — crash safety and the durability ordering it rests on
# ==============================================================================================


async def test_interrupted_between_archive_and_place_loses_nothing(cairn_env, monkeypatch):
    """2.29: archive first, place second — so an interruption between them is RECOVERABLE.

    The old proof is safe in the archive, the canonical path is absent, and the caller wrote no row
    state, so the file is still `pending` and the next pass completes the placement. The reverse
    order would destroy the proof before preserving it, which is the whole bug.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"new"})
    settings = get_settings()
    store = Path(settings.proof_store_path)
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    old_digest = _sha(b"old")
    old_bytes = _write_proof(canonical, old_digest, height=123_456)

    def crash(*a, **k):
        raise RuntimeError("power cut")

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    monkeypatch.setattr(ots.os, "replace", crash)

    with pytest.raises(RuntimeError):
        await _stamp(cid)

    archived = store / ".superseded" / str(cid) / old_digest[:2] / f"{old_digest}.ots"
    assert archived.read_bytes() == old_bytes
    assert not canonical.exists()
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "pending" and fe.ots_path is None and fe.ots_digest is None

    monkeypatch.undo()
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "incomplete" and fe.ots_digest == _sha(b"new")
    assert archived.read_bytes() == old_bytes


def _syscall_recorder(monkeypatch, under: Path) -> list[tuple]:
    """Record the file-system syscalls `_place_proof` makes, WITH their paths, in order.

    The durability argument is entirely about ordering — which name exists, and is durable, at each
    instant — so a test that only counts generic "a directory was fsynced" events cannot tell a
    correct implementation from one that flushes the wrong directories, flushes before closing, or
    writes a prefix of the proof (Codex MINOR). Every recorded event therefore carries its path:
    exclusive creations, per-call write byte counts, closes, each fsync tagged file-vs-directory,
    unlinks and renames. Events outside ``under`` (pytest's own I/O) are dropped.
    """
    events: list[tuple] = []
    fds: dict[int, str] = {}
    real_open, real_write, real_close = os.open, os.write, os.close
    real_fsync, real_unlink, real_replace = os.fsync, os.unlink, os.replace
    prefix = str(under)

    def keep(path: str | None) -> bool:
        return path is not None and path.startswith(prefix)

    def rec_open(path, flags, mode=0o777, **kw):
        fd = real_open(path, flags, mode, **kw)
        fds[fd] = str(path)
        if flags & os.O_EXCL and keep(str(path)):
            events.append(("open_excl", str(path)))
        return fd

    def rec_write(fd, data):
        n = real_write(fd, data)
        if keep(fds.get(fd)):
            events.append(("write", fds[fd], n))
        return n

    def rec_fsync(fd):
        path = fds.get(fd)
        if keep(path):
            kind = "fsync_dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync_file"
            events.append((kind, path))
        return real_fsync(fd)

    def rec_close(fd):
        path = fds.pop(fd, None)
        if keep(path):
            events.append(("close", path))
        return real_close(fd)

    def rec_unlink(path, **kw):
        if keep(str(path)):
            events.append(("unlink", str(path)))
        return real_unlink(path, **kw)

    def rec_replace(src, dst, **kw):
        if keep(str(dst)):
            events.append(("replace", str(dst)))
        return real_replace(src, dst, **kw)

    monkeypatch.setattr(os, "open", rec_open)
    monkeypatch.setattr(os, "write", rec_write)
    monkeypatch.setattr(os, "fsync", rec_fsync)
    monkeypatch.setattr(os, "close", rec_close)
    monkeypatch.setattr(os, "unlink", rec_unlink)
    monkeypatch.setattr(os, "replace", rec_replace)
    return events


def test_preservation_syncs_directories_before_unlinking_the_source(tmp_path, monkeypatch):
    """2.29a: the exact call ordering the durability argument rests on.

    A file `fsync` commits BYTES, never the directory entry that NAMES them. So "create -> copy ->
    fsync file -> unlink source" has a real crash window: the unlink can persist while the archive's
    new name never lands, and the only copy of the proof is gone. A true power-loss test is out of
    scope — nothing here can cut power or model write reordering — but the ordering is the part the
    implementation can get wrong, so the whole sequence is asserted literally, path by path.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "55" * 32, height=555_000)
    staged = tmp_path / "staged.ots"
    _write_proof(staged, "66" * 32)

    events = _syscall_recorder(monkeypatch, tmp_path)
    ots._place_proof(staged, canonical, store_root=store)

    archived = store / ".superseded" / "1" / "55" / f"{'55' * 32}.ots"
    assert events == [
        # 1. claim the archive name exclusively (never `os.replace`: it would overwrite a proof),
        ("open_excl", str(archived)),
        # 2. write EVERY byte of the proof — a short write must never be fsynced and named,
        ("write", str(archived), len(old_bytes)),
        # 3. flush the bytes, then close,
        ("fsync_file", str(archived)),
        ("close", str(archived)),
        # 4. flush the chain of directories that NAME it, deepest-first (parent after child), all
        #    the way to the proof-store root — never just the ones this call happened to create,
        ("fsync_dir", str(store / ".superseded" / "1" / "55")),
        ("close", str(store / ".superseded" / "1" / "55")),
        ("fsync_dir", str(store / ".superseded" / "1")),
        ("close", str(store / ".superseded" / "1")),
        ("fsync_dir", str(store / ".superseded")),
        ("close", str(store / ".superseded")),
        ("fsync_dir", str(store)),
        ("close", str(store)),
        # 5. and ONLY THEN remove the source: before this instant the proof is at its canonical
        #    path; after it, the archived name is durable. "Both names gone" is unreachable.
        ("unlink", str(canonical)),
        # 6. the placement, whose own name is likewise durable before the caller records anything.
        ("replace", str(canonical)),
        ("fsync_dir", str(store / "1")),
        ("close", str(store / "1")),
        ("fsync_dir", str(store)),
        ("close", str(store)),
    ]
    assert archived.read_bytes() == old_bytes


def test_a_short_write_still_archives_the_complete_proof(tmp_path, monkeypatch):
    """`os.write` may legally write FEWER bytes than it was handed — and then the source is unlinked.

    An unchecked single `os.write` archives a PREFIX of the proof, fsyncs it, makes its name durable
    and removes the intact original: the evidence is destroyed exactly as thoroughly as by the
    overwrite this whole design exists to prevent, and nothing reports a failure.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "aa" * 32, height=444_000)
    staged = tmp_path / "staged.ots"
    staged_bytes = _write_proof(staged, "bb" * 32)

    real_write = os.write
    calls: list[int] = []

    def dribble(fd, data):
        # The pathological-but-legal filesystem: one byte per call.
        n = real_write(fd, bytes(data)[:1])
        calls.append(n)
        return n

    monkeypatch.setattr(os, "write", dribble)
    outcome = ots._place_proof(staged, canonical, store_root=store)

    assert outcome.kind == "placed"
    archived = store / ".superseded" / "1" / "aa" / f"{'aa' * 32}.ots"
    assert archived.read_bytes() == old_bytes, "the archived proof is a truncated copy"
    assert len(calls) == len(old_bytes), "the write loop did not cover the whole payload"
    assert canonical.read_bytes() == staged_bytes


def test_a_write_that_cannot_progress_refuses_and_keeps_the_source(tmp_path, monkeypatch):
    """A write that makes no progress is a failure, not an infinite retry — and never a proof loss.

    The refusal is TRANSIENT (the archive is not the final output path), so the file stays queued;
    the canonical proof is untouched; and the half-claimed archive slot does not linger as a
    truncated impostor of a proof.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "cc" * 32, height=333_000)
    staged = tmp_path / "staged.ots"
    staged_bytes = _write_proof(staged, "dd" * 32)

    monkeypatch.setattr(os, "write", lambda fd, data: 0)

    with pytest.raises(ots.OtsError) as excinfo:
        ots._place_proof(staged, canonical, store_root=store)
    assert not isinstance(excinfo.value, ots.OtsPathError), "a preservation failure is transient"

    assert canonical.read_bytes() == old_bytes, "the source was removed despite a failed archive"
    assert staged.read_bytes() == staged_bytes, "the staged proof was consumed by a failed placement"
    family = store / ".superseded" / "1" / "cc"
    assert not list(family.glob("*.ots")), "a truncated archive slot was left behind"


def test_a_retry_after_a_failed_attempt_flushes_the_whole_chain(tmp_path, monkeypatch):
    """A directory left by a FAILED attempt must not be mistaken for a durable one.

    "Sync what this call created" is wrong across retries: the first attempt creates the archive
    chain and dies before flushing it, so the retry sees those directories as pre-existing, flushes
    only the deepest, unlinks the source and lets the row be recorded — and a power loss can still
    lose an ancestor's name, with it the archived proof (Codex B2). Anchoring the chain on the
    proof-store root, whose name predates every proof, is immune to that residue.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    canonical = store / "1" / "doc.txt.ots"
    old_bytes = _write_proof(canonical, "ff" * 32, height=222_000)
    staged = tmp_path / "staged.ots"
    _write_proof(staged, "0a" * 32)

    # Attempt 1: fails mid-write, AFTER `mkdir -p` created the whole archive chain.
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(ots.OtsError):
        ots._place_proof(staged, canonical, store_root=store)
    monkeypatch.undo()
    assert (store / ".superseded" / "1" / "ff").is_dir(), "the residue this test is about"
    assert canonical.read_bytes() == old_bytes

    # Attempt 2 succeeds — and must still flush every directory up to the store root, even though
    # it created none of them.
    events = _syscall_recorder(monkeypatch, tmp_path)
    ots._place_proof(staged, canonical, store_root=store)

    unlink_at = next(i for i, e in enumerate(events) if e[0] == "unlink")
    assert [e for e in events[:unlink_at] if e[0] == "fsync_dir"] == [
        ("fsync_dir", str(store / ".superseded" / "1" / "ff")),
        ("fsync_dir", str(store / ".superseded" / "1")),
        ("fsync_dir", str(store / ".superseded")),
        ("fsync_dir", str(store)),
    ]
    archived = store / ".superseded" / "1" / "ff" / f"{'ff' * 32}.ots"
    assert archived.read_bytes() == old_bytes


def test_the_archive_family_has_no_ceiling(tmp_path):
    """A fixed suffix bound turns a busy proof store into one that can never preserve again.

    Preservation is the step that exists so no proof is ever destroyed; a ceiling on it means every
    later placement refuses permanently once the family is full — reachable by a prolonged deferral
    loop or by a prepopulated store (Codex M1). There is no bound, and finding the free slot costs
    one directory read rather than one failed `open` per occupied slot.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    digest = "1e" * 32
    archive_root = ots.superseded_root(store, 1)
    family = archive_root / digest[:2]
    family.mkdir(parents=True)
    occupied = 10_002  # past the ceiling this used to have
    for i in range(occupied):
        (family / (f"{digest}.ots" if i == 0 else f"{digest}.{i}.ots")).write_bytes(b"old")

    source = tmp_path / "incoming.ots"
    payload = _write_proof(source, digest, height=111_000)
    slot = ots._preserve_proof(
        source,
        archive_root,
        ots.StoredProofFacts(readable=True, digest=digest, anchored=True),
        store_key=str(store),
    )

    assert slot.name == f"{digest}.{occupied}.ots"
    assert slot.read_bytes() == payload
    assert not source.exists()
    assert len(list(family.glob("*.ots"))) == occupied + 1, "an existing archived proof was replaced"


def test_a_store_that_cannot_flush_directories_degrades_instead_of_wedging(
    tmp_path, monkeypatch, caplog
):
    """2.29b: EINVAL/ENOTSUP/EOPNOTSUPP on a directory is DETERMINISTIC, not transient.

    Treating it as transient would refuse every occupied-path placement forever, and pile up one
    more suffixed archive slot on every doomed retry: the notary silently stops notarizing while the
    archive grows without bound. A durability nicety must never cost the notary its ability to stamp,
    so the store degrades to best-effort with ONE warning. Every other errno stays a real failure.
    """
    from src.services import ots

    store = tmp_path / "proofs"
    real_fsync = os.fsync
    fsynced_files: list[int] = []

    def no_dir_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTSUP, "Operation not supported")
        fsynced_files.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", no_dir_fsync)

    old_digest = "77" * 32
    old_bytes = []
    unlinks: list[str] = []
    real_unlink = os.unlink

    def rec_unlink(path, **kw):
        unlinks.append(str(path))
        return real_unlink(path, **kw)

    monkeypatch.setattr(os, "unlink", rec_unlink)

    _unmute_cairn_loggers()
    with caplog.at_level(logging.WARNING, logger="cairn.ots"):
        for i, name in enumerate(("a.txt", "b.txt")):
            canonical = store / "1" / f"{name}.ots"
            old_bytes.append(_write_proof(canonical, old_digest, height=700_000 + i))
            staged = tmp_path / f"staged{i}.ots"
            _write_proof(staged, f"{80 + i}" * 32)
            outcome = ots._place_proof(staged, canonical, store_root=store)
            assert outcome.kind == "placed"
            assert canonical.exists()

    family = {p.name: p.read_bytes() for p in (store / ".superseded" / "1" / "77").iterdir()}
    assert set(family) == {f"{old_digest}.ots", f"{old_digest}.1.ots"}, (
        "a retry on a store that cannot flush directories added an extra archive slot"
    )
    assert [family[f"{old_digest}.ots"], family[f"{old_digest}.1.ots"]] == old_bytes
    assert fsynced_files, "the archived file's own bytes must still be flushed"
    assert unlinks, "the source must still be unlinked, and only after the file fsync"

    limitation = [
        r for r in caplog.records
        if "does not support flushing directory entries" in r.getMessage()
    ]
    assert len(limitation) == 1, "the limitation is announced exactly once per proof store"

    # Counter-case: a real I/O error is NOT an unsupported operation. It refuses the placement.
    ots._BEST_EFFORT_DIR_SYNC.clear()

    def io_error(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "Input/output error")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", io_error)
    canonical = store / "1" / "c.txt.ots"
    kept = _write_proof(canonical, old_digest, height=900_000)
    staged = tmp_path / "staged-eio.ots"
    _write_proof(staged, "99" * 32)
    with pytest.raises(ots.OtsError) as excinfo:
        ots._place_proof(staged, canonical, store_root=store)
    assert not isinstance(excinfo.value, ots.OtsPathError)
    assert canonical.read_bytes() == kept, "the existing proof must survive a refused placement"


async def test_interrupted_after_placement_before_the_commit_loses_nothing(cairn_env, monkeypatch):
    """2.30: a crash between `os.replace` and the DB commit leaves the file `pending`.

    The next pass re-enters placement, finds a proof for its OWN digest, and (per D1a) adopts,
    archives-and-replaces, or defers. In no branch is a proof lost.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"live bytes"})
    settings = get_settings()
    store = Path(settings.proof_store_path)
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    old_digest = _sha(b"previous bytes")
    old_bytes = _write_proof(canonical, old_digest, height=321_000)

    # The placement runs to completion; the row update never happens (the process died).
    staged = cairn_env / "staged.ots"
    _write_proof(staged, _sha(b"live bytes"))
    assert ots._place_proof(staged, canonical, store_root=store).kind == "placed"

    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "pending" and fe.ots_digest is None

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    monkeypatch.setattr(
        ots, "verify", lambda *a, **k: _verify_result(verified=True, block_height=1)
    )
    # The proof left on disk is `incomplete` (never anchored), so adoption must decline it and the
    # ordinary path runs — archiving that proof rather than destroying it.
    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "incomplete" and fe.ots_digest == _sha(b"live bytes")
    archived = _archive_files(store, cid)
    assert (store / ".superseded" / str(cid) / old_digest[:2] / f"{old_digest}.ots").read_bytes() == old_bytes
    assert len(archived) == 2, "both displaced proofs are preserved"


# ==============================================================================================
# 2.24 / 2.32 / 2.33 / 2.34 / 2.35 — adoption is not a bare digest match
# ==============================================================================================


async def test_a_different_digest_proof_is_never_adopted(cairn_env, monkeypatch):
    """2.24: nothing about the existing proof matches this file, so no lookup and no adoption."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"mine"})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    other = _sha(b"somebody else's bytes")
    other_bytes = _write_proof(canonical, other, height=650_000)

    def no_lookup(*a, **k):
        raise AssertionError("a different-digest proof needs no backend lookup")

    monkeypatch.setattr(ots, "verify", no_lookup)
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "incomplete" and fe.ots_digest == _sha(b"mine")
    archived = Path(settings.proof_store_path) / ".superseded" / str(cid) / other[:2] / f"{other}.ots"
    assert archived.read_bytes() == other_bytes


async def test_recorded_provenance_does_not_qualify_for_adoption(cairn_env, monkeypatch):
    """2.32: `ots_digest` is a DETECTION record, never an authentication of the artifact on disk.

    Unboundedly many distinct `.ots` files commit to one digest, so "recorded provenance equals the
    digest of the proof now at that path" is satisfied *by the swap itself*. Short-circuiting the
    backend lookup on it would promote a fabricated proof to `complete` with no chain consulted.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"contested"
    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": data}, ots_digest=_sha(data))
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    forged = _write_proof(canonical, _sha(data), height=999_999)

    lookups: list = []

    def answering_backend(*a, **k):
        lookups.append(a)
        return _verify_result(proof_mismatch=True, proof_digest=_sha(data))

    monkeypatch.setattr(ots, "verify", answering_backend)
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1
    assert lookups, "a provenance short-circuit would have skipped the backend lookup"
    fe = await _entry(cid, "doc.txt")
    # Recorded from the NEWLY PLACED proof, and `incomplete` — never `complete` from the rejected one.
    assert fe.ots_state == "incomplete" and fe.ots_stamped_at is not None
    assert canonical.read_bytes() != forged, "the rejected proof kept the canonical path"
    assert _archive_files(Path(settings.proof_store_path), cid)[0].read_bytes() == forged


async def test_an_incomplete_same_digest_proof_is_never_adopted(cairn_env, monkeypatch):
    """2.33: a never-anchored proof has no anchor to verify, and `stale_incomplete` exists precisely
    so it CAN be refreshed. Adopting it would freeze it in place with no submission behind it."""
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"awaiting bitcoin"
    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": data})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    old_bytes = _write_proof(canonical, _sha(data))  # PendingAttestation only

    def no_lookup(*a, **k):
        raise AssertionError("an unanchored proof needs no backend lookup")

    monkeypatch.setattr(ots, "verify", no_lookup)
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    # `incomplete` WITH a stamp time, so it stays visible to the stuck-proof report.
    assert fe.ots_state == "incomplete" and fe.ots_stamped_at is not None
    assert fe.ots_digest == _sha(data)
    assert _archive_files(Path(settings.proof_store_path), cid)[0].read_bytes() == old_bytes


async def test_a_disproven_proof_is_neither_adopted_nor_kept(cairn_env, monkeypatch):
    """2.34: the caller's `disproven` verdict must reach placement, or the forgery is simply kept.

    `_place_proof` is offline, so "complete" there means only *carries a Bitcoin attestation*. An
    implementation that ignored the verdict would keep the forgery, discard the real proof produced
    seconds earlier — and pass every other test in this file.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"target"
    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": data})
    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    forged = _write_proof(canonical, _sha(data), height=888_888)

    monkeypatch.setattr(ots, "verify", lambda *a, **k: _verify_result(proof_mismatch=True))
    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())

    assert await _stamp(cid) == 1
    assert canonical.read_bytes() != forged, "the canonical path still holds the disproven proof"
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "incomplete"
    assert fe.ots_digest == ots.read_proof_facts(canonical).digest
    assert _archive_files(Path(settings.proof_store_path), cid)[0].read_bytes() == forged


@pytest.mark.parametrize("prerecorded_provenance", [False, True])
async def test_an_unreachable_backend_defers_and_records_nothing(
    cairn_env, monkeypatch, prerecorded_provenance
):
    """2.35: an outage must buy neither a `complete` notarization nor the loss of a proof.

    Failing open (adopt, or keep-and-record-complete) makes a completed notarization purchasable by
    anyone who can take the backend offline — the cheapest attack on the list. Failing closed the
    other way (demote or discard the existing anchored proof) destroys probably-genuine evidence over
    a network blip. Deferral is the only branch that does neither, and recorded provenance must not
    change it: an outage plus provenance must not add up to a `complete`.
    """
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"steady bytes"
    root = cairn_env / "vault"
    kw = {"ots_digest": _sha(data)} if prerecorded_provenance else {}
    cid = await _seed(root, {"doc.txt": data}, **kw)
    settings = get_settings()
    store = Path(settings.proof_store_path)
    canonical = proofs.proof_path(settings, cid, "doc.txt")
    existing = _write_proof(canonical, _sha(data), height=808_080)

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    monkeypatch.setattr(
        ots,
        "verify",
        lambda *a, **k: _verify_result(transport_error="explorer unreachable", transport_failures=1),
    )

    assert await _stamp(cid) == 0, "a deferral is neither a stamp nor a failure"
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "pending" and fe.ots_path is None and fe.ots_stamped_at is None
    assert fe.ots_digest == (_sha(data) if prerecorded_provenance else None)
    assert canonical.read_bytes() == existing, "the existing anchored proof was demoted or replaced"
    # The proof produced during the outage is preserved, not discarded: same digest, so it lands in
    # that digest's archive family under its own slot.
    family = store / ".superseded" / str(cid) / _sha(data)[:2]
    assert [p.name for p in family.iterdir()] == [f"{_sha(data)}.ots"]

    # A later pass whose backend answers reaches a conclusive outcome with both artifacts on hand.
    monkeypatch.setattr(
        ots, "verify", lambda *a, **k: _verify_result(verified=True, block_height=808_080)
    )
    assert await _stamp(cid) == 1
    fe = await _entry(cid, "doc.txt")
    assert fe.ots_state == "complete" and fe.ots_digest == _sha(data)
    assert canonical.read_bytes() == existing


# ==============================================================================================
# 2.18 — the full #15 narrative, end to end
# ==============================================================================================


async def test_the_accept_restore_rescan_narrative_never_loses_the_original_proof(
    cairn_env, monkeypatch
):
    """2.18: stamp -> accept (the row, and its `ots_path`, are deleted) -> restore -> rescan -> stamp.

    This is #15's reachability story, through the product's own recommended workflow with no misuse:
    a mass "Accept all changes" after an unmounted volume used to re-stamp every returning file onto
    its own multi-year anchor. The original proof must still exist and be reachable afterwards, and
    verify must not report a proof made today.
    """
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import ots, proofs
    from src.services.scanner import accept_collection, scan_collection

    root = cairn_env / "vault"
    root.mkdir()
    data = b"a 2023 tax document"
    (root / "tax.pdf").write_bytes(data)
    cid = await seed_collection(root)

    monkeypatch.setattr(ots, "_run_ots", _stamp_fake())
    async with get_sessionmaker()() as s:
        await scan_collection(s, await s.get(Collection, cid))

    settings = get_settings()
    canonical = proofs.proof_path(settings, cid, "tax.pdf")
    # Stand in for "the calendars anchored it, years ago": the stored proof acquires a Bitcoin
    # attestation, which is exactly the evidence the overwrite used to destroy.
    original = _write_proof(canonical, _sha(data), height=770_000)

    # 1. the volume goes away, 2. the operator accepts the removals — which DELETES the row that
    # held `ots_path`, `sha256` and `first_seen`, while the `.ots` stays on disk.
    (root / "tax.pdf").unlink()
    async with get_sessionmaker()() as s:
        collection = await s.get(Collection, cid)
        await scan_collection(s, collection)
        await accept_collection(s, collection, collection.user_id)

    # 3. the volume comes back at the same path, 4. the rescan sees an unknown file -> `added`,
    # `pending`, 5. the stamp pass lands on the live proof.
    (root / "tax.pdf").write_bytes(data)
    monkeypatch.setattr(
        ots, "verify", lambda *a, **k: _verify_result(verified=True, block_height=770_000)
    )
    async with get_sessionmaker()() as s:
        await scan_collection(s, await s.get(Collection, cid))

    fe = await _entry(cid, "tax.pdf")
    assert canonical.read_bytes() == original, "the 2023 anchor was overwritten by today's proof"
    assert fe.ots_state == "complete"
    assert fe.ots_stamped_at is None, "an adopted proof must not claim a submission made today"
    assert ots.read_proof_facts(canonical).anchored is True


# ==============================================================================================
# 2.25 / 2.26 / 2.27 — the corroborated backfill in the daily upgrade pass
# ==============================================================================================


async def _upgrade(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services import proofs

    async with get_sessionmaker()() as s:
        return await proofs.upgrade_incomplete(s, await s.get(Collection, cid))


async def test_upgrade_backfills_provenance_when_the_baseline_corroborates_it(
    cairn_env, monkeypatch
):
    """2.25: the row's own recorded `sha256` is the corroborating witness, so this is not a lazy fill."""
    from src.config import get_settings
    from src.services import ots, proofs

    data = b"long-pending"
    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": data}, ots_state="incomplete")
    canonical = proofs.proof_path(get_settings(), cid, "doc.txt")
    _write_proof(canonical, _sha(data))

    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        fe.ots_path = str(canonical)
        await s.commit()

    monkeypatch.setattr(ots, "upgrade", lambda path: False)
    assert await _upgrade(cid) == {"upgraded": 0, "still_incomplete": 1}
    assert (await _entry(cid, "doc.txt")).ots_digest == _sha(data)


async def test_upgrade_never_launders_a_non_matching_proof_digest(cairn_env, monkeypatch, caplog):
    """2.26: the anti-laundering test. A proof committing to bytes Cairn never recorded for this
    file IS the corrupted/swapped/misfiled case the column exists to catch. Recording it would
    destroy the finding; the column must stay NULL and the operator must be told."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(root, {"doc.txt": b"the real bytes"}, ots_state="incomplete")
    canonical = proofs.proof_path(get_settings(), cid, "doc.txt")
    stranger = _sha(b"somebody else's bytes")
    _write_proof(canonical, stranger)

    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        fe = await s.scalar(select(FileEntry).where(FileEntry.collection_id == cid))
        fe.ots_path = str(canonical)
        await s.commit()

    monkeypatch.setattr(ots, "upgrade", lambda path: False)
    _unmute_cairn_loggers()
    with caplog.at_level(logging.WARNING, logger="cairn.proofs"):
        assert await _upgrade(cid) == {"upgraded": 0, "still_incomplete": 1}

    assert (await _entry(cid, "doc.txt")).ots_digest is None, "a swapped proof was laundered"
    messages = [r.getMessage() for r in caplog.records]
    assert any(stranger in m and _sha(b"the real bytes") in m for m in messages)
    assert any("cairn verify" in m for m in messages)


async def test_upgrade_leaves_recorded_provenance_and_unreadable_proofs_alone(
    cairn_env, monkeypatch
):
    """2.27: an already-recorded `ots_digest` is never rewritten — a later disagreement is verify's
    finding to report, not this pass's to overwrite — and an unreadable `.ots` leaves it NULL."""
    from src.config import get_settings
    from src.services import ots, proofs

    root = cairn_env / "vault"
    cid = await _seed(
        root, {"kept.txt": b"kept", "broken.txt": b"broken"}, ots_state="incomplete"
    )
    settings = get_settings()
    kept = proofs.proof_path(settings, cid, "kept.txt")
    broken = proofs.proof_path(settings, cid, "broken.txt")
    _write_proof(kept, _sha(b"drifted since"))  # parses to something else entirely
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"truncated garbage")

    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    async with get_sessionmaker()() as s:
        for fe in await s.scalars(select(FileEntry).where(FileEntry.collection_id == cid)):
            fe.ots_path = str(proofs.proof_path(settings, cid, fe.relpath))
            if fe.relpath == "kept.txt":
                fe.ots_digest = "aa" * 32
        await s.commit()

    monkeypatch.setattr(ots, "upgrade", lambda path: False)
    await _upgrade(cid)  # must not raise

    assert (await _entry(cid, "kept.txt")).ots_digest == "aa" * 32
    assert (await _entry(cid, "broken.txt")).ots_digest is None


# ==============================================================================================
# 2.23 / 2.23a / 2.23b — verify blame, on both surfaces
# ==============================================================================================


LIVE = _sha(b"hello")  # the on-disk bytes every blame fixture below uses


async def _seed_for_blame(
    root: Path,
    *,
    ots_digest: str | None,
    baseline: str = LIVE,
    status: str = "ok",
    ots_state: str = "complete",
) -> int:
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_bytes(b"hello")
    cid = await seed_collection(root)
    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(
            FileEntry(
                collection_id=cid,
                relpath="doc.txt",
                size=5,
                sha256=baseline,
                status=status,
                ots_state=ots_state,
                ots_path=str(root.parent / "p" / "doc.txt.ots"),
                ots_digest=ots_digest,
                ots_stamped_at=now,
                first_seen=now,
                last_checked=now,
            )
        )
        await s.commit()
    return cid


def _panel_blame(
    cairn_env, monkeypatch, *, ots_digest, result, status="ok", ots_state="complete"
):
    """POST /verify with `ots.verify` stubbed; return the rendered card."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app
    from src.services import ots as ots_svc

    asyncio.run(
        _seed_for_blame(
            cairn_env / "vault", ots_digest=ots_digest, status=status, ots_state=ots_state
        )
    )
    database.reset_engine()
    monkeypatch.setattr(ots_svc, "verify", lambda *a, **k: result)
    with TestClient(app) as client:
        token = re.search(
            r'name="csrf-token" content="([^"]+)"', client.get("/").text
        ).group(1)
        r = client.post("/verify", data={"csrf_token": token, "file_id": 1})
    assert r.status_code == 200, r.text
    return r.text


_CLI_BLAME_SEQ = iter(range(1, 100))


def _cli_blame(cairn_env, capsys, *, ots_digest, result, status="ok", ots_state="complete"):
    """Run `cairn verify` over a freshly seeded collection; return (rc, stdout+stderr).

    Each call seeds its OWN collection (the CLI refuses a bare `verify` once more than one exists),
    so several readings can be compared inside one test — which is the point: the panel and the
    command line must never disagree about which artifact is blamed.
    """
    import src.cli as cli
    from src import database
    from src.services import ots as ots_svc

    name = f"vault{next(_CLI_BLAME_SEQ)}"
    asyncio.run(
        _seed_for_blame(
            cairn_env / name, ots_digest=ots_digest, status=status, ots_state=ots_state
        )
    )
    database.reset_engine()
    real = ots_svc.verify
    ots_svc.verify = lambda *a, **k: result
    try:
        rc = cli.main(["verify", "doc.txt", "--collection", name])
    finally:
        ots_svc.verify = real
    captured = capsys.readouterr()
    return rc, captured.out + captured.err


def test_panel_blame_with_provenance_for_these_bytes(cairn_env, monkeypatch):
    """2.23a: `ots_digest == live` and the stored proof disagrees ⇒ ESTABLISHED proof blame."""
    html = _panel_blame(
        cairn_env,
        monkeypatch,
        ots_digest=LIVE,
        result=_verify_result(digest_mismatch=True, proof_digest="ff" * 32),
    )
    assert "This is not the proof Cairn placed" in html
    assert "not evidence against the file" in html
    assert "predates this version" not in html
    assert "Cairn cannot tell which" not in html


def test_panel_blame_established_stale_needs_the_proof_to_be_the_recorded_one(
    cairn_env, monkeypatch
):
    """2.23: `ots_digest != live` AND the parsed proof commits to the recorded one ⇒ `proof-stale`.

    And the card says only what that comparison establishes. Matching digests are NOT artifact
    identity — any `.ots` built over the same earlier bytes commits to the same digest, so a
    fabricated or unanchored proof dropped at this path passes the same test — and no attestation
    was validated: the ladder exits on the digest disagreement first. Claiming "this is the proof
    Cairn placed" or that it "keeps covering" the earlier version would launder exactly that
    substitution into a reassurance.
    """
    earlier = _sha(b"an earlier version")
    html = _panel_blame(
        cairn_env,
        monkeypatch,
        ots_digest=earlier,
        result=_verify_result(digest_mismatch=True, proof_digest=earlier),
    )
    assert "Proof commits to the previously recorded fingerprint" in html
    assert "commits to the fingerprint Cairn previously recorded for this file" in html
    assert "Bitcoin attestations were not validated here" in html
    assert "not evidence against the current file" in html
    assert "verdict--warn" in html and "verdict--danger" not in html
    # The claims the check did not establish, on either artifact:
    assert "the proof Cairn placed" not in html
    assert "keeps covering" not in html
    # No re-stamp is queued on this row, so the card must not claim one is.
    assert "a re-stamp is still pending" not in html
    assert "A re-stamp is queued" not in html


def test_panel_established_stale_takes_its_pending_clause_from_the_proof_state(
    cairn_env, monkeypatch
):
    """The pending clause is a fact about the ROW's proof state, not about its status.

    A `perfile` collection switched to `ots_mode="none"` after a modification sits at
    `status="modified"`, `ots_state="complete"` forever: nothing is queued and nothing ever will
    be. The status-derived heuristic promised that operator a re-stamp that will never run — on the
    page they opened to find out where their evidence stands.
    """
    earlier = _sha(b"an earlier version")
    stale = _verify_result(digest_mismatch=True, proof_digest=earlier)

    # Stamping disabled after the change: `modified`, but nothing queued.
    html = _panel_blame(
        cairn_env, monkeypatch, ots_digest=earlier, result=stale,
        status="modified", ots_state="complete",
    )
    assert "Proof commits to the previously recorded fingerprint" in html
    assert "A re-stamp is queued" not in html
    assert "Re-stamp the file if you want a proof over the bytes on disk today" in html


def test_panel_established_stale_does_claim_a_restamp_when_one_is_queued(cairn_env, monkeypatch):
    """The other half: `ots_state="pending"` is a queued re-stamp, and the card may say so."""
    earlier = _sha(b"an earlier version")
    html = _panel_blame(
        cairn_env, monkeypatch, ots_digest=earlier,
        result=_verify_result(digest_mismatch=True, proof_digest=earlier),
        status="modified", ots_state="pending",
    )
    assert "Proof commits to the previously recorded fingerprint" in html
    assert "A re-stamp is queued" in html


def test_panel_abc_case_is_not_reported_as_staleness(cairn_env, monkeypatch):
    """2.23a, the A/B/C case — the one a staleness-first ladder gets wrong.

    Recorded provenance `A`, live == baseline `B`, and the `.ots` on disk committing to a third
    digest `C`. `ots_digest != live` is true, so a ladder that reads staleness first says "simply an
    older proof of your file" about an artifact that is neither the recorded proof nor this file's
    proof. Staleness may only be concluded once the on-disk proof has been shown to BE the recorded
    proof.
    """
    html = _panel_blame(
        cairn_env,
        monkeypatch,
        ots_digest=_sha(b"A"),
        result=_verify_result(digest_mismatch=True, proof_digest=_sha(b"C")),
    )
    assert "This is not the proof Cairn placed" in html
    assert "predates this version" not in html


def test_panel_provenance_without_a_parsed_digest_falls_back_to_undecidable(
    cairn_env, monkeypatch
):
    """2.23b: nothing was parsed, so neither "swapped" nor "stale" is established — never staleness."""
    html = _panel_blame(
        cairn_env,
        monkeypatch,
        ots_digest=_sha(b"A"),
        result=_verify_result(digest_mismatch=True),  # no proof_digest
    )
    assert "Cairn cannot tell which" in html
    assert "predates this version" not in html
    assert "This is not the proof Cairn placed" not in html


def test_panel_legacy_null_provenance_keeps_sprint_ones_wording(cairn_env, monkeypatch):
    """2.23: a legacy row loses nothing it had — sprint 1's exact undecidable copy survives."""
    html = _panel_blame(
        cairn_env,
        monkeypatch,
        ots_digest=None,
        result=_verify_result(digest_mismatch=True, proof_digest="ff" * 32),
    )
    assert "Cairn cannot tell which without per-proof records" in html
    assert "This is not the proof Cairn placed" not in html


def test_cli_blame_matches_the_panel_on_all_three_readings(cairn_env, capsys):
    """2.23 / 2.23a / 2.23b for `cairn verify` — the two surfaces must never disagree."""
    earlier = _sha(b"an earlier version")

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=LIVE,
        result=_verify_result(digest_mismatch=True, proof_digest="ff" * 32),
    )
    assert rc == 1 and "THIS IS NOT THE PROOF CAIRN PLACED" in out
    assert "PROOF PREDATES THIS VERSION" not in out

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=earlier,
        result=_verify_result(digest_mismatch=True, proof_digest=earlier),
    )
    assert rc == 1 and "PROOF COMMITS TO THE PREVIOUSLY RECORDED FINGERPRINT" in out
    assert "Bitcoin attestations were not validated here" in out
    assert "NOT evidence against the current file" in out
    # The identity and validity claims the digest comparison does not support:
    assert "the proof Cairn placed for an earlier version" not in out
    assert "PROOF PREDATES THIS VERSION" not in out
    assert "a re-stamp is queued" not in out

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=None,
        result=_verify_result(digest_mismatch=True, proof_digest="ff" * 32),
    )
    assert rc == 1 and "Cairn cannot tell which without per-proof records" in out


def test_cli_abc_case_and_missing_parsed_digest(cairn_env, capsys):
    """2.23a / 2.23b on the command line."""
    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=_sha(b"A"),
        result=_verify_result(digest_mismatch=True, proof_digest=_sha(b"C")),
    )
    assert rc == 1 and "THIS IS NOT THE PROOF CAIRN PLACED" in out
    assert "PROOF PREDATES THIS VERSION" not in out

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=_sha(b"A"),
        result=_verify_result(digest_mismatch=True),
    )
    assert rc == 1 and "PROOF DOES NOT MATCH THIS FILE" in out
    assert "PROOF PREDATES THIS VERSION" not in out


def test_cli_established_stale_takes_its_pending_clause_from_the_proof_state(cairn_env, capsys):
    """Mirrors the panel: `modified` + nothing queued must not promise a re-stamp."""
    earlier = _sha(b"an earlier version")
    stale = _verify_result(digest_mismatch=True, proof_digest=earlier)

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=earlier, result=stale,
        status="modified", ots_state="complete",
    )
    assert rc == 1 and "PROOF COMMITS TO THE PREVIOUSLY RECORDED FINGERPRINT" in out
    assert "a re-stamp is queued" not in out

    rc, out = _cli_blame(
        cairn_env, capsys, ots_digest=earlier, result=stale,
        status="modified", ots_state="pending",
    )
    assert rc == 1 and "PROOF COMMITS TO THE PREVIOUSLY RECORDED FINGERPRINT" in out
    assert "a re-stamp is queued" in out
