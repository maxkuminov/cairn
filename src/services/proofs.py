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
from dataclasses import dataclass
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


def _anchor_verdict(result: ots.VerifyResult) -> str | None:
    """Classify a backend answer about an existing proof's Bitcoin anchor (design D1a).

    ``'confirmed'`` — an attestation was validated against its real block: the artifact on disk is
    one Cairn may stand behind. ``'disproven'`` — the backend ANSWERED and the answer does not
    confirm (the attestation does not commit to its block, or the proof commits to other bytes).
    ``None`` — nothing was established: the backend was unreachable, or (the node backend) cannot
    tell "not confirmed" from "unreachable" apart at all.

    The three map onto :func:`ots._place_proof`'s three branches, and the ``None`` case must stay
    ``None``: an outage that produced a verdict would let anyone who can take the backend offline
    buy a ``complete`` notarization for an unverified artifact.
    """
    if result.verified:
        return "confirmed"
    if result.proof_mismatch or result.digest_mismatch:
        return "disproven"
    return None


async def _adopt_or_verdict(
    entry: FileEntry, out: Path, settings: Settings
) -> tuple[bool, str | None]:
    """Decide whether ``entry``'s existing canonical proof may be adopted; else return a verdict.

    Returns ``(adopted, verdict)``. Adoption records a proof Cairn did not just place AND writes
    provenance from it, so a bare same-digest match is not enough — an attacker who can write into
    the proof store can leave a well-formed ``.ots`` committing to the file's real digest. All three
    of design D1a's conditions must hold:

    1. the canonical proof PARSES; and
    2. it commits to the digest recorded for the file (``entry.sha256``) — the file's own bytes
       corroborate the value about to be written to ``ots_digest``; and
    3. its Bitcoin anchor VERIFIES against the configured backend at this moment.

    The row's own ``ots_digest`` is deliberately NOT a substitute for (3) and must not short-circuit
    the lookup: it records the digest a proof Cairn placed committed to — a fact about the watched
    file's bytes — not an identity of the artifact at that path. Unboundedly many distinct ``.ots``
    files commit to one digest, so "recorded provenance equals the digest of the file now there" is
    satisfied BY the swap itself. The column's job is detection, never authentication.

    Declining to adopt costs one calendar round-trip. Adopting wrongly records provenance for a
    proof nothing vouched for, so every uncertain case declines.
    """
    if not out.exists():
        return False, None
    facts = await asyncio.to_thread(ots.read_proof_facts, out)
    row_sha = (entry.sha256 or "").strip().lower()
    if not (facts.readable and facts.digest and row_sha and facts.digest == row_sha):
        # Different digest, or unreadable, or no recorded baseline: nothing to adopt and nothing
        # established about the anchor, so no verdict travels down. Placement archives and places.
        return False, None
    if not facts.anchored:
        # An `incomplete` same-digest proof is NEVER adopted: it has no anchor to verify, and
        # freezing a never-anchored proof in place with no submission behind it is exactly what
        # `stale_incomplete` exists to make refreshable. Placement archives it and places a fresh
        # proof (design D1a).
        return False, None

    result = await asyncio.to_thread(
        ots.verify,
        str(out),
        row_sha,
        backend=settings.verify_backend,
        explorer_url=settings.explorer_url,
        node_rpc_url=settings.node_rpc_url,
    )
    verdict = _anchor_verdict(result)
    if verdict == "confirmed":
        entry.ots_path = str(out)
        entry.ots_state = "complete"
        entry.ots_digest = facts.digest
        # `ots_stamped_at` is deliberately NOT moved forward: no submission was made here, and the
        # proof's own attestation carries the real date. Stamping a years-old anchor with today's
        # date is the same class of lie as labelling a download "existed by <ots_stamped_at>".
        log.info(
            "adopted the existing proof for %s (digest %s) — anchor confirmed by Bitcoin block %s; "
            "no calendar round-trip taken",
            entry.relpath,
            facts.digest,
            result.block_height,
        )
        return True, "confirmed"
    return False, verdict


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

    Before any staging symlink or calendar traffic, a pending file whose canonical path already
    holds a proof is offered to :func:`_adopt_or_verdict`: a proof that parses, commits to the row's
    own ``sha256`` AND whose anchor verifies right now is ADOPTED (recorded, no submission), and the
    backend's answer about any other existing proof is carried into placement as a verdict so a
    forged attestation cannot hold the canonical path (design D1/D1a). Whatever is on disk is
    preserved either way — :func:`ots._place_proof` never destroys a proof.

    The caller must hold the collection's single-operation claim (design D10). Inspect -> preserve ->
    place -> record is check-then-act, so two writers would be a lost-update machine; the claim is
    taken by the caller (a scan's own ``running`` run, ``run_stamp_backfill``, or the CLI) rather
    than here, because this function is called from inside a scan that already holds one.
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
    # The proof-store root bounds the "can this proof path be written?" pre-check: only the
    # components Cairn creates under it (`<collection_id>/<relpath>.ots`) are length-checked. The
    # store root's own components are the operator's and must never make a proof look permanently
    # unwritable — that would drop a whole collection to `ots_state='none'`.
    store_root = Path(settings.proof_store_path)
    calendars = settings.ots_calendars

    # Only stamp files still on disk; a vanished file stays pending for the next scan to reclassify.
    work: list[tuple[FileEntry, Path, Path]] = []
    for entry in pending:
        real = root / entry.relpath
        if not real.is_file():
            log.warning("skip stamp, file missing: %s", real)
            continue
        work.append((entry, real, proof_path(settings, collection.id, entry.relpath)))

    # Adoption pass (design D1a): drop from `work` every pending file whose existing canonical
    # proof Cairn may stand behind, and remember the backend's verdict about the ones it may not.
    adopted = 0
    verdicts: list[str | None] = []
    keep: list[tuple[FileEntry, Path, Path]] = []
    for entry, real, out in work:
        try:
            was_adopted, verdict = await _adopt_or_verdict(entry, out, settings)
        except Exception:  # pragma: no cover - adoption is an optimisation, never a failure path
            log.exception("adoption check failed for %s; falling back to a normal stamp", out)
            was_adopted, verdict = False, None
        if was_adopted:
            adopted += 1
            continue
        keep.append((entry, real, out))
        verdicts.append(verdict)
    work = keep

    batch_size = max(1, settings.ots_stamp_batch_size)
    now = _utcnow()
    stamped = adopted
    skipped = 0  # files whose proof path can never be written (e.g. name past the FS byte limit)
    deferred = 0  # same-digest anchored proofs nothing could confirm; nothing recorded, still pending
    for start in range(0, len(work), batch_size):
        chunk = work[start : start + batch_size]
        chunk_verdicts = verdicts[start : start + batch_size]
        # Offload the blocking `ots` subprocess (process spawn + calendar round-trip) to a worker
        # thread so the event loop stays free to serve the panel — mirrors scanner hashing.
        try:
            outcomes = await asyncio.to_thread(
                ots.stamp_batch_via_symlink,
                [(real, out) for _entry, real, out in chunk],
                calendars,
                staging,
                store_root=store_root,
                verdicts=chunk_verdicts,
            )
        except ots.OtsError as exc:
            # A batch-level failure (e.g. the shared staging dir cannot be created) says nothing
            # about the individual files. Degrade to the per-file fallback below, which classifies
            # each one permanent vs. transient — and, crucially, never abort the later chunks or
            # propagate to the caller (a scan must not fail because stamping could not run).
            log.warning("batch stamp failed for collection %s: %s", collection.id, exc)
            outcomes = [None] * len(chunk)
        for (entry, real, out), outcome, verdict in zip(chunk, outcomes, chunk_verdicts):
            if outcome is None:
                # Isolate the failure: retry just this file on its own before giving up on it.
                try:
                    outcome = await asyncio.to_thread(
                        ots.stamp_via_symlink, real, out, calendars, staging,
                        store_root=store_root, verdict=verdict,
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
                    # …and no stamp time either: a file renamed onto an un-writable path after an
                    # earlier stamp would otherwise keep the OLD content's timestamp with no proof
                    # to back it, which the panel renders as trust metadata ("notarized on …").
                    # `none` must mean nothing is claimed. The monitored `status` is untouched —
                    # this is a notarization skip, not a re-baseline.
                    entry.ots_stamped_at = None
                    # …and no provenance: `ots_digest` records the digest of a proof Cairn placed at
                    # `ots_path`, so with no proof there is nothing for it to be a record of.
                    entry.ots_digest = None
                    skipped += 1
                    continue
                except ots.OtsError as exc:
                    log.warning("stamp failed for %s: %s", real, exc)
                    continue
            if outcome.kind == "deferred":
                # A same-digest anchored proof neither confirmed nor disproven (the backend was
                # unreachable). Both artifacts survive on disk; the row is left EXACTLY as it was —
                # `pending`, no path, no state, no provenance, no stamp time — so an outage produces
                # no recorded claim and the next pass that reaches the backend decides the case.
                deferred += 1
                continue
            entry.ots_path = str(out)
            entry.ots_digest = outcome.digest
            if outcome.kind == "kept":
                # An existing anchored proof the caller CONFIRMED was kept canonical. Record what it
                # is, and leave `ots_stamped_at` alone — no submission happened here (design D1).
                entry.ots_state = "complete"
            else:
                entry.ots_state = outcome.state or "incomplete"
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
    if deferred:
        log.warning(
            "collection %s: deferred %d proof placement(s) — the existing proof's anchor could not "
            "be checked, so both proofs were kept and the files stay pending for the next pass",
            collection.id,
            deferred,
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
    # Read the identity BEFORE the claim: a lost claim rolls back, which expires the ORM object,
    # and an async lazy refresh on the refusal path raises instead of reporting the refusal.
    collection_id = collection.id
    await mark_unstamped_pending(session, collection)
    total = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .where(FileEntry.collection_id == collection_id, FileEntry.ots_state == "pending")
    )
    run = Run(
        collection_id=collection_id,
        kind="stamp",
        started=_utcnow(),
        result="running",
        total=int(total or 0),
    )
    # Atomically claim the collection's single in-progress slot (partial unique index on a `running`
    # run) so a concurrent scan/stamp can't run a second writer over the same collection. A lost claim
    # means an op is already in flight — refuse this backfill rather than starting it.
    if await collections.claim_run(session, run) is None:
        log.info(
            "stamp backfill refused for collection %s — another operation already claimed it",
            collection_id,
        )
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


def _backfill_provenance(entry: FileEntry, facts: ots.StoredProofFacts) -> None:
    """Corroborated ``ots_digest`` backfill for one row already visited by the upgrade pass (D3).

    Fill a NULL ``ots_digest`` from the stored proof **only when the proof's committed digest equals
    the row's recorded ``sha256``**. The recorded baseline is the corroborating witness: a swapped
    proof cannot be laundered into "recorded", because to be written it would have to commit to
    exactly the digest Cairn already has on file for those bytes — and a proof that does that is, by
    definition, not a swap. The column's detection property (recorded vs. parsed disagreeing ⇒ this
    is not the proof Cairn placed) therefore survives intact.

    A parsed digest that does NOT match is exactly the corrupted / swapped / misfiled proof the
    column exists to catch: it is left NULL and logged at WARNING naming both digests and the action.
    Recording it would destroy the finding; skipping it silently would hide it.

    A row that already has provenance is NEVER rewritten — a later disagreement is verify's finding
    to report, not this pass's to overwrite. Neither branch changes whether the proof is upgraded,
    and neither may raise out of the pass.
    """
    if entry.ots_digest is not None or not facts.readable or not facts.digest:
        return
    row_sha = (entry.sha256 or "").strip().lower()
    if not row_sha:
        return
    if facts.digest == row_sha:
        entry.ots_digest = facts.digest
        return
    log.warning(
        "proof provenance mismatch for %s: Cairn recorded sha256=%s for this file, but the stored "
        "proof at %s commits to %s. The proof is corrupted, swapped or misfiled — run `cairn verify "
        "%s` (or the panel's Verify) for the full attribution. Leaving ots_digest unset.",
        entry.relpath,
        row_sha,
        entry.ots_path,
        facts.digest,
        entry.relpath,
    )


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

    This pass is also the ONE place ``ots_digest`` is backfilled (design D3): it already holds every
    row and already opens every one of these proofs, so a corroborated fill costs one local parse of
    a sub-kilobyte file already in the page cache — no calendar traffic, no extra query, and none at
    all once the backlog drains. See :func:`_backfill_provenance` for why it is safe here and
    forbidden on every read path.
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
            if entry.ots_digest is None:
                # Corroborated provenance backfill (design D3). Offline, never raises, and cannot
                # change the upgrade outcome decided above.
                facts = await asyncio.to_thread(ots.read_proof_facts, entry.ots_path)
                _backfill_provenance(entry, facts)
        if complete:
            entry.ots_state = "complete"
            upgraded += 1
        else:
            still += 1
        if progress is not None:
            await progress(processed)

    await session.commit()
    return {"upgraded": upgraded, "still_incomplete": still}


@dataclass
class UpgradePass:
    """What one collection's upgrade attempt did, for a caller that must report or exit on it."""

    collection_id: int
    run: Run | None = None
    upgraded: int = 0
    still_incomplete: int = 0
    refused: bool = False  # the collection's single-operation slot was already held
    idle: bool = False  # nothing incomplete to upgrade; no run recorded


async def upgrade_collection(
    session: AsyncSession, collection: Collection, settings: Settings | None = None
) -> UpgradePass:
    """Upgrade one collection's incomplete proofs under a claimed ``kind='upgrade'`` run (D10).

    Proof mutation is single-writer per collection, enforced by the DB claim
    (:func:`collections.claim_run` + the partial unique index), not by a lock: the claim serializes
    across processes and hosts sharing the datastore, which no in-process lock does. A collection
    whose slot is already held is REFUSED — never waited on: waiting would turn a cron
    ``cairn upgrade`` into an unbounded stall behind a multi-hour deep scan, and the work is
    idempotent, so the next invocation picks it up.

    A collection with nothing incomplete records no run at all (no empty daily rows). ``kind='upgrade'``
    runs never feed scan freshness, so this can never refresh the dead-man's switch.
    """
    settings = settings or get_settings()
    # Identity read up front: a lost claim rolls back and expires the ORM object, and a lazy
    # refresh on the refusal path would raise rather than report the refusal.
    collection_id, collection_name = collection.id, collection.name
    result = UpgradePass(collection_id=collection_id)
    if await collections.active_run(session, collection_id) is not None:
        result.refused = True
        return result
    incomplete = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .where(FileEntry.collection_id == collection_id, FileEntry.ots_state == "incomplete")
    )
    if not incomplete:
        result.idle = True
        return result

    run = Run(
        collection_id=collection_id,
        kind="upgrade",
        started=_utcnow(),
        result="running",
        total=int(incomplete),
    )
    # The `active_run` pre-check above is only advisory; this commit (guarded by the partial unique
    # index) is the race-free claim.
    if await collections.claim_run(session, run) is None:
        result.refused = True
        return result
    result.run = run

    async def _progress(done: int) -> None:
        run.processed = done
        await session.commit()

    try:
        outcome = await upgrade_incomplete(session, collection, settings, progress=_progress)
        result.upgraded = outcome.get("upgraded", 0)
        result.still_incomplete = outcome.get("still_incomplete", 0)
        run.upgraded = result.upgraded
        run.processed = int(incomplete)
        run.result = "ok"
    except Exception:
        log.exception("upgrade failed for collection %s (%s)", collection_id, collection_name)
        run.result = "error"
    run.finished = _utcnow()
    await session.commit()
    return result


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
