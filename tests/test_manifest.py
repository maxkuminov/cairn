"""Manifest import: baseline load, no-stamp-after-import, idempotency, parser, re-hash.

The importer loads a legacy photo-tripwire ``manifest.tsv`` as a pre-existing, UNSTAMPED baseline:
every row becomes a ``status='ok'`` / ``ots_state='none'`` file with the manifest's SHA-256 and no
``added`` event. A subsequent scan therefore never stamps imported files, while a genuinely new
file (not in the manifest) is classified ``added`` and stamped in a perfile collection.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_manifest.py``
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from src.services.scanner import sha256_file


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


async def _files(session, cid: int):
    from src.models.db import FileEntry

    return list(await session.scalars(select(FileEntry).where(FileEntry.collection_id == cid)))


async def _events(session, cid: int, kind: str | None = None):
    from src.models.db import Event

    stmt = select(Event).where(Event.collection_id == cid)
    if kind:
        stmt = stmt.where(Event.kind == kind)
    return list(await session.scalars(stmt))


def _write_tab_manifest(path: Path, entries: list[tuple[str, str]]) -> None:
    """Write a ``relpath\\tsha256`` manifest."""
    path.write_text("".join(f"{relpath}\t{sha}\n" for relpath, sha in entries))


# --- Import: an OK baseline with no events --------------------------------------------------


async def test_import_creates_ok_baseline_with_no_events(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.manifest import import_manifest

    root = cairn_env / "photos"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    sha_a = sha256_file(root / "a.txt")
    sha_b = sha256_file(root / "b.txt")
    manifest = cairn_env / "manifest.tsv"
    _write_tab_manifest(manifest, [("a.txt", sha_a), ("b.txt", sha_b)])

    cid = await _make_collection(root, mode="worm", ots_mode="none")
    sm = get_sessionmaker()

    async with sm() as s:
        result = await import_manifest(s, await s.get(Collection, cid), manifest)
        assert result.imported == 2
        assert result.updated == 0
        assert result.skipped == 0

    async with sm() as s:
        files = {f.relpath: f for f in await _files(s, cid)}
        assert set(files) == {"a.txt", "b.txt"}
        for f in files.values():
            assert f.status == "ok"
            assert f.ots_state == "none"
            assert f.first_seen is not None
            assert f.last_checked is not None
        assert files["a.txt"].sha256 == sha_a
        assert files["b.txt"].sha256 == sha_b
        # The defining invariant: a baseline import writes ZERO added events.
        assert await _events(s, cid) == []


# --- No stamping after import; genuinely-new files ARE stamped ------------------------------


async def test_imported_files_not_stamped_new_file_is(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection, Run
    from src.services import ots
    from src.services.manifest import import_manifest
    from src.services.scanner import scan_collection

    root = cairn_env / "perfile"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    sha_a = sha256_file(root / "a.txt")
    sha_b = sha256_file(root / "b.txt")
    manifest = cairn_env / "manifest.tsv"
    _write_tab_manifest(manifest, [("a.txt", sha_a), ("b.txt", sha_b)])

    cid = await _make_collection(root, mode="worm", ots_mode="perfile")
    sm = get_sessionmaker()

    async with sm() as s:
        await import_manifest(s, await s.get(Collection, cid), manifest)

    # Mock stamp: write the .ots beside the staged symlink, like the real CLI.
    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        link = Path(args[-1])
        link.with_name(link.name + ".ots").write_bytes(b"proof")
        return 0, "", ""

    monkeypatch.setattr(ots, "_run_ots", fake_run)

    # A brand-new file NOT in the manifest is added after the import.
    (root / "c.txt").write_text("charlie")

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 1  # only c.txt is first-seen
        files = {f.relpath: f for f in await _files(s, cid)}
        # Imported files stay an unstamped baseline.
        assert files["a.txt"].ots_state == "none"
        assert files["b.txt"].ots_state == "none"
        assert files["a.txt"].ots_path is None
        assert files["b.txt"].ots_path is None
        # The genuinely-new file is queued and stamped.
        assert files["c.txt"].ots_state == "incomplete"
        run = await s.scalar(select(Run).where(Run.collection_id == cid))
        assert run.stamped == 1


# --- Idempotent re-import --------------------------------------------------------------------


async def test_reimport_is_idempotent(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.manifest import import_manifest

    root = cairn_env / "idem"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    manifest = cairn_env / "manifest.tsv"
    _write_tab_manifest(
        manifest,
        [("a.txt", sha256_file(root / "a.txt")), ("b.txt", sha256_file(root / "b.txt"))],
    )

    cid = await _make_collection(root, mode="worm", ots_mode="none")
    sm = get_sessionmaker()

    async with sm() as s:
        first = await import_manifest(s, await s.get(Collection, cid), manifest)
        assert first.imported == 2 and first.updated == 0

    async with sm() as s:
        second = await import_manifest(s, await s.get(Collection, cid), manifest)
        assert second.imported == 0
        assert second.updated == 2

    async with sm() as s:
        # No duplicate rows — still exactly two.
        assert len(await _files(s, cid)) == 2


# --- Parser tolerance ------------------------------------------------------------------------


def test_parser_handles_tab_sha256sum_and_skips_malformed():
    from src.services.manifest import parse_manifest

    sha = "a" * 64
    sha2 = "b" * 64
    text = (
        f"path/one.jpg\t{sha}\n"  # tab form: relpath then hash
        "\n"  # blank line ignored, not counted
        "# a comment line\n"  # comment ignored, not counted
        f"{sha2}  path/two.jpg\n"  # sha256sum form: <hash>  <path>
        "garbage line without any hash\n"  # malformed: no sha256 -> skipped + counted
    )
    rows, skipped = parse_manifest(text)

    by_path = {r.relpath: r.sha256 for r in rows}
    assert by_path == {"path/one.jpg": sha, "path/two.jpg": sha2}
    assert skipped == 1


def test_parser_reads_optional_size_and_mtime():
    from src.services.manifest import parse_manifest

    sha = "c" * 64
    # relpath \t size \t mtime \t sha256 (size is the larger integer, mtime the epoch-ish one).
    rows, skipped = parse_manifest(f"photo.jpg\t5242880\t1700000000\t{sha}\n")
    assert skipped == 0
    assert len(rows) == 1
    row = rows[0]
    assert row.relpath == "photo.jpg"
    assert row.sha256 == sha
    assert row.size == 5242880
    assert row.mtime == 1700000000.0


# --- Re-hash trust check ---------------------------------------------------------------------


async def test_rehash_reports_tampered_file(cairn_env):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.manifest import import_manifest

    root = cairn_env / "rehash"
    root.mkdir()
    (root / "a.txt").write_text("alpha")
    (root / "b.txt").write_text("beta")
    manifest = cairn_env / "manifest.tsv"
    sha_a = sha256_file(root / "a.txt")
    sha_b = sha256_file(root / "b.txt")
    _write_tab_manifest(manifest, [("a.txt", sha_a), ("b.txt", sha_b)])

    # Tamper a.txt AFTER writing the manifest, so the manifest hash no longer matches the bytes.
    (root / "a.txt").write_text("alpha-tampered-and-longer")

    cid = await _make_collection(root, mode="worm", ots_mode="none")
    sm = get_sessionmaker()

    async with sm() as s:
        result = await import_manifest(s, await s.get(Collection, cid), manifest, rehash=True)
        # The intact row imports; the tampered row is skipped rather than seeded with the stale
        # manifest hash (issue #7 — a baseline row whose stored hash != the bytes is a silent
        # integrity gap; the scanner will classify it as `added` on the next scan instead).
        assert result.imported == 1
        assert result.skipped == 1
        assert result.missing == []
        # The tampered file is still reported as a mismatch (relpath, manifest_hash, actual_hash).
        assert len(result.mismatches) == 1
        relpath, manifest_hash, actual_hash = result.mismatches[0]
        assert relpath == "a.txt"
        assert manifest_hash == sha_a
        assert actual_hash == sha256_file(root / "a.txt")
        assert actual_hash != sha_a

    async with sm() as s:
        # The mismatched file is NOT persisted (no row trusting the wrong hash); the intact one is.
        files = {f.relpath: f for f in await _files(s, cid)}
        assert "a.txt" not in files
        assert files["b.txt"].status == "ok"
        assert files["b.txt"].ots_state == "none"


def test_parser_handles_legacy_tripwire_format():
    """``relpath <TAB> size(int) <TAB> mtime(float) <TAB> sha256`` — the real bash-tripwire layout.

    Regression for the deploy: a SHORT filename must not lose the longest-field tie-break to the
    21-char mtime float, and the high-precision epoch float must be captured as mtime (not dropped,
    which would force a full re-hash on the first scan).
    """
    from src.services.manifest import parse_manifest

    sha = "8cea05853e56ea22833b3b011cd2dab472b351dbfba212e62f6a566e0585ca8f"
    text = (
        f"20250223_161622.jpg\t6536053\t1740345451.8128441110\t{sha}\n"
        f"Alice - modeling agency/4x6.jpg\t1629323\t1418076796.5799910000\t{'b' * 64}\n"
    )
    rows, skipped = parse_manifest(text)
    assert skipped == 0
    assert len(rows) == 2
    first = rows[0]
    assert first.relpath == "20250223_161622.jpg"  # not the mtime float
    assert first.size == 6536053
    assert first.mtime is not None and abs(first.mtime - 1740345451.8128) < 1.0
    assert first.sha256 == sha
    assert rows[1].relpath == "Alice - modeling agency/4x6.jpg"
