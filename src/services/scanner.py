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

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, Event, FileEntry, Run
from . import rules as path_rules

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


def _beneath(relpath: str, dirs: list[str]) -> bool:
    """True when ``relpath`` lies under one of ``dirs`` (collection-relative, literal prefix)."""
    return any(relpath.startswith(d + "/") for d in dirs)


def iter_relpaths(root: Path, globs: list[str], walk_errors: list[OSError] | None = None):
    """Yield POSIX relpaths of files under root, pruning excluded dirs; no symlink following.

    ``os.walk`` silently skips a directory it cannot list unless given an ``onerror`` hook. A
    skipped directory looks exactly like a deleted one to the missing sweep, so when
    ``walk_errors`` is a list every listing error is appended to it and the caller decides what an
    incomplete walk means (add-scan-safety-nets D1: no file is marked missing by such a pass).
    """
    root = Path(root)
    onerror = walk_errors.append if walk_errors is not None else None
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
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
    # Existing files the unresolved-change rule (design D12) recorded `modified` where the pass
    # would otherwise have recorded them `ok` (or left them `ok`/`new`): each carries an open
    # `modified`/`restored_changed` event. No event is written and no alert raised for them.
    reflagged: int = 0
    # Files whose digest changed while size and mtime did not (possible corruption, design D2). Each
    # is ALSO counted in `modified`, like `restored_changed`.
    corrupted: int = 0
    # Files found back at the contents on record before an unaccepted change (design D3): recorded
    # `ok`, their change alerts system-acknowledged. Informational; never alerts.
    reverted: int = 0
    # Churn re-baselines converted into `modified` issues by the mass-change rule (design D4); 0
    # when the rule did not fire. Each is ALSO counted in `modified`.
    mass_change: int = 0
    # True when at least one directory under the root could not be listed: the pass marked nothing
    # missing, reconciled no moves, and is not a complete (deep) verification (design D1).
    walk_incomplete: bool = False
    # The refusal kind (REFUSAL_UNREACHABLE / REFUSAL_EMPTY) when the pass refused the root.
    refused: str | None = None
    # ...and its human sentence (the text `refusal_reason` returns from the run row).
    refusal: str | None = None
    result: str = "ok"
    # Alarming events newly created THIS run — (kind, relpath), capped at ALARM_PATH_CAP. Only
    # `missing` (any mode), `restored_changed` (any mode) and `modified` (WORM) accumulate here;
    # `added`, `restored`, `moved` and churn re-baselines do not. The post-commit dispatch hook
    # turns a non-empty list into a single batched alert.
    alarming: list[tuple[str, str]] = field(default_factory=list)


# Event kinds that record an unresolved CHANGE to a file's bytes. While one of them is open
# (unacknowledged) on a file, the file is an issue whatever happens to it afterwards (design D12).
UNRESOLVED_CHANGE_KINDS = ("modified", "restored_changed", "corrupted")

# --- Root refusal and walk errors (add-scan-safety-nets D1) -------------------------------------
#
# A refused run stores `runs.error_sample` = a one-entry JSON list "<kind>: <human sentence>" and
# `runs.errors` = 0; a pass with unreadable directories stores one "unreadable: <rendering>" entry
# per directory (first in the sample), counted in `runs.errors`. The prefixes are a contract: the
# alert rate limit and `refusal_reason` key on them.
REFUSAL_UNREACHABLE = "unreachable"
REFUSAL_EMPTY = "empty"
WALK_UNREADABLE = "unreadable"
REFUSAL_KINDS = (REFUSAL_UNREACHABLE, REFUSAL_EMPTY)
# Alerts about a refused root / an unreadable folder are repeated at most this often per collection.
SAFETY_ALERT_INTERVAL_SECONDS = 24 * 60 * 60
# The informational `restored` event a return writes (design D3).
RETURN_DETAIL = "returned to the contents on record before it changed"


class ScanRefused(Exception):
    """The pass refused its root (unreachable, or empty while files are tracked).

    Raised inside the scan body before any file record or event was written; finalised as a failed
    scan (``result='error'``) whose ``error_sample`` carries the reason.
    """

    def __init__(self, kind: str, sentence: str) -> None:
        super().__init__(f"{kind}: {sentence}")
        self.kind = kind
        self.sentence = sentence


def _bounded_entry(entry: str) -> str:
    """Hold one free-text `error_sample` entry to the per-entry byte bound, ASCII-escaped."""
    entry = entry.encode("ascii", errors="backslashreplace").decode("ascii")
    if len(entry) <= RUN_ERROR_SAMPLE_ENTRY_BYTES:
        return entry
    return entry[: RUN_ERROR_SAMPLE_ENTRY_BYTES - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


def refusal_reason(run) -> str | None:
    """The human sentence of a refused run (``None`` for any other run).

    A refused run is one with ``result='error'`` whose ``error_sample`` is a one-entry list
    beginning with a refusal prefix. Anything unparseable reads as "not a refusal".
    """
    if getattr(run, "result", None) != "error" or not getattr(run, "error_sample", None):
        return None
    try:
        entries = json.loads(run.error_sample)
    except (TypeError, ValueError):
        return None
    if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], str):
        return None
    for kind in REFUSAL_KINDS:
        prefix = f"{kind}: "
        if entries[0].startswith(prefix):
            return entries[0][len(prefix) :]
    return None


def _change_detail(relpath: str, before: tuple, after: tuple, mass: int | None = None) -> str:
    """JSON `detail` of a `modified`/`corrupted` event: path + contents before and after (D3)."""
    out: dict = {
        "path": relpath,
        "from": {"sha256": before[0], "size": before[1], "mtime": before[2]},
        "to": {"sha256": after[0], "size": after[1], "mtime": after[2]},
    }
    if mass is not None:
        out["mass"] = mass
    return json.dumps(out)


def _keep_prior(row: FileEntry, sha: str | None, size: int | None, mtime: float | None) -> None:
    """Record the contents before an unaccepted change — only if nothing is kept yet (D3)."""
    if row.prior_sha256 is None and sha is not None:
        row.prior_sha256, row.prior_size, row.prior_mtime = sha, size, mtime


def _clear_prior(row: FileEntry) -> None:
    row.prior_sha256 = row.prior_size = row.prior_mtime = None


async def _unresolved_change_ids(session: AsyncSession, collection_id: int) -> set[int]:
    """Ids of the collection's files carrying an open change alert (:data:`UNRESOLVED_CHANGE_KINDS`).

    Read once at the start of a scan pass (after its claim): no accept can run while the claim is
    held, so the set cannot go stale during the pass (design D12).
    """
    return set(
        await session.scalars(
            select(Event.file_id)
            .where(
                Event.collection_id == collection_id,
                Event.kind.in_(UNRESOLVED_CHANGE_KINDS),
                Event.acknowledged_at.is_(None),
                Event.file_id.is_not(None),
            )
            .distinct()
        )
    )


def _healthy_status(row: FileEntry, unresolved_ids: set[int], reflagged_ids: set[int]) -> str:
    """The status a pass records for an EXISTING row it would otherwise record healthy (``ok``).

    ``modified`` if the row carries an open change alert (its id is in ``unresolved_ids``), else
    ``ok``. A re-flagged row's id is added to ``reflagged_ids`` so the scan can count it once. This
    is the one place the at-the-write layer of design D12 lives; every branch that sets an existing
    row ``ok`` goes through it, so no batch a scan commits ever holds such a file as healthy.
    """
    if row.id is not None and row.id in unresolved_ids:
        reflagged_ids.add(row.id)
        return "modified"
    # A healthy row never carries kept prior contents (add-scan-safety-nets D3). Every caller
    # assigns the returned status to `row.status`, so the row is about to read `ok`.
    if row.prior_sha256 is not None:
        _clear_prior(row)
    return "ok"


async def _reconcile_moves(
    session: AsyncSession,
    collection: Collection,
    new_rows: list[FileEntry],
    newly_missing: list[FileEntry],
    now: datetime,
    summary: RunSummary,
    unresolved_ids: set[int] | None = None,
    reflagged_ids: set[int] | None = None,
) -> set[int]:
    """Content-address moved/renamed files within one scan; return reconciled missing-row ids.

    A file newly classified ``missing`` whose stored ``(sha256, size)`` matches **exactly one**
    newly-``added`` file — a key shared by no other missing or added row in this run — is the same
    file relocated, not an independent deletion + addition. Such a pair is reconciled into a single
    surviving row that keeps its identity (``first_seen``, ``sha256``, and the whole OTS proof
    record — ``ots_path``, ``ots_state``, ``ots_stamped_at`` and the recorded provenance
    ``ots_digest``, all unchanged) and is repointed to the new path with status ``ok``; the added
    row is dropped and one informational ``moved`` event records the old → new path. Ambiguous keys
    (matching >1 candidate on either side) and zero-byte files never reconcile — they fall back to
    plain ``missing`` + ``added`` (logged).

    Mutates the index only (never collection bytes, and never the proof store) and never re-queues a
    move for OTS stamping (the surviving row stays ``ok``, not ``pending``).

    The retained ``ots_path`` therefore names the OLD relpath's canonical proof location — still the
    true location of the proof. What happens next belongs entirely to the notarization capability
    (:mod:`src.services.proofs`), which owns both halves: its referenced-slot stamp guard keeps any
    stamp from displacing that proof while a row records it, and its healing sweep — the one code
    path that relocates proofs — later brings it to the new relpath's canonical location. A scan
    must not do either; relocating from here would add a second proof-mutation path with its own
    crash windows and its own ordering hazard against this same scan's stamp pass (GitHub #39).
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
        # Healthy unless the file carries an unresolved change alert (design D12): a changed file
        # that is then renamed is still changed.
        m.status = _healthy_status(
            m,
            unresolved_ids if unresolved_ids is not None else set(),
            reflagged_ids if reflagged_ids is not None else set(),
        )
        m.last_checked = now
        # Preserve first_seen / sha256 / ots_path / ots_state / ots_stamped_at / ots_digest —
        # identity, proof, provenance and notarization history follow the file to its new path. The
        # proof FILE is not touched here (see the docstring): the notarization capability's guard
        # protects it where it is, and its healing sweep moves it.
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
    session: AsyncSession,
    collection: Collection,
    *,
    deep: bool = False,
    allow_empty: bool = False,
) -> RunSummary:
    """Scan one collection, writing files/events and a runs row. Never raises on per-file errors.

    When ``deep`` is True every tracked, non-missing file is re-hashed regardless of its size and
    mtime (catching silent bit-rot the fast-path skips); a digest that changed while size and mtime
    did not is reported as possible corruption (design D2).

    The pass refuses a root that is not a directory, and a root over which the walk saw no file at
    all while the collection tracks at least one non-``missing`` record — unless ``allow_empty`` is
    given (the CLI's ``--allow-empty``; the scheduler and the panel never pass it). A refusal changes
    no file record, writes no event, and finalises the run ``error`` with its reason (design D1).
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
    # When this collection row was created: part of the alert-attempt key, so a new collection
    # that reuses a deleted one's id does not inherit its alert limit.
    collection_created = collection.created_at
    collection_alert_json = collection.alert_json
    collection_name = collection.name
    summary = RunSummary(collection_id=collection_id)
    perfile = collection.ots_mode == "perfile"
    # The collection's path rules, compiled once for the whole pass (add-path-rules D3) and read
    # here, before any rollback can expire `collection`: a rule stored while this pass runs
    # applies from the next one. Consulted in ONE place — the hash-compare branch below.
    rule_set = path_rules.load(collection)

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
        # Design D12: the files carrying an open change alert, read once under the claim. Every
        # branch below that would record an existing row healthy routes through `_healthy_status`
        # with this set; `reflagged_ids` collects the rows the rule changed (counted once).
        unresolved_ids = await _unresolved_change_ids(session, collection_id)
        reflagged_ids: set[int] = set()
        # Records the pass holds at its start, `missing` excluded: the root-refusal check and the
        # mass-change share are both measured against this (design D1, D4).
        tracked_at_start = sum(1 for r in existing.values() if r.status != "missing")
        seen: set[str] = set()
        # Paths that were listed (so: present) but could not be examined in this pass.
        unexamined: set[str] = set()
        # Directory-listing errors from the walk (design D1). Any one makes the walk incomplete:
        # no missing sweep, no move reconciliation, result `partial`.
        walk_errors: list[OSError] = []
        # Collection-relative directories the walk could not list, and whether one of them was the
        # root itself (or could not be placed under it).
        unreadable_dirs: list[str] = []
        unreadable_all = False
        # Files the walk yielded at all (before any per-file skip) — "did the walk see any file".
        walked_files = 0
        # Ids of files found back at the contents kept before an unaccepted change (design D3).
        # `_drain` system-acknowledges their open change alerts in the same transaction that commits
        # the `ok` status, exactly as `restored_ids` does for `missing` alerts.
        returned_ids: list[int] = []
        # Silent re-baselines of this pass — churn policy or a path rule: (row, sha, size, mtime)
        # it replaced (design D4; add-path-rules D3).
        churn_rebaselines: list[tuple[FileEntry, str | None, int, float | None]] = []
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
            nonlocal added_buffer, restored_ids, returned_ids
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
            # A return closes EVERY open change alert of the file (`modified`, `restored_changed`,
            # `corrupted`) in the transaction that commits its `ok` status — never one without the
            # other, or a healthy file would wear an open alert (or an alerted file read healthy).
            for i in range(0, len(returned_ids), ACK_CHUNK):
                chunk = returned_ids[i : i + ACK_CHUNK]
                await session.execute(
                    update(Event)
                    .where(
                        Event.file_id.in_(chunk),
                        Event.kind.in_(UNRESOLVED_CHANGE_KINDS),
                        Event.acknowledged_at.is_(None),
                    )
                    .values(acknowledged_at=now, acknowledged_by=None)
                    .execution_options(synchronize_session=False)
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
            returned_ids = []

        def _return_to_prior(row: FileEntry, sha: str, size: int, mtime: float) -> None:
            """The file is back at the contents kept before its unaccepted change (design D3).

            Called only on digest EQUALITY with ``row.prior_sha256`` — nothing weaker. The row
            becomes ``ok``, the kept contents are discarded, its open change alerts are queued for
            the system acknowledgement `_drain` issues in the same commit, and one informational
            ``restored`` event (born acknowledged) records the return. No alarm.
            """
            row.size, row.mtime, row.sha256 = size, mtime, sha
            row.last_checked = row.last_changed = now
            row.status = "ok"
            _clear_prior(row)
            if perfile:
                # The proof at this path commits to the changed bytes; these bytes get their own.
                row.ots_state = "pending"
            returned_ids.append(row.id)
            # Out of the pass's open-alert set, so `_healthy_status` and the end-of-pass sweep
            # (which re-reads the set after this commit) agree the file is healthy.
            unresolved_ids.discard(row.id)
            reflagged_ids.discard(row.id)
            session.add(
                Event(
                    collection_id=collection.id,
                    file_id=row.id,
                    kind="restored",
                    detected_at=now,
                    acknowledged_at=now,
                    acknowledged_by=None,
                    detail=RETURN_DETAIL,
                )
            )
            summary.reverted += 1

        # The refusal's "<kind>: <sentence>", for `runs.error_sample` (design D1).
        refusal_entry = ""
        try:
            # Design D1, before the walk: the root must be an existing directory. `os.path.isdir`
            # treats every OSError as "no", which is the reading that destroys nothing.
            if not os.path.isdir(root):
                raise ScanRefused(REFUSAL_UNREACHABLE, f"the folder {root} is not reachable")
            for relpath in iter_relpaths(root, globs, walk_errors):
                # `walked_files` answers one question for the empty-root refusal: "did this walk
                # find any file Cairn can TRACK?" — a regular file with a storable name, whether or
                # not it could then be examined. A symlink (ignored) and an un-storable name (never
                # tracked) do not count: neither is evidence that the tracked files' folder is
                # really there, and an otherwise empty (unmounted) root must not pass as populated
                # because one of them is lying in it.
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
                    # The name was listed, so the file is THERE; it just could not be examined
                    # (search permission gone, an I/O error). It is counted as skipped — and it is
                    # `seen`, so the missing sweep cannot report a file we failed to read as a file
                    # that was deleted. It is also `unexamined`: nothing may conclude from `seen`
                    # that its bytes were looked at in this pass.
                    walked_files += 1
                    seen.add(relpath)
                    unexamined.add(relpath)
                    continue
                if is_symlink:
                    continue  # conservative: never follow symlinks out of the read-only jail
                walked_files += 1
                try:
                    st = full.stat()
                except OSError:
                    # Silent until 0012: the file was counted into `partial` and named nowhere.
                    summary.errors += 1
                    if len(error_entries) < RUN_ERROR_SAMPLE_MAX:
                        error_entries.append(_render_skip("stat", relpath))
                    seen.add(relpath)  # present but unreadable — never "missing" (see above)
                    unexamined.add(relpath)
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
                        recorded_size, recorded_mtime = row.size, row.mtime
                        sha = await _hash(full)
                        # Design D3, order of tests on reappearance: equals the recorded digest (the
                        # `else` below, unchanged); equals the KEPT digest — the file was changed,
                        # then went missing, and a copy of the original came back — a return;
                        # otherwise it came back different. Digest equality only.
                        returned = (
                            prior is not None
                            and sha != prior
                            and row.prior_sha256 is not None
                            and sha == row.prior_sha256
                        )
                        # The index must keep describing the bytes on disk NOW (sprint-1's verify
                        # blame attribution reads `files.sha256` as the last-seen digest), so the
                        # recorded digest is still updated in every outcome. The fix is *compare
                        # before overwrite*, not *stop overwriting*.
                        row.sha256 = sha
                        row.size, row.mtime = size, mtime
                        row.last_checked = row.last_changed = now
                        if returned:
                            _return_to_prior(row, sha, size, mtime)
                        elif prior is not None and sha != prior:
                            # Something is back at the path, but it is not what left: a wrong backup
                            # snapshot, a truncated restore, or bytes planted while the real file was
                            # known-absent. It alarms in BOTH modes — a churn collection exempts
                            # ordinary edits, and a file that was absent and came back different is
                            # not an ordinary edit (design D4). Reusing `modified` would be silent
                            # there, which is why `restored_changed` exists.
                            # Keep the contents from before the change (design D3) — only when the
                            # file was healthy when it went missing (no open change alert): then the
                            # digest recorded at that moment is the last-healthy one. If it carried
                            # an unaccepted change, that digest is the unaccepted version and must
                            # never become "prior" (it could later close the issue as a return); an
                            # already-kept prior is left as it is by `_keep_prior` itself.
                            if row.id not in unresolved_ids:
                                _keep_prior(row, prior, recorded_size, recorded_mtime)
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
                            # Matching the bytes on record does not resolve an earlier, unaccepted
                            # change to them (design D12): such a row comes back `modified`.
                            row.status = _healthy_status(row, unresolved_ids, reflagged_ids)
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
                    elif (
                        deep
                        or row.size != size
                        or row.mtime != mtime
                        or row.sha256 is None
                        # A file with an unaccepted change is re-read on EVERY pass, not only when
                        # its size or date moved: a restore that preserves metadata (restic,
                        # `rsync -a`, `cp -p`) puts the original bytes back under the very size and
                        # mtime that are on record — for a corrupted file they never differed — and
                        # the quick pass would otherwise never notice that the issue closed itself.
                        # Bounded: only open issues carry a prior.
                        or row.prior_sha256 is not None
                    ):
                        # Deep pass re-hashes every file; a normal pass only when size/mtime moved or
                        # no prior hash exists. Either way the sha comparison below classifies it.
                        sha = await _hash(full)
                        if row.sha256 is None:
                            # No digest was on record (a manifest import that never hashed): there
                            # is nothing to compare against, so this is not a change, and certainly
                            # not corruption (design D2). Record the digest; preserve the status.
                            row.size, row.mtime, row.sha256 = size, mtime, sha
                            row.last_checked = now
                            summary.ok += 1
                        elif row.prior_sha256 is not None and sha == row.prior_sha256:
                            # Back at the contents kept before an unaccepted change (design D3).
                            # Checked before every other classification: a return is decided by
                            # digest equality with the kept digest and nothing else.
                            _return_to_prior(row, sha, size, mtime)
                        elif sha != row.sha256:
                            before = (row.sha256, row.size, row.mtime)
                            # Healthy before this change: no open change alert, status ok/new.
                            was_healthy = row.id not in unresolved_ids and row.status in ("ok", "new")
                            # Design D2: the digest moved while size AND mtime both equal the
                            # recorded ones — bit-rot, or a write that bypassed the file system's
                            # bookkeeping. Never the routine editing `churn` exempts. A row with no
                            # modification time on record (a manifest import that carried none) has
                            # no evidence either way: a same-size digest change is then reported as
                            # possible corruption rather than absorbed, because "the date moved" is
                            # the only thing that makes a change an ordinary edit.
                            is_corruption = row.size == size and (
                                row.mtime is None or row.mtime == mtime
                            )
                            row.size, row.mtime, row.sha256 = size, mtime, sha
                            row.last_checked = row.last_changed = now
                            # Content changed: re-queue for a fresh stamp (each distinct content
                            # state gets its own proof). Applies to both worm and churn collections.
                            if perfile:
                                row.ots_state = "pending"
                            # Quiet: an ordinary change the policy (`churn`) or a path rule says
                            # is expected (add-path-rules D3). Decided only after the return
                            # and corruption checks above; neither of those consults the rules.
                            quiet = collection.mode == "churn" or rule_set.matches(relpath)
                            if is_corruption or not quiet:
                                kind = "corrupted" if is_corruption else "modified"
                                if was_healthy:
                                    _keep_prior(row, *before)
                                row.status = "modified"
                                session.add(
                                    Event(
                                        collection_id=collection.id,
                                        file_id=row.id,
                                        kind=kind,
                                        detected_at=now,
                                        detail=_change_detail(relpath, before, (sha, size, mtime)),
                                    )
                                )
                                summary.modified += 1
                                if is_corruption:
                                    summary.corrupted += 1
                                _record_alarm(kind, relpath)
                            else:
                                # Change is expected (churn policy or a path rule): silently
                                # re-baseline, no nag — unless an earlier alarm on this file is
                                # still open (design D12).
                                # ...and, where the change is quiet only because a RULE says so
                                # (a `worm` collection), unless the file is already an issue at
                                # all: a row recorded `modified` with NO open alert (an alert
                                # closed by the retired "mark reviewed" verb) is still listed for
                                # review, and a rule resolves nothing — only an accept or an exact
                                # return takes it off that list. Its kept contents stay as they
                                # are. Under `churn` the long-standing behaviour is unchanged.
                                if collection.mode == "churn" or row.status != "modified":
                                    row.status = _healthy_status(row, unresolved_ids, reflagged_ids)
                                summary.ok += 1
                                if row.status == "ok" and was_healthy:
                                    # Remembered for the mass-change ceiling (design D4).
                                    churn_rebaselines.append((row, *before))
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
                    # Present (so never missing) but its bytes were not read in this pass: nothing
                    # may treat it as verified — in particular a deep pass must not graduate it.
                    unexamined.add(relpath)
                    continue

                processed += 1
                if processed % BATCH == 0:
                    await _drain()

            # Design D1, after the walk. Unreadable directories first: they take precedence over
            # the empty-root refusal (a walk that saw nothing because nothing could be listed is an
            # unreadable folder, not an empty one). Each is counted among the run's errors and
            # sampled FIRST, ahead of any per-file skip, so the alert rate limit can key on the
            # sample's first entry.
            if walk_errors:
                summary.errors += len(walk_errors)
                walk_entries = []
                for err in walk_errors:
                    where = getattr(err, "filename", None)
                    try:
                        rel = os.path.relpath(os.fsdecode(where), root) if where else "?"
                    except (TypeError, ValueError):
                        rel = "?"
                    rel = rel.replace(os.sep, "/")
                    # Only a real parent reference — a directory may legitimately be NAMED `..x`.
                    if rel in ("?", ".", "..") or rel.startswith("../"):
                        # The root itself, or a directory we cannot place: nothing under the root
                        # can be vouched for.
                        unreadable_all = True
                    else:
                        unreadable_dirs.append(rel)
                    if len(walk_entries) < RUN_ERROR_SAMPLE_MAX:
                        walk_entries.append(_render_skip(WALK_UNREADABLE, rel))
                error_entries[:0] = walk_entries
                del error_entries[RUN_ERROR_SAMPLE_MAX:]
                # What an unreadable directory takes away is knowledge of the files BENEATH it —
                # nothing else. So only those are shielded from the missing sweep, and the pass is
                # "incomplete" (not a deep verification, worth an alert) only when a tracked file
                # actually lies beneath one. A root-owned `lost+found` or a NAS recycle folder that
                # this process can never list, with nothing tracked inside, must not switch off
                # deletion detection for the whole collection or keep its deep pass owed forever.
                summary.walk_incomplete = unreadable_all or any(
                    row.status != "missing" and _beneath(relpath, unreadable_dirs)
                    for relpath, row in existing.items()
                )
                logging.getLogger("cairn.scanner").warning(
                    "collection %s: %d director(y/ies) under the root could not be listed — files "
                    "beneath them are not marked missing by this pass%s; run is partial",
                    collection_id,
                    len(walk_errors),
                    "" if summary.walk_incomplete else " (no tracked file lies beneath them)",
                )
            elif walked_files == 0 and tracked_at_start > 0 and not allow_empty:
                # An empty mount point is the usual shape of a volume that did not mount, and it is
                # indistinguishable from "everything was deleted": the reading that destroys nothing
                # wins, and the rare deliberate case has an explicit override.
                raise ScanRefused(
                    REFUSAL_EMPTY,
                    f"the folder {root} is empty but {tracked_at_start:,} files are tracked"
                    " (if it was emptied on purpose, run: cairn scan --allow-empty)",
                )

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
            # Beneath a directory that could not be listed, "gone" cannot be told from "could not
            # be read": those files are no candidates (design D1). Everything else is judged as
            # usual — one unreadable folder does not blind the pass to the rest of the collection.
            newly_missing = (
                []
                if unreadable_all
                else [
                    row
                    for relpath, row in existing.items()
                    if relpath not in seen
                    and row.status != "missing"
                    and not _beneath(relpath, unreadable_dirs)
                ]
            )

            # Move/rename reconciliation: a candidate-missing file whose content (sha256 + size)
            # uniquely matches one newly-added file is the same file relocated, not a deletion +
            # addition. Reconcile it in place (preserves identity/proof) and emit one `moved` event.
            reconciled_ids = await _reconcile_moves(
                session,
                collection,
                new_rows,
                newly_missing,
                now,
                summary,
                unresolved_ids,
                reflagged_ids,
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
                    # ...and only files this pass actually examined: one that was merely listed
                    # (its metadata could not be read) was not re-hashed and must not graduate.
                    if row.status == "new" and relpath in seen and relpath not in unexamined:
                        row.status = _healthy_status(row, unresolved_ids, reflagged_ids)
                        if row.status == "ok":
                            summary.baselined += 1
                if summary.baselined:
                    logging.getLogger("cairn.scanner").info(
                        "collection %s: auto-baselined %d intact new file(s) to ok on deep pass",
                        collection.id,
                        summary.baselined,
                    )

            # Design D4, the mass-change ceiling, over EVERY quiet re-baseline of this pass — churn
            # policy or path rule, so in a worm collection too (add-path-rules D3); changes a worm
            # collection reports anyway never enter `churn_rebaselines`. Evaluated once, on this
            # pass's own silent re-baselines, before the D12 sweep so the events it writes are part
            # of the set that sweep re-reads.
            q = len(churn_rebaselines)
            if q:
                from ..config import get_settings

                _settings = get_settings()
                percent = _settings.mass_change_percent
                if (
                    percent > 0
                    and q >= _settings.mass_change_min_files
                    and q * 100 >= percent * tracked_at_start
                ):
                    for row, old_sha, old_size, old_mtime in churn_rebaselines:
                        row.status = "modified"
                        _keep_prior(row, old_sha, old_size, old_mtime)
                        session.add(
                            Event(
                                collection_id=collection.id,
                                file_id=row.id,
                                kind="modified",
                                detected_at=now,
                                detail=_change_detail(
                                    row.relpath,
                                    (old_sha, old_size, old_mtime),
                                    (row.sha256, row.size, row.mtime),
                                    mass=q,
                                ),
                            )
                        )
                        _record_alarm("modified", row.relpath)
                    summary.mass_change = q
                    summary.modified += q
                    summary.ok -= q
                    logging.getLogger("cairn.scanner").warning(
                        "collection %s: %d of %d tracked file(s) changed in one pass — over the "
                        "mass-change ceiling (%d files and %d%%); every one is now `modified` with "
                        "an open alert instead of being silently re-baselined",
                        collection_id,
                        q,
                        tracked_at_start,
                        _settings.mass_change_min_files,
                        percent,
                    )

            # Design D12, the end layer: after every other classification step of the pass and in
            # the same transaction as its final batch, no held row carrying an open change alert
            # may read `ok`/`new`. On the ORM objects, never a bulk UPDATE — a later flush of a
            # stale in-memory status would overwrite it. Catches paths nobody routed through
            # `_healthy_status` and rows left `ok` by earlier versions. `missing` is never touched.
            #
            # The set read at the start of the pass is re-read here, under the same transaction as
            # the final batch. The panel refuses an accept while a scan runs, but `cairn accept`
            # does not, so an alert can be acknowledged mid-pass: a row this pass held `modified`
            # on the strength of the earlier read is then healthy, and leaving it flagged would
            # show an issue the operator has already accepted, with nothing open behind it. Only
            # rows THIS rule flagged are returned to `ok` — never a row a classification branch
            # found changed, which carries its own open event and is therefore still in the set.
            current_ids = await _unresolved_change_ids(session, collection_id)
            for row in existing.values():
                if row.id in reflagged_ids and row.id not in current_ids and row.status == "modified":
                    row.status = "ok"
                    _clear_prior(row)  # a healthy row never carries a prior
                    reflagged_ids.discard(row.id)
                elif row.id in current_ids and row.status in ("ok", "new"):
                    row.status = "modified"
                    reflagged_ids.add(row.id)
            # ...and the same statement made against the DATABASE, not only against the rows this
            # pass holds. A held row can already read `modified` while the stored row reads `ok`
            # (an accept from another connection wrote it mid-pass); assigning `modified` to it
            # again is no change as far as the ORM is concerned, so nothing would be written and a
            # change this very pass has just recorded an alert for would commit as healthy. Flushed
            # first, so the two cannot disagree about a row; chunked for the parameter ceiling.
            if current_ids:
                await session.flush()
                ids = sorted(current_ids)
                for i in range(0, len(ids), ACK_CHUNK):
                    await session.execute(
                        update(FileEntry)
                        .where(
                            FileEntry.collection_id == collection_id,
                            FileEntry.id.in_(ids[i : i + ACK_CHUNK]),
                            FileEntry.status.in_(("ok", "new")),
                        )
                        .values(status="modified")
                        .execution_options(synchronize_session=False)
                    )
            summary.reflagged = len(reflagged_ids)
            if summary.reflagged:
                logging.getLogger("cairn.scanner").info(
                    "collection %s: kept %d file(s) `modified` that carry an open, unaccepted "
                    "change alert (no new event, no alert)",
                    collection_id,
                    summary.reflagged,
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
        except ScanRefused as refusal:
            # Design D1: a refused root. Nothing about any file was written (the refusal fires
            # before the walk, or after a walk that saw no file and so had nothing to commit), and
            # the run finalises `error` with the reason as the single `error_sample` entry. The
            # rollback discards nothing but is what makes "nothing written" hold by construction.
            logging.getLogger("cairn.scanner").warning(
                "scan of collection %s REFUSED (%s): %s — no file record changed, no event "
                "written; the collection's scan freshness is not refreshed",
                collection_id,
                refusal.kind,
                refusal.sentence,
            )
            summary.result = "error"
            summary.refused = refusal.kind
            summary.refusal = refusal.sentence
            refusal_entry = f"{refusal.kind}: {refusal.sentence}"
            await session.rollback()
            try:
                await session.get(Collection, collection_id)
            except Exception:  # pragma: no cover - the datastore is gone
                logging.getLogger("cairn.scanner").exception(
                    "reloading collection %s after a refused scan failed", collection_id
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
        # A refused pass writes nothing about any file — its stamp tail included (design D1).
        stamp_tail = perfile and summary.refused is None
        if stamp_tail and not lease_lost and not await collections.lease_held(run_id):
            lease_lost = True
            logging.getLogger("cairn.scanner").warning(
                "collection %s: the operation claim (run %s) was RECLAIMED before this scan's stamp "
                "pass — stamping nothing; the queued files stay pending for the next pass",
                collection_id,
                run_id,
            )
        if stamp_tail and not lease_lost:
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
        if summary.refused is not None:
            # One entry, "<kind>: <sentence>"; `runs.errors` stays 0 (it counts skipped files).
            error_sample = json.dumps([_bounded_entry(refusal_entry)])
        else:
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

    # Best-effort alerts AFTER the commit. Dispatch isolates per-channel failures and each call is
    # itself wrapped here, so a notification error never affects the scan result.
    #
    # 1. Newly detected alarming changes: a missing file (any mode), a file that came back with
    #    different bytes (any mode), possible corruption (any mode), a WORM modification, or the
    #    files of a churn mass change. A return to the kept contents never alarms.
    if summary.alarming:
        try:
            parts: list[str] = []
            if summary.missing:
                parts.append(f"{summary.missing} missing")
            # `summary.modified` already includes the restored-changed and possibly-corrupted
            # files (their status IS `modified`), so they are subtracted out and named separately
            # rather than counted twice. With none of them the wording is byte-identical to before.
            plain_modified = summary.modified - summary.restored_changed - summary.corrupted
            if plain_modified > 0:
                if summary.mass_change:
                    parts.append(f"{plain_modified} modified — unusually many for one scan")
                else:
                    parts.append(f"{plain_modified} modified")
            if summary.corrupted:
                parts.append(f"{summary.corrupted} possibly corrupted")
            if summary.restored_changed:
                parts.append(f"{summary.restored_changed} came back changed")
            await _dispatch_alert(
                session,
                collection,
                collection_id,
                collection_name,
                summary=", ".join(parts) or "changes detected",
                paths=[rp for _kind, rp in summary.alarming],
                detected_at=now,
                link_path=f"/collection/{collection_id}/review",
            )
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "alert dispatch failed for collection %s", collection_id
            )

    # 2. A refused root, or a pass that could not list some directories (design D1/D5). Neither
    #    carries file paths; each is repeated at most once per SAFETY_ALERT_INTERVAL_SECONDS per
    #    collection — a rate limit, not a once-per-outage dedupe, because dispatch is best-effort
    #    and a lost alert must be followed by another within a day.
    safety: tuple[tuple[str, ...], str] | None = None
    if summary.refused is not None:
        safety = (
            REFUSAL_KINDS,
            f"scan refused, nothing was checked — {refusal_entry.split(': ', 1)[-1]}",
        )
    elif summary.walk_incomplete and not lease_lost:
        n = len(walk_errors)
        safety = (
            (WALK_UNREADABLE,),
            f"{n} folder{'s' if n != 1 else ''} could not be read — the files beneath "
            f"{'them' if n != 1 else 'it'} are not being checked",
        )
    if safety is not None:
        prefixes, text = safety
        try:
            due = True
            try:
                marker = _safety_alert_key(collection_id, collection_created, prefixes)
                due = await _safety_alert_due(session, marker, now)
            except Exception:
                # Unknown → send: an extra alert is the cheap error, a lost one is not.
                logging.getLogger("cairn.scanner").exception(
                    "checking the alert rate limit failed for collection %s; alerting anyway",
                    collection_id,
                )
            if due:
                # Marked BEFORE dispatch: the limit is on attempts, and an attempt that raises
                # must still count, or a broken channel would be retried on every scan. Marked
                # only when a channel is enabled, though: with none there is no attempt to limit,
                # and a marker would silence the first alert after one is switched on.
                if _any_channel_enabled(collection_alert_json):
                    try:
                        await _safety_alert_mark(session, marker, now)
                    except Exception:
                        await session.rollback()
                        logging.getLogger("cairn.scanner").exception(
                            "recording the alert attempt failed for collection %s", collection_id
                        )
                await _dispatch_alert(
                    session,
                    collection,
                    collection_id,
                    collection_name,
                    summary=text,
                    paths=[],
                    detected_at=now,
                    link_path=f"/collection/{collection_id}",
                )
            else:
                logging.getLogger("cairn.scanner").info(
                    "collection %s: %s alert already attempted within the last 24 hours; "
                    "not repeating it",
                    collection_id,
                    "/".join(prefixes),
                )
        except Exception:
            logging.getLogger("cairn.scanner").exception(
                "alert dispatch failed for collection %s", collection_id
            )

    return summary


def _safety_alert_key(
    collection_id: int, created_at: datetime | None, prefixes: tuple[str, ...]
) -> str:
    """Where the time of the last refusal / unreadable-folder alert ATTEMPT is kept.

    The collection's creation time is part of the key: `collections.id` is a rowid, and a new
    collection that reuses a deleted one's id must not inherit its alert limit.
    """
    klass = "unreadable" if prefixes == (WALK_UNREADABLE,) else "refusal"
    born = int(created_at.timestamp()) if created_at is not None else 0
    return f"safety_alert_at:{collection_id}:{klass}:{born}"


def _any_channel_enabled(alert_json: str | None) -> bool:
    """True when the collection routes alerts to at least one enabled channel."""
    try:
        routing = json.loads(alert_json or "{}")
    except ValueError:
        return False
    if not isinstance(routing, dict):
        return False
    return any(isinstance(v, dict) and v.get("enabled") for v in routing.values())


async def _safety_alert_due(session: AsyncSession, marker: str, now: datetime) -> bool:
    """Whether a refused-root / unreadable-folder alert is due (design D1).

    Not due when such an alert was last ATTEMPTED for this collection within
    :data:`SAFETY_ALERT_INTERVAL_SECONDS`. Keyed on the attempt, not on earlier refused runs: with
    scans more frequent than the interval a refused run always exists inside the window, so keying
    on runs would send exactly one alert per outage — and dispatch is best-effort, so that one can
    be lost. Keyed on the attempt, a lasting outage is re-announced once a day. The time is a row
    in the `app_settings` key-value table (no schema of its own; nothing else reads the key).
    """
    from ..models.db import AppSetting

    last = await session.scalar(select(AppSetting.value).where(AppSetting.key == marker))
    if not last:
        return True
    try:
        attempted = datetime.fromisoformat(last)
    except ValueError:
        return True
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    return (now - attempted).total_seconds() >= SAFETY_ALERT_INTERVAL_SECONDS


async def _safety_alert_mark(session: AsyncSession, marker: str, now: datetime) -> None:
    """Record that a refusal / unreadable-folder alert was attempted now."""
    from ..models.db import AppSetting

    row = await session.get(AppSetting, marker)
    if row is None:
        session.add(AppSetting(key=marker, value=now.isoformat()))
    else:
        row.value = now.isoformat()
    await session.commit()


async def _dispatch_alert(
    session: AsyncSession,
    collection: Collection,
    collection_id: int,
    collection_name: str,
    *,
    summary: str,
    paths: list[str],
    detected_at: datetime,
    link_path: str,
) -> None:
    """Build and dispatch one alert for a collection. Callers wrap this: it may raise."""
    from ..config import get_settings
    from ..notify.base import Alert
    from ..notify.dispatch import dispatch as notify_dispatch
    from . import app_settings

    # The deep link is a convenience; being told at all is the product. So the two things on the
    # way to it — resolving the effective settings, then building the URL — get SEPARATE guards, and
    # neither may take the other down with it. The caller's block protects the *scan*; these two
    # protect the *alert*.
    #
    # Guard 1 — the settings overlay. What it returns is the alert's *transport* config, not just
    # the link's address: a deployment that configures SMTP from the panel keeps host/user/password
    # in `app_settings`, and the env-only fallback has no smtp_host at all. So the fallback is used
    # only when the overlay itself fails; a successful overlay must survive anything that goes
    # wrong afterwards, or a cosmetic link bug would downgrade the settings until SmtpNotifier
    # raises "SMTP host is not configured" and the one channel actually in production silently
    # sends nothing.
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

    # Guard 2 — the link itself, around the synchronous builder only. A corrupt stored public_url
    # or a builder bug costs a click, never the alert.
    review_url: str | None = None
    try:
        from .panel_url import panel_link

        review_url = panel_link(eff_settings.public_url, link_path)
    except Exception:
        review_url = None
        logging.getLogger("cairn.scanner").exception(
            "building the alert review link failed for collection %s; sending a link-free alert",
            collection_id,
        )

    alert = Alert(
        collection_name=collection_name,
        summary=summary,
        paths=paths,
        detected_at=detected_at,
        url=review_url,
    )
    await notify_dispatch(alert, collection, eff_settings)


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
            # An accept makes the current contents the baseline; what was kept before the change
            # is discarded, so a later return to it reads as a change (design D3).
            _clear_prior(f)
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
        _clear_prior(file)  # design D3: an accept discards the kept contents
        accepted = 1

    for e in events:
        e.acknowledged_at = now
        e.acknowledged_by = user_id

    await session.commit()
    return {"accepted": accepted, "removed": removed, "events_ack": len(events)}


# Ids per statement in `accept_files` — comfortably under SQLite's bound-parameter ceiling (999)
# with room for the statement's own collection/status parameters. A module constant so a test can
# drive the chunking.
ACCEPT_FILES_CHUNK = 500


async def accept_files(
    session: AsyncSession,
    collection: Collection,
    file_ids,
    user_id: int | None,
) -> dict[str, int]:
    """Accept an explicit set of file records of ONE collection (design D6).

    For callers that have already established exactly which records the operator authorised (the
    panel's folder-scoped bulk verbs hand over the ids their fingerprint recount just hashed). Per
    record, the consequence :func:`accept_file` applies: ``new``/``modified`` → ``ok`` with its open
    events acknowledged; ``missing`` → its events detached (path carried into ``detail``) and
    acknowledged by :func:`_detach_ack_backfill` *before* the row is deleted. Nothing else in the
    collection is touched — no other record, no other open event.

    The id set is a claim, not a trust boundary: every statement re-asserts ``collection_id`` and the
    status it expects, so an id of another collection, an ``ok`` row or an id that no longer exists
    is simply not acted on. Ids are de-duplicated and chunked (:data:`ACCEPT_FILES_CHUNK`) so the
    set's size is not limited by SQLite's bound-parameter ceiling. Everything happens in ONE
    transaction with one ``commit()`` at the end (the panel caller already holds the write lock).
    ``events_ack`` counts only events this call acknowledged; an already-acknowledged event keeps its
    original timestamp and acknowledger.
    """
    now = _utcnow()
    ids = sorted({int(i) for i in file_ids})
    accepted = removed = events_ack = 0

    for i in range(0, len(ids), ACCEPT_FILES_CHUNK):
        chunk = ids[i : i + ACCEPT_FILES_CHUNK]

        # Re-assert collection + status, reading this chunk's two populations before mutating.
        changed_ids = list(
            await session.scalars(
                select(FileEntry.id).where(
                    FileEntry.id.in_(chunk),
                    FileEntry.collection_id == collection.id,
                    FileEntry.status.in_(("new", "modified")),
                )
            )
        )
        missing_ids = list(
            await session.scalars(
                select(FileEntry.id).where(
                    FileEntry.id.in_(chunk),
                    FileEntry.collection_id == collection.id,
                    FileEntry.status == "missing",
                )
            )
        )

        if changed_ids:
            open_changed = (
                Event.collection_id == collection.id,
                Event.file_id.in_(changed_ids),
                Event.acknowledged_at.is_(None),
            )
            events_ack += (
                await session.scalar(select(func.count()).select_from(Event).where(*open_changed))
            ) or 0
            await session.execute(
                update(Event)
                .where(*open_changed)
                .values(acknowledged_at=now, acknowledged_by=user_id)
            )
            await session.execute(
                update(FileEntry)
                .where(
                    FileEntry.id.in_(changed_ids),
                    FileEntry.collection_id == collection.id,
                    FileEntry.status.in_(("new", "modified")),
                )
                # Design D3: an accept discards the contents kept before the change.
                .values(status="ok", prior_sha256=None, prior_size=None, prior_mtime=None)
            )
            accepted += len(changed_ids)

        if missing_ids:
            events_ack += (
                await session.scalar(
                    select(func.count())
                    .select_from(Event)
                    .where(
                        Event.collection_id == collection.id,
                        Event.file_id.in_(missing_ids),
                        Event.acknowledged_at.is_(None),
                    )
                )
            ) or 0
            # Detach + backfill + ack BEFORE the delete, or ON DELETE CASCADE takes the history.
            await session.execute(
                _detach_ack_backfill(
                    (Event.collection_id == collection.id) & Event.file_id.in_(missing_ids),
                    now,
                    user_id,
                )
            )
            await session.execute(
                delete(FileEntry).where(
                    FileEntry.id.in_(missing_ids),
                    FileEntry.collection_id == collection.id,
                    FileEntry.status == "missing",
                )
            )
            removed += len(missing_ids)

    await session.commit()
    return {"accepted": accepted, "removed": removed, "events_ack": events_ack}
