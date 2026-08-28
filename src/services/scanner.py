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

from sqlalchemy import case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, Event, FileEntry, Run

CHUNK = 1 << 20  # 1 MiB streamed-hash chunk
BATCH = 500  # files per DB commit
# Ids per `WHERE id IN (...)` chunk when system-acknowledging a restored file's open
# `missing` events (SQLite's bound-parameter ceiling is 999). Kept a module constant so a
# test can drive the chunking without creating hundreds of files.
ACK_CHUNK = 500
ALARM_PATH_CAP = 20  # max relpaths carried into a batched alert

# --- Bounds on `runs.error_sample` (design D6) -------------------------------------------------
#
# "Capped at 20 entries" is not a bound on its own: one entry is a rendering of a PATH, `repr` of
# `os.fsencode` output escapes each bad byte to four characters, and a deep tree of long names
# makes 20 entries arbitrarily large — in a column read on every collection-card render. So the
# sample is bounded on three axes, all enforced at write time, and whichever bound bites, the
# dropped entries are COUNTED rather than silently omitted.
RUN_ERROR_SAMPLE_MAX = 20  # real entries
RUN_ERROR_SAMPLE_ENTRY_BYTES = 256  # per entry, encoded
RUN_ERROR_SAMPLE_TOTAL_BYTES = 4096  # the whole serialized JSON array, encoded
# ASCII, not `…`: this column's entire invariant is that its stored bytes cannot fail to bind for
# the class of reason the sample exists to report. Deterministic and explicit, so a truncated
# rendering can never be mistaken for a whole name.
_TRUNCATION_MARKER = "..."


def _render_skip(reason: str, relpath: str) -> str:
    """One `error_sample` entry: an ASCII-safe DIAGNOSTIC RENDERING of a skipped path, not a path.

    This distinction is load-bearing. The headline cause of a skip is a name that could not be
    stored as TEXT in the first place (``_db_storable`` rejects a lone surrogate — that is literally
    why no row exists for it), so writing the raw name into ``error_sample`` would reproduce the
    ``UnicodeEncodeError`` that this column was added to report. ``repr`` of ``os.fsencode`` output
    escapes every non-ASCII byte to ``\\xNN``, so the result is pure ASCII by construction for any
    input. The value MUST never be fed back to a filesystem call or offered as a copyable path.
    """
    entry = f"{reason}: {os.fsencode(relpath)!r}"
    encoded = entry.encode("utf-8")
    if len(encoded) <= RUN_ERROR_SAMPLE_ENTRY_BYTES:
        return entry
    keep = RUN_ERROR_SAMPLE_ENTRY_BYTES - len(_TRUNCATION_MARKER.encode("utf-8"))
    # Cut on a byte boundary; `errors="ignore"` drops a partial trailing sequence (unreachable for
    # the ASCII renderings above, kept so the bound holds for any caller).
    return encoded[:keep].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER


def _build_error_sample(entries: list[str], total_errors: int) -> str | None:
    """Serialize the bounded sample for ``runs.error_sample``; ``None`` when nothing was skipped.

    Entries are appended only while the SERIALIZED array stays inside
    :data:`RUN_ERROR_SAMPLE_TOTAL_BYTES`, with room reserved for the trailing marker, so the budget
    is measured on the encoded form that is actually stored. Whichever bound bites, the array's last
    element is ``"+N more skipped (sample truncated)"`` where ``N`` is ``total_errors`` minus the
    real entries kept — the TRUE remainder, not the remainder of the cap. ``json.dumps`` runs at its
    default ``ensure_ascii=True``, escaping any non-ASCII to ``\\uXXXX``: belt and braces over the
    ASCII-safe rendering, and the reason the byte budget can be measured on the encoded form.
    """
    if total_errors <= 0:
        return None

    def _marker(dropped: int) -> str:
        return f"+{dropped} more skipped (sample truncated)"

    kept: list[str] = []
    for entry in entries[:RUN_ERROR_SAMPLE_MAX]:
        candidate = [*kept, entry]
        dropped = total_errors - len(candidate)
        trial = [*candidate, _marker(dropped)] if dropped > 0 else candidate
        if len(json.dumps(trial).encode("utf-8")) > RUN_ERROR_SAMPLE_TOTAL_BYTES:
            break
        kept = candidate
    dropped = total_errors - len(kept)
    out = [*kept, _marker(dropped)] if dropped > 0 else kept
    return json.dumps(out)


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
    # Files that reappeared after being recorded `missing` with bytes that do NOT match the digest
    # recorded for them (#21). Each one is ALSO counted in `modified` (its status is `modified`) —
    # this is the narrower story, for the scan line and the alert summary. Deliberately not a
    # `runs` column: the file is already counted in `runs.modified` and the event kind carries the
    # distinction (design D4).
    restored_changed: int = 0
    ok: int = 0
    errors: int = 0
    # Intact `new` files promoted to `ok` by the deep pass (auto_baseline_new). Informational only.
    baselined: int = 0
    result: str = "ok"
    # Alarming events newly created THIS run — (kind, relpath), capped at ALARM_PATH_CAP. Only
    # `missing` (any mode), `restored_changed` (any mode) and `modified` (WORM) accumulate here;
    # `added`, `restored`, `moved` and churn re-baselines do not. The post-commit dispatch hook
    # turns a non-empty list into a single batched alert.
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
    # Identity captured up front, as plain locals. Every `session.rollback()` below — the lost
    # claim, and the scan-body failure handler — expires EVERY ORM object in the session
    # (independent of `expire_on_commit=False`, which only covers commits), so a later
    # `collection.id` would trigger a lazy refresh and raise `MissingGreenlet` from async code.
    # A logging call on a refusal/failure path must never be the thing that crashes the scan, so
    # nothing after the claim reads identity off the ORM object.
    collection_id = collection.id
    collection_name = collection.name
    summary = RunSummary(collection_id=collection_id)
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
            "scan refused for collection %s — another operation already claimed it",
            collection_id,
        )
        summary.result = "skipped"
        # The lost claim rolled back, expiring `collection` along with everything else in the
        # session. Hand it back usable: `run_due_scans` reads `collection.hash_cadence_seconds` in
        # its `finally` the moment this returns, and a refusal must never be the thing that kills a
        # scheduler tick. One SELECT, only on the rare contended path.
        try:
            await session.get(Collection, collection_id)
        except Exception:  # pragma: no cover - the datastore is gone; the caller will find out
            logging.getLogger("cairn.scanner").exception(
                "reloading collection %s after a refused scan failed", collection_id
            )
        return summary
    # Capture the id now (expire_on_commit=False keeps it populated): a later rollback expires the
    # ORM object, and the terminal-state fallback must reference the run by id without a lazy load.
    run_id = run.id
    # Set by any fence that finds this run is no longer the collection's live claim. Once true the
    # scan mutates nothing further: no batch commits, no stamping, no terminal-state write.
    lease_lost = False

    # The lease keeps its own time. Every heartbeat below rides on the completion of a unit of
    # work — a batch drain, a stamp batch — so a scan that spends longer than the abandonment
    # interval inside ONE unit (hashing a multi-terabyte file, a stalled NAS batch) would starve
    # its own claim and be legitimately reclaimed while it is still working, after which it would
    # walk straight into the stamp tail as a second proof writer. The keepalive refreshes the
    # claim on a timer, in its own session, for as long as this body runs (design D10).
    async with collections.run_keepalive(run_id):
        existing: dict[str, FileEntry] = {
            f.relpath: f
            for f in await session.scalars(
                select(FileEntry).where(FileEntry.collection_id == collection.id)
            )
        }
        seen: set[str] = set()
        added_buffer: list[FileEntry] = []
        # Ids of files restored in this batch (missing → ok). `_drain` system-acknowledges their open
        # `missing` events inside the same transaction that commits the restore (design D10).
        restored_ids: list[int] = []
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
        # One bounded, ASCII-safe diagnostic rendering per skipped file, across ALL FOUR causes
        # (un-storable name, `is_symlink` OSError, `stat` OSError, hash OSError), persisted to
        # `runs.error_sample` and logged once at finalize. Capped here as well as at serialization
        # so a pathological tree cannot grow this list without bound in memory either.
        # `summary.errors` keeps the TRUE total; this list only ever holds the sample.
        error_entries: list[str] = []

        def _record_alarm(kind: str, relpath: str) -> None:
            if len(summary.alarming) < ALARM_PATH_CAP:
                summary.alarming.append((kind, relpath))

        async def _drain() -> None:
            nonlocal added_buffer, restored_ids
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
            # A restored file's own `missing` alert is closed by the same transaction that commits the
            # restore. `_drain` commits every BATCH files *and* after the walk, so acknowledging later
            # would already have persisted `status='ok'` + the `restored` event: a failing ack would
            # then leave a healthy file wearing an open `missing` alert nothing can clear. Raising here
            # instead takes the whole batch down with it — the exception reaches the scan body's
            # `except`, the session is rolled back and the run finalizes `error` (design D10).
            for i in range(0, len(restored_ids), ACK_CHUNK):
                chunk = restored_ids[i : i + ACK_CHUNK]
                # `kind == "missing"` is load-bearing: a blanket `WHERE file_id = ...` would also
                # acknowledge an open WORM `modified` event on the same file, which is a different
                # alert about a different thing and nothing here has resolved it (#12's rejected
                # fix 7). `acknowledged_by=None` marks a *system* ack, matching the born-acked
                # convention used for `added`/`restored`/`moved`.
                await session.execute(
                    update(Event)
                    .where(
                        Event.file_id.in_(chunk),
                        Event.kind == "missing",
                        Event.acknowledged_at.is_(None),
                    )
                    .values(acknowledged_at=now, acknowledged_by=None)
                )
            run.processed = processed  # persist live progress for the status badge
            # ...and the claim's liveness with it. A batch is the scan's unit of progress, so this is
            # the scan's heartbeat: without it a long scan looks abandoned to the startup reaper, which
            # would revoke a live claim and let a second writer in (design D10). The keepalive around
            # this whole body is what covers the gap BETWEEN two of these; this one stays because a
            # batch commit is also where `processed` belongs.
            run.heartbeat_at = _utcnow()
            # The fence, immediately before the write. If this scan's claim was reclaimed while the
            # batch was being built, another operation now owns the collection and nothing of ours
            # may land on top of its work: the in-flight batch is rolled back (nothing commits after
            # the fence sees a reclamation) and the scan aborts. Batches already committed were
            # committed under a lease that was valid at the time and stand as written.
            if not await collections.lease_held(run_id):
                await session.rollback()
                raise collections.LeaseLost(
                    f"the operation claim for collection {collection_id} (run {run_id}) was "
                    f"reclaimed mid-scan"
                )
            await session.commit()
            # Cleared only once the commit has returned, so a rollback never drops an acknowledgement
            # that was never persisted.
            restored_ids = []

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
                    if len(error_entries) < RUN_ERROR_SAMPLE_MAX:
                        error_entries.append(_render_skip("unstorable-name", relpath))
                    continue
                full = root / relpath
                try:
                    is_symlink = full.is_symlink()
                except OSError:
                    # `is_symlink()` lstats, so it fails for the same reasons the guarded `stat()`
                    # below does; unguarded it aborts the whole scan instead of skipping one entry.
                    summary.errors += 1
                    if len(error_entries) < RUN_ERROR_SAMPLE_MAX:
                        error_entries.append(_render_skip("lstat", relpath))
                    continue
                if is_symlink:
                    continue  # conservative: never follow symlinks out of the read-only jail
                try:
                    st = full.stat()
                except OSError:
                    # Silent until 0012: the file was counted into `partial` and named nowhere.
                    summary.errors += 1
                    if len(error_entries) < RUN_ERROR_SAMPLE_MAX:
                        error_entries.append(_render_skip("stat", relpath))
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
                        # Reappeared after being recorded missing. COMPARE BEFORE OVERWRITE: the
                        # recorded digest is the only record of what this file used to be, and this
                        # branch used to destroy it with `row.sha256 = await _hash(full)` as its very
                        # first statement — adopting whatever bytes turned up as "restored / OK"
                        # without ever checking them (#21). Capture the prior digest first, hash, then
                        # classify from the comparison (design D6).
                        prior = row.sha256
                        sha = await _hash(full)
                        # The index must keep describing the bytes on disk NOW (sprint-1's verify
                        # blame attribution reads `files.sha256` as the last-seen digest), so the
                        # recorded digest is still updated in every outcome. The fix is *compare
                        # before overwrite*, not *stop overwriting*.
                        row.sha256 = sha
                        row.size, row.mtime = size, mtime
                        row.last_checked = row.last_changed = now
                        if prior is not None and sha != prior:
                            # Something is back at the path, but it is not what left: a wrong backup
                            # snapshot, a truncated restore, or bytes planted while the real file was
                            # known-absent. It alarms in BOTH modes — a churn collection exempts
                            # ordinary edits, and a file that was absent and came back different is
                            # not an ordinary edit (design D4). Reusing `modified` would be silent
                            # there, which is why `restored_changed` exists.
                            row.status = "modified"
                            if perfile:
                                # The new bytes get their own proof; #15's preservation keeps the
                                # previous bytes' proof from being overwritten by it.
                                row.ots_state = "pending"
                            session.add(
                                Event(
                                    collection_id=collection.id,
                                    file_id=row.id,
                                    kind="restored_changed",
                                    detected_at=now,
                                    # Both digests in full: the one recorded before the file went
                                    # missing, and the one observed now. Truncating either would cost
                                    # the operator the ability to identify what actually came back.
                                    detail=f"recorded {prior} → found {sha}",
                                )
                            )
                            summary.modified += 1
                            summary.restored_changed += 1
                            _record_alarm("restored_changed", relpath)
                        else:
                            # Identical bytes — or a legacy row with no recorded digest, where nothing
                            # can be established and so nothing is alarmed. `restored` is
                            # informational (a missing file came back, the benign direction): born
                            # acknowledged like `added`, so it stays in the feed without nagging.
                            # `ots_state` is untouched — the stored proof still commits to these bytes.
                            row.status = "ok"
                            session.add(
                                Event(
                                    collection_id=collection.id,
                                    file_id=row.id,
                                    kind="restored",
                                    detected_at=now,
                                    acknowledged_at=now,
                                    acknowledged_by=None,
                                    detail=(
                                        None
                                        if prior is not None
                                        else "no digest was recorded for this file, so the returned "
                                        "bytes could not be compared"
                                    ),
                                )
                            )
                            summary.restored += 1
                        # Close the file's own open `missing` alert(s) in the same transaction that
                        # commits this reappearance — drained in `_drain` just below (design D5/D10).
                        # The trigger is that the file REAPPEARED, not that it is healthy: the
                        # proposition a `missing` alert asserts ("this file is absent") is false
                        # either way, and for a changed reappearance the alarm rides on the
                        # unacknowledged `restored_changed` event, which says strictly more.
                        restored_ids.append(row.id)
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
                    # Silent until 0012, exactly as the `stat` skip above was.
                    summary.errors += 1
                    if len(error_entries) < RUN_ERROR_SAMPLE_MAX:
                        error_entries.append(_render_skip("hash", relpath))
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

            if summary.restored_changed:
                # One batched line, not one per file: a mass wrong restore (the whole point of the
                # check) must not bury the log. The per-file digests live in `events.detail`.
                logging.getLogger("cairn.scanner").warning(
                    "collection %s: %d file(s) reappeared with bytes that do NOT match the digest "
                    "recorded for them — each is now `modified` with an unacknowledged "
                    "`restored_changed` event carrying both digests; review before trusting them",
                    collection.id,
                    summary.restored_changed,
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
        except collections.LeaseLost:
            # Not a failure of this scan: the fence caught the collection being taken from under it
            # (its lease aged out and something reclaimed it), and stopping is the correct response.
            # Loud, because a lease this scan believed it held was revoked while it was working —
            # the operator wants to know the claim timed out, not just that a scan ended early.
            lease_lost = True
            logging.getLogger("cairn.scanner").warning(
                "collection %s: this scan's operation claim (run %s) was RECLAIMED while it was "
                "running — aborting without committing the in-flight batch, skipping the stamp "
                "pass, and leaving the run row as the reclamation left it",
                collection_id,
                run_id,
            )
            # Reported as `skipped`, the same word a refused claim uses: this scan did not complete
            # a pass over the collection, and reporting it as `ok`/`partial` would let a cron
            # `cairn scan` record a clean integrity pass it never finished.
            summary.result = "skipped"
            # `_drain` already rolled back, which expired `collection`; the alert tail below reads
            # it. Reload for the same reason (and by the same means) as the failure path.
            try:
                await session.get(Collection, collection_id)
            except Exception:  # pragma: no cover - the datastore is gone
                logging.getLogger("cairn.scanner").exception(
                    "reloading collection %s after a reclaimed scan failed", collection_id
                )
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "scan failed for collection %s; finalizing run as error", collection_id
            )
            summary.result = "error"
            # A failed flush/commit leaves the session in a pending-rollback state. Clear it so the run
            # row (committed `running` up front) can still be moved to a terminal state below — otherwise
            # it stays `running` and the concurrency guard blocks the collection until the next restart.
            await session.rollback()
            # That rollback also expired `collection`, so the stamp/alert tail below would raise
            # `MissingGreenlet` on its first attribute read. Reload it (same identity-mapped instance,
            # one SELECT). If even that fails the datastore is gone: the tail's own guards handle it,
            # and the terminal-run write below is what must still happen.
            try:
                await session.get(Collection, collection_id)
            except Exception:
                logging.getLogger("cairn.scanner").exception(
                    "reloading collection %s after a failed scan body failed", collection_id
                )

        # Stamp the files this scan queued (perfile only). A stamp failure must never fail the
        # scan: count what succeeded, log the rest, and finish the run normally.
        #
        # THE FENCE IN FRONT OF PROOF MUTATION. This tail runs after the walk, under the same claim,
        # and it is the one part of a scan that writes the proof store. If the claim is no longer
        # ours, stamping here is precisely the second writer design D10 exists to exclude — two
        # processes both finding a canonical path free and both `os.replace`-ing onto it, one
        # submission destroyed with no trace. So the lease is re-read from the datastore first, and
        # a lost one skips stamping entirely: the files stay `pending` and whoever holds the
        # collection now (or the next pass) stamps them.
        stamped_count = 0
        if perfile and not lease_lost and not await collections.lease_held(run_id):
            lease_lost = True
            logging.getLogger("cairn.scanner").warning(
                "collection %s: the operation claim (run %s) was RECLAIMED before this scan's stamp "
                "pass — stamping nothing; the queued files stay pending for the next pass",
                collection_id,
                run_id,
            )
        if perfile and not lease_lost:
            try:
                from . import proofs

                async def _stamp_heartbeat(done: int) -> None:
                    # The stamp pass runs after the walk, under the same claim, and can take far longer
                    # than the reaper's threshold on a large backlog. Batch-granular heartbeat so the
                    # claim keeps reading live; `processed` stays the scan's own count.
                    run.heartbeat_at = _utcnow()
                    await session.commit()

                stamped_count = await proofs.stamp_pending(
                    session, collection, progress=_stamp_heartbeat, run_id=run_id
                )
            except collections.LeaseLost:
                # `stamp_pending`'s own per-batch fence fired: the claim went away between batches.
                # Proofs already placed stand (see `stamp_pending`); nothing more is written.
                lease_lost = True
                logging.getLogger("cairn.scanner").warning(
                    "collection %s: the operation claim (run %s) was RECLAIMED during this scan's "
                    "stamp pass — stopped between batches; proofs already placed stand",
                    collection_id,
                    run_id,
                )
                # The fence rolled back to commit nothing further, which expired `collection`; the
                # alert tail below reads it. Reload it exactly as the failure path does.
                try:
                    await session.get(Collection, collection_id)
                except Exception:  # pragma: no cover - the datastore is gone
                    logging.getLogger("cairn.scanner").exception(
                        "reloading collection %s after a reclaimed stamp pass failed", collection_id
                    )
            except Exception:
                logging.getLogger("cairn.scanner").exception(
                    "stamp_pending failed for collection %s", collection_id
                )

        # Finalize under the fence: the terminal state is written only while this run is still the
        # collection's live claim (`finalize_if_held` fuses the check into the UPDATE's WHERE). A run
        # that was reclaimed keeps the `interrupted` state the reclamation wrote — overwriting it
        # with `ok`/`partial` would let a scan that was taken off the collection mid-flight refresh
        # the dead-man's switch, which is a false negative of exactly the kind this product exists
        # to prevent. Nothing here reads `run` afterwards, so it is deliberately not refreshed.
        # One WARNING per skipping run, covering EVERY cause. Until this line, only the un-storable
        # site logged: a `stat` or hash skip was counted into `partial` and named nowhere, so the
        # operator's only copy of "which files" would have been the `runs.error_sample` column — and
        # a schema downgrade that dropped it would have destroyed information that existed nowhere
        # else. Bounded by construction (the sample is already capped) and emitted once per run
        # rather than once per file. The existing un-storable-specific WARNING stays as it is: it
        # names its own cause and its own count.
        error_sample = _build_error_sample(error_entries, summary.errors)
        if summary.errors:
            logging.getLogger("cairn.scanner").warning(
                "collection %s: run %s skipped %d file(s); result=%s. Diagnostic sample (renderings, "
                "NOT usable paths): %s",
                collection_id,
                run_id,
                summary.errors,
                summary.result,
                error_sample,
            )

        try:
            finalized = await collections.finalize_if_held(
                session,
                run_id,
                added=summary.added,
                modified=summary.modified,
                missing=summary.missing,
                moved=summary.moved,
                stamped=stamped_count,
                processed=processed,
                # The count that already decides `partial`, persisted at last, plus the bounded
                # sample that says WHICH files it refers to. `errors` is always the TRUE total; the
                # sample may name fewer and says so.
                errors=summary.errors,
                error_sample=error_sample,
                finished=_utcnow(),
                result=summary.result,
            )
            if not finalized:
                logging.getLogger("cairn.scanner").warning(
                    "collection %s: run %s was RECLAIMED before this scan could finalize it — "
                    "leaving the terminal state the reclamation wrote and recording nothing",
                    collection_id,
                    run_id,
                )
        except Exception:
            # A scan MUST reach a terminal run state — never leave the badge/concurrency guard wedged at
            # `running`. If even this finalizing commit fails, reset and force the row terminal directly.
            # Still guarded on `result='running'`: a reclaimed run is already terminal and must not be
            # relabelled by the loser of that race.
            logging.getLogger("cairn.scanner").exception(
                "finalizing run %s failed; forcing terminal error state", run_id
            )
            await session.rollback()
            await session.execute(
                update(Run)
                .where(Run.id == run_id, Run.result == "running")
                .values(result="error", finished=_utcnow())
            )
            await session.commit()
            summary.result = "error"

    # Best-effort alert AFTER the commit: a newly-detected missing (any mode), a file that came
    # back with different bytes (any mode) or a modified-WORM change fans out to the collection's
    # enabled channels. Dispatch isolates per-channel failures and is itself wrapped here, so a
    # notification error never affects the scan result.
    if summary.alarming:
        try:
            from ..config import get_settings
            from ..notify.base import Alert
            from ..notify.dispatch import dispatch as notify_dispatch
            from . import app_settings

            parts: list[str] = []
            if summary.missing:
                parts.append(f"{summary.missing} missing")
            # `summary.modified` already includes the restored-changed files (their status IS
            # `modified`), so they are subtracted out and named separately rather than counted
            # twice. With none of them the wording is byte-identical to before.
            plain_modified = summary.modified - summary.restored_changed
            if plain_modified > 0:
                parts.append(f"{plain_modified} modified")
            if summary.restored_changed:
                parts.append(f"{summary.restored_changed} came back changed")

            # The deep link is a convenience; being told at all is the product. So the two things
            # on the way to it — resolving the effective settings, then building the URL — get
            # SEPARATE guards, and neither may take the other down with it. The enclosing block
            # protects the *scan*; these two protect the *alert*.
            #
            # Guard 1 — the settings overlay. What it returns is the alert's *transport* config,
            # not just the link's address: a deployment that configures SMTP from the panel keeps
            # host/user/password in `app_settings`, and the env-only fallback has no smtp_host at
            # all. So the fallback is used only when the overlay itself fails; a successful overlay
            # must survive anything that goes wrong afterwards, or a cosmetic link bug would
            # downgrade the settings until SmtpNotifier raises "SMTP host is not configured" and
            # the one channel actually in production silently sends nothing.
            eff_settings = get_settings()
            try:
                # Rebound only on success, so a raise leaves the env-derived value standing.
                eff_settings = await app_settings.effective_settings(session, eff_settings)
            except Exception:
                logging.getLogger("cairn.scanner").exception(
                    "resolving effective settings failed for collection %s; "
                    "falling back to environment settings",
                    collection_id,
                )

            # Guard 2 — the link itself, around the synchronous builder only. A corrupt stored
            # public_url or a builder bug costs a click, never the alert.
            review_url: str | None = None
            try:
                from .panel_url import panel_link

                review_url = panel_link(
                    eff_settings.public_url, f"/collection/{collection_id}/review"
                )
            except Exception:
                review_url = None
                logging.getLogger("cairn.scanner").exception(
                    "building the alert review link failed for collection %s; "
                    "sending a link-free alert",
                    collection_id,
                )

            alert = Alert(
                collection_name=collection_name,
                summary=", ".join(parts) or "changes detected",
                paths=[rp for _kind, rp in summary.alarming],
                detected_at=now,
                url=review_url,
            )
            await notify_dispatch(alert, collection, eff_settings)
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "alert dispatch failed for collection %s", collection_id
            )

    return summary


# The three file states an accept can act on. A scope naming anything else is a caller bug and
# is refused outright: silently widening a typo'd scope to "everything" is exactly the unscoped
# blast radius this split exists to remove (#16, root cause R2 of #12).
ACCEPT_SCOPES = frozenset({"new", "modified", "missing"})


class AcceptScopeError(ValueError):
    """An accept was asked to act outside what it was given.

    Raised for a scope naming a state that is not one of :data:`ACCEPT_SCOPES`, and for a
    single-file accept whose file does not belong to the named collection. A ``ValueError``
    subclass so existing callers that guard on ``ValueError`` keep working.
    """


def _detach_ack_backfill(where, now: datetime, user_id: int | None):
    """The one statement that retires a file record's events, issued *before* the row is deleted.

    Detaching (``file_id = NULL``) is what saves the audit trail from ``ON DELETE CASCADE``. It is
    also what erases the events' only link to a path, so the same statement carries the path
    forward into ``detail`` (#35) and acknowledges the alerts the operator just resolved (#16/D8).

    Five properties are load-bearing (design D7):

    1. **Correlated, per row** — the scalar subquery gives each event *its own* file's relpath;
       one bulk ``values()`` could only apply a single value across the batch.
    2. **Evaluated against pre-update values** — SQLite computes every ``SET`` expression from the
       row's *old* values, so ``detail``'s subquery still sees the pre-NULL ``file_id`` in the very
       statement that clears it. Splitting this into detach-then-backfill would backfill nothing.
    3. **``detail`` filled only when NULL or empty** — a ``moved`` event's ``old → new`` pair and a
       ``restored_changed`` event's digest pair are the findings those kinds exist to carry.
    4. **``acknowledged_at`` via COALESCE** — an already-acknowledged event keeps its original
       timestamp and its original acknowledger; re-stamping would make the reading log lie.
    5. **A subquery, never a Python ``IN`` list** — a deleted folder is easily more ``missing`` rows
       than SQLite's bound-parameter ceiling, which an id list would blow.
    """
    relpath_of_this_events_file = (
        select(FileEntry.relpath).where(FileEntry.id == Event.file_id).correlate(Event)
    ).scalar_subquery()
    return (
        update(Event)
        .where(where)
        .values(
            file_id=None,
            acknowledged_at=func.coalesce(Event.acknowledged_at, now),
            acknowledged_by=case(
                (Event.acknowledged_at.is_(None), user_id), else_=Event.acknowledged_by
            ),
            detail=case(
                (
                    or_(Event.detail.is_(None), Event.detail == ""),
                    relpath_of_this_events_file,
                ),
                else_=Event.detail,
            ),
        )
        # The in-memory Event objects this touches are only ever re-read for `acknowledged_*`,
        # which the ack loop rewrites to the same values; leaving them un-synchronized avoids
        # SQLAlchemy trying to evaluate a correlated subquery in Python.
        .execution_options(synchronize_session=False)
    )


async def accept_collection(
    session: AsyncSession,
    collection: Collection,
    user_id: int | None,
    scope: set[str] | frozenset[str] | None = None,
) -> dict[str, int]:
    """Re-baseline acknowledged changes (nag-until-accept). Idempotent.

    ``scope`` names which of the three populations to act on — ``new``, ``modified``, ``missing``.
    ``None`` is the unscoped legacy verb (`cairn accept`): all three populations, and the
    collection-wide event acknowledgement, including events already detached from any file.

    **A scoped accept acknowledges only the events of the files its scope touched** (#16/D8). An
    accept that still cleared every open event would close alarms its label never mentioned — a
    ``{"new"}`` accept, which deletes nothing, silently closing a missing-file alert. That is the
    same false negative the scoping exists to remove.
    """
    now = _utcnow()
    if scope is not None:
        unknown = sorted(set(scope) - ACCEPT_SCOPES)
        if unknown:
            raise AcceptScopeError(
                f"unrecognized accept scope: {', '.join(unknown)} "
                f"(expected any of {', '.join(sorted(ACCEPT_SCOPES))})"
            )
    statuses = ACCEPT_SCOPES if scope is None else frozenset(scope)
    accepted = removed = 0

    files = list(
        await session.scalars(select(FileEntry).where(FileEntry.collection_id == collection.id))
    )

    # Read the acknowledgement population *before* anything mutates, and by subquery rather than
    # by an id list, so a collection with more touched files than SQLite's parameter ceiling still
    # completes. Unscoped keeps the blanket collection-wide set (detached events included).
    if scope is None:
        ack_where = (Event.collection_id == collection.id,)
    else:
        ack_where = (
            Event.collection_id == collection.id,
            Event.file_id.in_(
                select(FileEntry.id).where(
                    FileEntry.collection_id == collection.id,
                    FileEntry.status.in_(sorted(statuses)),
                )
            ),
        )
    events = list(
        await session.scalars(
            select(Event).where(Event.acknowledged_at.is_(None), *ack_where)
        )
    )

    # Detach the events of the rows we are about to delete so the audit trail survives the
    # ON DELETE CASCADE (a vanished file's history must not vanish too) — and, in the same
    # statement, keep the path those rows are about. Must precede the deletes.
    if "missing" in statuses:
        await session.execute(
            _detach_ack_backfill(
                Event.file_id.in_(
                    select(FileEntry.id).where(
                        FileEntry.collection_id == collection.id,
                        FileEntry.status == "missing",
                    )
                ),
                now,
                user_id,
            )
        )

    for f in files:
        if f.status not in statuses:
            continue
        if f.status in ("new", "modified"):
            f.status = "ok"
            accepted += 1
        elif f.status == "missing":
            await session.delete(f)
            removed += 1

    for e in events:
        e.acknowledged_at = now
        e.acknowledged_by = user_id

    await session.commit()
    return {"accepted": accepted, "removed": removed, "events_ack": len(events)}


async def accept_file(
    session: AsyncSession,
    collection: Collection,
    file: FileEntry,
    user_id: int | None,
) -> dict[str, int]:
    """Accept exactly one file record, applying what its scoped collection accept would apply.

    A ``new`` or ``modified`` row is set ``ok``; a ``missing`` row has its events detached (and its
    path carried into their ``detail``) *before* the row is deleted, in the same order and by the
    same statement a bulk stop-tracking uses. Only this file's open events are acknowledged; every
    other file's alerts, baselines and records are untouched (#30).

    Raises :class:`AcceptScopeError` — mutating nothing — if the file is not in the collection.
    """
    if file.collection_id != collection.id:
        raise AcceptScopeError(
            f"file {file.id} does not belong to collection {collection.id}"
        )

    now = _utcnow()
    if file.status not in ACCEPT_SCOPES:
        # Nothing to accept on an already-baselined row: acknowledge nothing, mutate nothing.
        return {"accepted": 0, "removed": 0, "events_ack": 0}

    accepted = removed = 0
    events = list(
        await session.scalars(
            select(Event).where(Event.file_id == file.id, Event.acknowledged_at.is_(None))
        )
    )

    if file.status == "missing":
        await session.execute(_detach_ack_backfill(Event.file_id == file.id, now, user_id))
        await session.delete(file)
        removed = 1
    else:
        file.status = "ok"
        accepted = 1

    for e in events:
        e.acknowledged_at = now
        e.acknowledged_by = user_id

    await session.commit()
    return {"accepted": accepted, "removed": removed, "events_ack": len(events)}
