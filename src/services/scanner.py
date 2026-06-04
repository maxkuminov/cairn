"""The integrity scanner: walk → diff → hash → classify → events + run.

The scanner is the single writer to SQLite. It fast-paths on size+mtime and only streams a
SHA-256 for new/changed files, so steady-state scans of huge collections stay cheap. Per DESIGN.md
§5 (per-run flow) and §8 (nag-until-accept lifecycle).

A *deep* scan (``scan_collection(..., deep=True)``) bypasses that fast-path and re-hashes every
tracked file, so silent bit-rot — bytes that change while size and mtime stay identical — is
detected. Classification is otherwise identical: an intact file stays ``ok`` (and is never
re-stamped), a genuinely changed file nags/​re-baselines exactly as a normal scan would. The
scheduler runs a deep pass on each collection's ``verify_cadence_seconds``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, Event, FileEntry, Run

CHUNK = 1 << 20  # 1 MiB streamed-hash chunk
BATCH = 500  # files per DB commit
ALARM_PATH_CAP = 20  # max relpaths carried into a batched alert


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    """Stream a file through SHA-256 without loading it into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


async def _hash(path: Path) -> str:
    # Keep the event loop free while hashing (scheduler drives this later).
    return await asyncio.to_thread(sha256_file, path)


def _db_storable(relpath: str) -> bool:
    """Whether ``relpath`` can be stored as SQLite TEXT (and thus tracked at all).

    ``os.walk`` decodes a non-UTF-8 on-disk name via ``surrogateescape`` into lone surrogate
    characters (``\\udcXX``). Filesystem ops (stat/open/hash) accept those, but Python's ``sqlite3``
    binds ``str`` as plain UTF-8 and a lone surrogate is not encodable — the row write raises
    ``UnicodeEncodeError``. Such a path cannot be tracked, so the scanner skips it (counted + logged)
    rather than let one hostile filename abort the whole scan.
    """
    try:
        relpath.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _excluded(relpath: str, globs: list[str]) -> bool:
    if not globs:
        return False
    name = relpath.rsplit("/", 1)[-1]
    parts = relpath.split("/")
    for raw in globs:
        g = raw.rstrip("/")
        if fnmatch.fnmatch(relpath, raw) or fnmatch.fnmatch(name, g):
            return True
        if any(fnmatch.fnmatch(part, g) for part in parts):
            return True
    return False


def iter_relpaths(root: Path, globs: list[str]):
    """Yield POSIX relpaths of files under root, pruning excluded dirs; no symlink following."""
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        # Prune excluded subdirectories in place.
        kept = []
        for d in dirnames:
            child = d if not rel_dir else f"{rel_dir}/{d}"
            if not _excluded(child, globs):
                kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            child = fn if not rel_dir else f"{rel_dir}/{fn}"
            if not _excluded(child, globs):
                yield child


@dataclass
class RunSummary:
    collection_id: int
    added: int = 0
    modified: int = 0
    missing: int = 0
    moved: int = 0
    restored: int = 0
    ok: int = 0
    errors: int = 0
    # Intact `new` files promoted to `ok` by the deep pass (auto_baseline_new). Informational only.
    baselined: int = 0
    result: str = "ok"
    # Alarming events newly created THIS run — (kind, relpath), capped at ALARM_PATH_CAP. Only
    # `missing` (any mode) and `modified` (WORM) accumulate here; `added` and churn re-baselines
    # do not. The post-commit dispatch hook turns a non-empty list into a single batched alert.
    alarming: list[tuple[str, str]] = field(default_factory=list)


async def _reconcile_moves(
    session: AsyncSession,
    collection: Collection,
    new_rows: list[FileEntry],
    newly_missing: list[FileEntry],
    now: datetime,
    summary: RunSummary,
) -> set[int]:
    """Content-address moved/renamed files within one scan; return reconciled missing-row ids.

    A file newly classified ``missing`` whose stored ``(sha256, size)`` matches **exactly one**
    newly-``added`` file — a key shared by no other missing or added row in this run — is the same
    file relocated, not an independent deletion + addition. Such a pair is reconciled into a single
    surviving row that keeps its identity (``first_seen``, ``sha256``, OTS proof) and is repointed
    to the new path with status ``ok``; the added row is dropped and one informational ``moved``
    event records the old → new path. Ambiguous keys (matching >1 candidate on either side) and
    zero-byte files never reconcile — they fall back to plain ``missing`` + ``added`` (logged).

    Mutates the index only (never collection bytes / proof files) and never re-queues a move for OTS
    stamping (the surviving row stays ``ok``, not ``pending``).
    """
    log = logging.getLogger("cairn.scanner")
    if not newly_missing or not new_rows:
        return set()

    # Index candidate-missing rows by content key; skip zero-byte and hash-less rows (a zero-byte
    # hash is shared by every empty file, so it can never be an unambiguous 1:1 match).
    missing_by_key: dict[tuple[str, int], list[FileEntry]] = {}
    for m in newly_missing:
        if m.size and m.sha256:
            missing_by_key.setdefault((m.sha256, m.size), []).append(m)
    if not missing_by_key:
        return set()

    # Index only the added rows whose key has a missing counterpart (bounds work on initial scans
    # where everything is added and nothing is missing).
    added_by_key: dict[tuple[str, int], list[FileEntry]] = {}
    for a in new_rows:
        key = (a.sha256, a.size)
        if a.size and a.sha256 and key in missing_by_key:
            added_by_key.setdefault(key, []).append(a)

    matches: list[tuple[FileEntry, FileEntry]] = []
    for key, m_list in missing_by_key.items():
        a_list = added_by_key.get(key, [])
        if not a_list:
            continue  # no added counterpart → genuine deletion
        if len(m_list) == 1 and len(a_list) == 1:
            matches.append((m_list[0], a_list[0]))
        else:
            # Shared by more than one candidate on a side → target is ambiguous; do not guess.
            log.info(
                "move reconciliation skipped (ambiguous) for collection %s: "
                "%d missing + %d added share content %s (size %d) — kept as missing+added",
                collection.id,
                len(m_list),
                len(a_list),
                key[0][:12],
                key[1],
            )
    if not matches:
        return set()

    # Drop the added rows first so their (collection_id, relpath) is free before a surviving row
    # claims it — UNIQUE(collection_id, relpath) would otherwise collide. Flush the deletes before
    # repointing. Each added row's just-written `added` event is removed by FK cascade.
    captured = [(m, m.relpath, a.relpath, a.mtime) for m, a in matches]
    for _m, a in matches:
        await session.delete(a)
    await session.flush()

    reconciled_ids: set[int] = set()
    for m, old_rel, new_rel, new_mtime in captured:
        m.relpath = new_rel
        m.mtime = new_mtime  # adopt the new file's mtime so the next fast-path scan skips re-hash
        m.status = "ok"
        m.last_checked = now
        # Preserve first_seen / sha256 / ots_path / ots_state / ots_stamped_at — identity, proof,
        # and notarization history follow the file to its new path.
        session.add(
            Event(
                collection_id=collection.id,
                file_id=m.id,
                kind="moved",
                detail=f"{old_rel} → {new_rel}",
                detected_at=now,
                # Informational (like `added`/`restored`): born acknowledged so it never nags.
                acknowledged_at=now,
                acknowledged_by=None,
            )
        )
        # This pair was counted as one `added` during the walk; it is a move, not an addition.
        summary.added -= 1
        summary.moved += 1
        reconciled_ids.add(m.id)
        log.info("move reconciled for collection %s: %s → %s", collection.id, old_rel, new_rel)

    return reconciled_ids


async def scan_collection(
    session: AsyncSession, collection: Collection, *, deep: bool = False
) -> RunSummary:
    """Scan one collection, writing files/events and a runs row. Never raises on per-file errors.

    When ``deep`` is True every tracked, non-missing file is re-hashed regardless of its size and
    mtime (catching silent bit-rot the fast-path skips); classification of the result is unchanged.
    """
    root = Path(collection.root)
    globs = json.loads(collection.exclude_globs_json or "[]")
    now = _utcnow()
    summary = RunSummary(collection_id=collection.id)
    perfile = collection.ots_mode == "perfile"

    # Progress estimate: the last completed scan's processed count is the best guess of how many
    # files this walk will cover. A first scan (or the first after the 0006 migration, where older
    # runs read processed=0) has no estimate → total stays NULL → indeterminate progress. We do NOT
    # use a live count(*) FROM files: a first scan inserts rows as it walks, so processed≈file_count
    # would read a false ~100%.
    prior_processed = await session.scalar(
        select(Run.processed)
        .where(
            Run.collection_id == collection.id,
            Run.kind == "scan",
            Run.result.in_(("ok", "partial")),
        )
        .order_by(Run.started.desc())
        .limit(1)
    )
    run = Run(
        collection_id=collection.id,
        kind="scan",
        started=now,
        result="running",
        deep=deep,
        total=prior_processed if prior_processed else None,
    )
    # Atomically claim the collection's single in-progress slot. This commits the running run up front
    # so the concurrency guard (a manual op vs. the scheduler) and the live status badge observe it
    # immediately — and the partial unique index makes the claim race-free: a near-simultaneous
    # second scan loses the claim (IntegrityError → claim_run returns None) and is refused here
    # rather than running a second writer over a half-mutated index.
    from . import collections

    if await collections.claim_run(session, run) is None:
        logging.getLogger("cairn.scanner").info(
            "scan refused for collection %s — another operation already claimed it", collection.id
        )
        summary.result = "skipped"
        return summary
    # Capture the id now (expire_on_commit=False keeps it populated): a later rollback expires the
    # ORM object, and the terminal-state fallback must reference the run by id without a lazy load.
    run_id = run.id

    existing: dict[str, FileEntry] = {
        f.relpath: f
        for f in await session.scalars(
            select(FileEntry).where(FileEntry.collection_id == collection.id)
        )
    }
    seen: set[str] = set()
    added_buffer: list[FileEntry] = []
    # Every row this scan creates (status 'new'), retained across batch drains so the post-walk
    # move/rename pass can correlate them with files that went missing in the same run.
    new_rows: list[FileEntry] = []
    processed = 0
    # Files whose names are not valid UTF-8 (lone surrogates from surrogateescape) cannot be stored
    # as SQLite TEXT, so they are skipped rather than allowed to poison a batch commit. Count all of
    # them (folded into the run's errors → `partial`) and keep a capped sample of the raw bytes for
    # one summary WARNING so the operator can find them.
    skipped_unstorable = 0
    unstorable_sample: list[bytes] = []

    def _record_alarm(kind: str, relpath: str) -> None:
        if len(summary.alarming) < ALARM_PATH_CAP:
            summary.alarming.append((kind, relpath))

    async def _drain() -> None:
        nonlocal added_buffer
        await session.flush()  # assign ids to freshly-added FileEntry rows
        for obj in added_buffer:
            # `added` is informational, not a nag: born acknowledged (system ack, no user) so a
            # routine new file never inflates the dashboard's "needs action" count.
            session.add(
                Event(
                    collection_id=collection.id,
                    file_id=obj.id,
                    kind="added",
                    detected_at=now,
                    acknowledged_at=now,
                    acknowledged_by=None,
                )
            )
        added_buffer = []
        run.processed = processed  # persist live progress for the status badge
        await session.commit()

    try:
        for relpath in iter_relpaths(root, globs):
            if not _db_storable(relpath):
                # Non-UTF-8 name: it cannot be tracked (SQLite TEXT can't bind a lone surrogate).
                # Skip before any row is created so one bad name can't abort the scan. Not added to
                # `seen` and never stored, so it also never reads as missing/added on a later scan.
                summary.errors += 1
                skipped_unstorable += 1
                if len(unstorable_sample) < ALARM_PATH_CAP:
                    unstorable_sample.append(os.fsencode(relpath))
                continue
            full = root / relpath
            if full.is_symlink():
                continue  # conservative: never follow symlinks out of the read-only jail
            try:
                st = full.stat()
            except OSError:
                summary.errors += 1
                continue
            seen.add(relpath)
            size = st.st_size
            mtime = st.st_mtime
            row = existing.get(relpath)

            try:
                if row is None:
                    sha = await _hash(full)
                    row = FileEntry(
                        collection_id=collection.id,
                        relpath=relpath,
                        size=size,
                        mtime=mtime,
                        sha256=sha,
                        status="new",
                        first_seen=now,
                        last_checked=now,
                        last_changed=now,
                        # perfile collections queue first-seen files for stamping (a 'none' collection
                        # is tripwire-only and must stay ots_state='none').
                        ots_state="pending" if perfile else "none",
                    )
                    session.add(row)
                    added_buffer.append(row)
                    new_rows.append(row)
                    summary.added += 1
                elif row.status == "missing":
                    # Reappeared after being recorded missing.
                    row.sha256 = await _hash(full)
                    row.size, row.mtime = size, mtime
                    row.status = "ok"
                    row.last_checked = row.last_changed = now
                    # `restored` is informational (a missing file came back, the benign
                    # direction): born acknowledged like `added`, so it stays in the feed without
                    # nagging.
                    session.add(
                        Event(
                            collection_id=collection.id,
                            file_id=row.id,
                            kind="restored",
                            detected_at=now,
                            acknowledged_at=now,
                            acknowledged_by=None,
                        )
                    )
                    summary.restored += 1
                elif deep or row.size != size or row.mtime != mtime or row.sha256 is None:
                    # Deep pass re-hashes every file; a normal pass only when size/mtime moved or
                    # no prior hash exists. Either way the sha comparison below classifies it.
                    sha = await _hash(full)
                    if sha != row.sha256:
                        row.size, row.mtime, row.sha256 = size, mtime, sha
                        row.last_checked = row.last_changed = now
                        # Content changed: re-queue for a fresh stamp (each distinct content
                        # state gets its own proof). Applies to both worm and churn collections.
                        if perfile:
                            row.ots_state = "pending"
                        if collection.mode == "churn":
                            # Change is expected: silently re-baseline, no nag.
                            row.status = "ok"
                            summary.ok += 1
                        else:
                            row.status = "modified"
                            session.add(
                                Event(
                                    collection_id=collection.id,
                                    file_id=row.id,
                                    kind="modified",
                                    detected_at=now,
                                )
                            )
                            summary.modified += 1
                            _record_alarm("modified", relpath)
                    else:
                        # Only metadata moved; bytes unchanged. Preserve pending status.
                        row.size, row.mtime = size, mtime
                        row.last_checked = now
                        summary.ok += 1
                else:
                    # Fast-path: unchanged. Preserve status (e.g. pending 'new'/'modified').
                    row.last_checked = now
                    summary.ok += 1
            except OSError:
                summary.errors += 1
                continue

            processed += 1
            if processed % BATCH == 0:
                await _drain()

        # Flush the final added batch (assigns ids + writes their `added` events) so every new
        # row is correlatable before the move pass runs.
        await _drain()

        if skipped_unstorable:
            logging.getLogger("cairn.scanner").warning(
                "collection %s: skipped %d file(s) with non-UTF-8 names that cannot be tracked "
                "(SQLite TEXT requires valid UTF-8); run is partial. Sample: %r",
                collection.id,
                skipped_unstorable,
                unstorable_sample,
            )

        # Files in the DB but no longer on disk → candidate deletions (skip ones already missing).
        newly_missing = [
            row
            for relpath, row in existing.items()
            if relpath not in seen and row.status != "missing"
        ]

        # Move/rename reconciliation: a candidate-missing file whose content (sha256 + size)
        # uniquely matches one newly-added file is the same file relocated, not a deletion +
        # addition. Reconcile it in place (preserves identity/proof) and emit one `moved` event.
        reconciled_ids = await _reconcile_moves(
            session, collection, new_rows, newly_missing, now, summary
        )

        # Genuine deletions: every candidate not reconciled as a move becomes `missing` + alarms.
        for row in newly_missing:
            if row.id in reconciled_ids:
                continue
            row.status = "missing"
            row.last_checked = now
            session.add(
                Event(collection_id=collection.id, file_id=row.id, kind="missing", detected_at=now)
            )
            summary.missing += 1
            _record_alarm("missing", row.relpath)

        # Auto-baseline: on a deep pass (which has just re-hashed everything), graduate every file
        # that is still `new` and present this scan to `ok`. Only pre-existing `new` rows qualify —
        # `existing` is the pre-scan snapshot, so files first discovered this pass (in `new_rows`)
        # are not promoted. A `new` row that this pass reclassified `modified`/`missing` is no longer
        # `new`, so it is never auto-accepted. No re-stamp: a `new` file was stamped when first seen.
        if deep and collection.auto_baseline_new:
            for relpath, row in existing.items():
                if row.status == "new" and relpath in seen:
                    row.status = "ok"
                    summary.baselined += 1
            if summary.baselined:
                logging.getLogger("cairn.scanner").info(
                    "collection %s: auto-baselined %d intact new file(s) to ok on deep pass",
                    collection.id,
                    summary.baselined,
                )

        await session.commit()
        summary.result = "partial" if summary.errors else "ok"
    except Exception:
        logging.getLogger("cairn.scanner").exception(
            "scan failed for collection %s; finalizing run as error", collection.id
        )
        summary.result = "error"
        # A failed flush/commit leaves the session in a pending-rollback state. Clear it so the run
        # row (committed `running` up front) can still be moved to a terminal state below — otherwise
        # it stays `running` and the concurrency guard blocks the collection until the next restart.
        await session.rollback()

    # Stamp the files this scan queued (perfile only). A stamp failure must never fail the
    # scan: count what succeeded, log the rest, and finish the run normally.
    if perfile:
        try:
            from . import proofs

            run.stamped = await proofs.stamp_pending(session, collection)
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "stamp_pending failed for collection %s", collection.id
            )

    run.added = summary.added
    run.modified = summary.modified
    run.missing = summary.missing
    run.moved = summary.moved
    run.processed = processed
    run.finished = _utcnow()
    run.result = summary.result
    try:
        await session.commit()
    except Exception:
        # A scan MUST reach a terminal run state — never leave the badge/concurrency guard wedged at
        # `running`. If even this finalizing commit fails, reset and force the row terminal directly.
        logging.getLogger("cairn.scanner").exception(
            "finalizing run %s failed; forcing terminal error state", run_id
        )
        await session.rollback()
        await session.execute(
            update(Run).where(Run.id == run_id).values(result="error", finished=_utcnow())
        )
        await session.commit()
        summary.result = "error"

    # Best-effort alert AFTER the commit: a newly-detected missing (any mode) or modified-WORM
    # change fans out to the collection's enabled channels. Dispatch isolates per-channel failures and
    # is itself wrapped here, so a notification error never affects the scan result.
    if summary.alarming:
        try:
            from ..config import get_settings
            from ..notify.base import Alert
            from ..notify.dispatch import dispatch as notify_dispatch
            from . import app_settings

            parts: list[str] = []
            if summary.missing:
                parts.append(f"{summary.missing} missing")
            if summary.modified:
                parts.append(f"{summary.modified} modified")
            alert = Alert(
                collection_name=collection.name,
                summary=", ".join(parts) or "changes detected",
                paths=[rp for _kind, rp in summary.alarming],
                detected_at=now,
            )
            eff_settings = await app_settings.effective_settings(session, get_settings())
            await notify_dispatch(alert, collection, eff_settings)
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "alert dispatch failed for collection %s", collection.id
            )

    return summary


async def accept_collection(
    session: AsyncSession, collection: Collection, user_id: int | None
) -> dict[str, int]:
    """Re-baseline acknowledged changes (nag-until-accept). Idempotent."""
    now = _utcnow()
    accepted = removed = 0

    files = list(
        await session.scalars(select(FileEntry).where(FileEntry.collection_id == collection.id))
    )
    # Detach events from the files we're about to delete so the audit trail survives the
    # ON DELETE CASCADE on events.file_id (a vanished file's history must not vanish too).
    missing_ids = [f.id for f in files if f.status == "missing"]
    if missing_ids:
        await session.execute(
            update(Event).where(Event.file_id.in_(missing_ids)).values(file_id=None)
        )
    for f in files:
        if f.status in ("new", "modified"):
            f.status = "ok"
            accepted += 1
        elif f.status == "missing":
            await session.delete(f)
            removed += 1

    events = list(
        await session.scalars(
            select(Event).where(
                Event.collection_id == collection.id, Event.acknowledged_at.is_(None)
            )
        )
    )
    for e in events:
        e.acknowledged_at = now
        e.acknowledged_by = user_id

    await session.commit()
    return {"accepted": accepted, "removed": removed, "events_ack": len(events)}
