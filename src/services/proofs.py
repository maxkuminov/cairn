"""Proof store layout + the stamp/upgrade/verify/export lifecycle (DESIGN.md §5/§6).

Proofs live on the writable volume laid out parallel to the collection:
``<proof_store>/<collection_id>/<relpath>.ots``. Nothing is ever written under a collection root — the
watched mounts are read-only. Stamping goes through a transient symlink in
``<proof_store>/.staging`` (see :func:`src.services.ots.stamp_via_symlink`).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..models.db import Collection, FileEntry, Run
from . import collections, ots
from .scanner import _utcnow

log = logging.getLogger("cairn.proofs")

# Called with the cumulative count handled so far, after each batch / proof, so a caller can
# persist live progress onto a run row that a concurrent reader (the status badge) observes.
ProgressCb = Callable[[int], Awaitable[None]]


def proof_path(settings: Settings, collection_id: int, relpath: str) -> Path:
    """Return the ``.ots`` path for a file: ``<proof_store>/<collection_id>/<relpath>.ots``."""
    base = Path(settings.proof_store_path) / str(collection_id)
    return base / (relpath + ".ots")


def staging_dir(settings: Settings) -> Path:
    """Return the transient stamp symlink directory: ``<proof_store>/.staging``."""
    return Path(settings.proof_store_path) / ".staging"


async def stamp_pending(
    session: AsyncSession,
    collection: Collection,
    settings: Settings | None = None,
    *,
    progress: ProgressCb | None = None,
) -> int:
    """Stamp every ``pending`` file in this collection in batches; return the count stamped.

    Pending rows are chunked into groups of ``ots_stamp_batch_size`` and each group is stamped in a
    single ``ots stamp`` call (one calendar round-trip → one independent ``.ots`` per file). For any
    batch member that produced no proof — whole-batch failure, timeout, or one bad file aborting the
    run — we fall back to the single-file :func:`ots.stamp_via_symlink`; members that still fail are
    left ``pending`` (retried next pass) and logged. A stamp failure never aborts the scan. Files
    that vanished between scan and stamp are skipped and stay ``pending`` for reclassification.

    ``progress`` (when given) is awaited after each batch with the cumulative stamped count, so an
    on-demand backfill can persist live progress onto its ``kind='stamp'`` run.
    """
    settings = settings or get_settings()
    pending = list(
        await session.scalars(
            select(FileEntry).where(
                FileEntry.collection_id == collection.id, FileEntry.ots_state == "pending"
            )
        )
    )
    if not pending:
        return 0

    root = Path(collection.root)
    staging = staging_dir(settings)
    calendars = settings.ots_calendars

    # Only stamp files still on disk; a vanished file stays pending for the next scan to reclassify.
    work: list[tuple[FileEntry, Path, Path]] = []
    for entry in pending:
        real = root / entry.relpath
        if not real.is_file():
            log.warning("skip stamp, file missing: %s", real)
            continue
        work.append((entry, real, proof_path(settings, collection.id, entry.relpath)))

    batch_size = max(1, settings.ots_stamp_batch_size)
    now = _utcnow()
    stamped = 0
    skipped = 0  # files whose proof path can never be written (e.g. name past the FS byte limit)
    for start in range(0, len(work), batch_size):
        chunk = work[start : start + batch_size]
        # Offload the blocking `ots` subprocess (process spawn + calendar round-trip) to a worker
        # thread so the event loop stays free to serve the panel — mirrors scanner hashing.
        outcomes = await asyncio.to_thread(
            ots.stamp_batch_via_symlink,
            [(real, out) for _entry, real, out in chunk],
            calendars,
            staging,
        )
        for (entry, real, out), ok in zip(chunk, outcomes):
            if not ok:
                # Isolate the failure: retry just this file on its own before giving up on it.
                try:
                    await asyncio.to_thread(
                        ots.stamp_via_symlink, real, out, calendars, staging
                    )
                except ots.OtsPathError as exc:
                    # The proof output path can never be written (typically ENAMETOOLONG — a
                    # multi-byte name plus ``.ots`` past the filesystem's per-name byte limit). Skip
                    # it and drop it out of `pending` to `none` so a normal scan does not re-queue and
                    # re-fail it every pass (a bad file used to abort the whole batch and re-run the
                    # tree). It is left unstamped-and-untracked-for-proof, exactly like an
                    # un-storable-path skip in the scanner; a `stamp --all` can retry it cheaply.
                    log.warning("skip stamp, unwritable proof path for %s: %s", real, exc)
                    entry.ots_state = "none"
                    entry.ots_path = None  # no proof stored; never leave a stale pointer behind
                    skipped += 1
                    continue
                except ots.OtsError as exc:
                    log.warning("stamp failed for %s: %s", real, exc)
                    continue
            entry.ots_path = str(out)
            entry.ots_state = "incomplete"
            entry.ots_stamped_at = now
            stamped += 1
        if progress is not None:
            # Persist progress per batch (the callback commits) so the badge advances live.
            await progress(stamped)

    if skipped:
        log.warning(
            "collection %s: skipped %d file(s) with an unwritable proof path (set ots_state=none)",
            collection.id,
            skipped,
        )
    await session.commit()
    return stamped


async def mark_unstamped_pending(session: AsyncSession, collection: Collection) -> int:
    """Queue every currently-unstamped, present file in ``collection`` for stamping; return the count.

    Sets ``ots_state='pending'`` for files with ``ots_state='none'`` and ``status != 'missing'`` —
    the on-demand backfill that lets an operator stamp a pre-existing baseline. Files that already
    hold a proof (``incomplete`` or ``complete``) are left untouched, so this never re-stamps work
    that is already done. Pair it with :func:`stamp_pending` to actually take the stamps.
    """
    result = await session.execute(
        update(FileEntry)
        .where(
            FileEntry.collection_id == collection.id,
            FileEntry.ots_state == "none",
            FileEntry.status != "missing",
        )
        .values(ots_state="pending")
    )
    await session.commit()
    return result.rowcount or 0


async def run_stamp_backfill(
    session: AsyncSession, collection: Collection, settings: Settings | None = None
) -> Run:
    """On-demand "Stamp all" backfill recorded as a typed ``kind='stamp'`` run with live progress.

    Queues the `none`-state baseline (:func:`mark_unstamped_pending`), opens a ``running`` stamp run
    whose ``total`` is the number of files now pending (the work it will do — known up front, so the
    badge is exact), then stamps them via the batched :func:`stamp_pending`, advancing ``processed``
    per batch. A stamp failure can never propagate: it is recorded as ``result='error'`` on the run.
    Returns the finalized run. ``kind='stamp'`` runs never count toward scan freshness.
    """
    settings = settings or get_settings()
    await mark_unstamped_pending(session, collection)
    total = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .where(FileEntry.collection_id == collection.id, FileEntry.ots_state == "pending")
    )
    run = Run(
        collection_id=collection.id,
        kind="stamp",
        started=_utcnow(),
        result="running",
        total=int(total or 0),
    )
    # Atomically claim the collection's single in-progress slot (partial unique index on a `running`
    # run) so a concurrent scan/stamp can't run a second writer over the same collection. A lost claim
    # means an op is already in flight — refuse this backfill rather than starting it.
    if await collections.claim_run(session, run) is None:
        log.info("stamp backfill refused for collection %s — another operation already claimed it", collection.id)
        return run

    async def _progress(done: int) -> None:
        run.processed = done
        await session.commit()

    try:
        stamped = await stamp_pending(session, collection, settings, progress=_progress)
        run.result = "ok"
    except Exception:  # pragma: no cover - stamping must never fail the operation
        log.exception("stamp backfill failed for collection %s", collection.id)
        stamped = 0
        run.result = "error"
    run.stamped = stamped
    run.processed = stamped
    run.finished = _utcnow()
    await session.commit()
    return run


async def upgrade_incomplete(
    session: AsyncSession,
    collection: Collection | None = None,
    settings: Settings | None = None,
    *,
    progress: ProgressCb | None = None,
) -> dict[str, int]:
    """Upgrade ``incomplete`` proofs (optionally scoped to one collection) after Bitcoin confirms.

    Returns ``{"upgraded": n, "still_incomplete": m}``. A still-pending proof is not an error and
    simply stays ``incomplete``.

    ``progress`` (when given) is awaited after each proof with the cumulative count examined, so the
    daily pass can persist live progress onto its ``kind='upgrade'`` run.
    """
    settings = settings or get_settings()
    stmt = select(FileEntry).where(FileEntry.ots_state == "incomplete")
    if collection is not None:
        stmt = stmt.where(FileEntry.collection_id == collection.id)
    files = list(await session.scalars(stmt))

    upgraded = still = processed = 0
    for entry in files:
        processed += 1
        complete = False
        if not entry.ots_path or not Path(entry.ots_path).exists():
            log.warning("incomplete proof has no .ots on disk: %s", entry.ots_path)
        else:
            try:
                # Off the event loop: `ots upgrade` spawns a process and contacts the calendars.
                complete = await asyncio.to_thread(ots.upgrade, entry.ots_path)
            except ots.OtsError as exc:
                log.warning("upgrade failed for %s: %s", entry.ots_path, exc)
        if complete:
            entry.ots_state = "complete"
            upgraded += 1
        else:
            still += 1
        if progress is not None:
            await progress(processed)

    await session.commit()
    return {"upgraded": upgraded, "still_incomplete": still}


def export_bundle(file_entry: FileEntry, dest_dir: str | Path, collection_root: str | Path) -> Path:
    """Copy a stamped file and its ``.ots`` into ``dest_dir`` for independent verification.

    Writes ``<basename>`` and ``<basename>.ots``. Raises ``FileNotFoundError`` if the file has no
    stored proof or the source bytes are unreadable. Returns the path to the copied file.
    """
    if not file_entry.ots_path:
        raise FileNotFoundError(
            f"no proof stored for {file_entry.relpath!r}; stamp it before exporting"
        )
    ots_src = Path(file_entry.ots_path)
    if not ots_src.exists():
        raise FileNotFoundError(f"proof missing on disk: {ots_src}")

    source = Path(collection_root) / file_entry.relpath
    if not source.is_file():
        raise FileNotFoundError(f"source file missing: {source}")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    basename = Path(file_entry.relpath).name
    dest_file = dest_dir / basename
    dest_ots = dest_dir / (basename + ".ots")
    shutil.copy2(source, dest_file)
    shutil.copy2(ots_src, dest_ots)
    return dest_file


async def stale_incomplete(
    session: AsyncSession, days: int, collection: Collection | None = None
) -> list[FileEntry]:
    """List proofs stuck ``incomplete`` longer than ``days`` (e.g. never confirmed by Bitcoin)."""
    cutoff = _utcnow() - timedelta(days=days)
    stmt = select(FileEntry).where(
        FileEntry.ots_state == "incomplete",
        FileEntry.ots_stamped_at.is_not(None),
        FileEntry.ots_stamped_at < cutoff,
    )
    if collection is not None:
        stmt = stmt.where(FileEntry.collection_id == collection.id)
    return list(await session.scalars(stmt))
