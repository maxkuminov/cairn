"""Proof store layout + the stamp/upgrade/verify/export lifecycle (DESIGN.md §5/§6).

Proofs live on the writable volume laid out parallel to the collection:
``<proof_store>/<collection_id>/<relpath>.ots``. Nothing is ever written under a collection root — the
watched mounts are read-only. Stamping goes through a transient symlink in
``<proof_store>/.staging`` (see :func:`src.services.ots.stamp_via_symlink`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, exists, func, literal, or_, select, update
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


# --- The referenced-slot guard: a stamp never displaces another row's proof (design D1, #39) ---
#
# A move reconciliation repoints a row's `relpath` and keeps its `ots_path`, which then names the
# OLD relpath's canonical slot — truthfully, but claimably: a different file appearing at that old
# path and being stamped there used to displace the moved file's proof into `.superseded/`, after
# which the moved row's pointer resolved to a stranger's proof. That reads at verify time as a proof
# mismatch on a perfectly healthy file: a false alarm on the product's core signal.
#
# The fix is not a race won by relocation — it is a refusal. Nothing may be stamped into a slot any
# row currently records as its proof. The stamp waits (the member stays `pending` and is warned);
# the healing sweep converges the blocker; the next pass stamps normally.

# How many candidate paths ride in one guard query. Two IN lists per query, so this keeps the bound
# parameter count far below SQLite's limit no matter how large an operator sets the stamp batch.
_SLOT_QUERY_CHUNK = 400


async def _slot_references(
    session: AsyncSession, collection_id: int, paths: list[Path]
) -> list[tuple[int, str, str]]:
    """Rows in the collection whose recorded ``ots_path`` is one of ``paths`` — or an alias of one.

    One bounded query over the candidate paths, returning ``(id, relpath, ots_path)``. Two legs:

    * an EXACT spelling match, which is authoritative on its own — the recorded pointer *is* that
      slot, whether or not anything is on disk there;
    * a spelling differing only in case, which is merely a CANDIDATE: on a case-insensitive store
      the two names are one directory entry, on a case-sensitive one they are two distinct slots,
      and only the filesystem can say which. :func:`_blocking_reference` confirms these by on-disk
      identity before they defer anything.

    The case leg folds through ``casefold`` — the SQL function registered on every connection in
    :func:`src.database._configure_sqlite`, which is Python's ``str.casefold`` — on BOTH sides, so
    the two keys are computed by one rule. SQLite's built-in ``lower()`` cannot be used here: it
    folds ASCII only, so a recorded ``Å.txt.ots`` and a member's ``å.txt.ots`` produced no
    candidate at all and the guard missed the alias entirely on a case-insensitive store. Full
    Unicode folding only widens CANDIDATE selection; :func:`ots.same_directory_entry` remains the
    decider, so a case-sensitive store still never defers a genuinely distinct slot.
    """
    wanted = sorted({str(p) for p in paths})
    if not wanted:
        return []
    found: list[tuple[int, str, str]] = []
    # Chunked so the bound-parameter count stays well inside SQLite's limit whatever
    # `ots_stamp_batch_size` is set to: two IN lists per chunk, and a batch is otherwise free to be
    # as large as the operator likes.
    for start in range(0, len(wanted), _SLOT_QUERY_CHUNK):
        group = wanted[start : start + _SLOT_QUERY_CHUNK]
        folded = sorted({p.casefold() for p in group})
        rows = await session.execute(
            select(FileEntry.id, FileEntry.relpath, FileEntry.ots_path).where(
                FileEntry.collection_id == collection_id,
                FileEntry.ots_path.is_not(None),
                or_(
                    FileEntry.ots_path.in_(group),
                    func.casefold(FileEntry.ots_path).in_(folded),
                ),
            )
        )
        found.extend((rid, relpath, recorded) for rid, relpath, recorded in rows)
    return found


def _blocking_reference(
    candidates: list[tuple[int, str, str]], out: Path, *, own_id: int | None
) -> tuple[int, str, str] | None:
    """The row (if any) that records ``out`` as its proof — never the member's own row.

    The exclusion is per member, not per batch: a row can be `pending` AND still carry a pointer at
    another member's canonical slot (a file that was moved and then modified), so excluding the
    whole batch up front would let exactly the #39 case through. Only a row's own pointer at its own
    canonical path is uninteresting, and that is what ``own_id`` skips.
    """
    out_s = str(out)
    # The same full-Unicode fold the candidate query used (`str.casefold`, registered as the
    # `casefold` SQL function): the two keys must be computed by ONE rule, or a candidate the SQL
    # surfaced could be silently dropped here.
    out_folded = out_s.casefold()
    for rid, relpath, recorded in candidates:
        if rid == own_id:
            continue
        if recorded == out_s:
            return rid, relpath, recorded
        if recorded.casefold() == out_folded and ots.same_directory_entry(recorded, out_s):
            return rid, relpath, recorded
    return None


async def _defer_referenced_slots(
    session: AsyncSession,
    collection_id: int,
    chunk: list[tuple[FileEntry, Path, Path]],
) -> tuple[list[tuple[FileEntry, Path, Path]], int]:
    """Drop from ``chunk`` every member whose output slot is another row's proof; return the rest.

    **This is the first canonical-slot decision made about any member**, and the position is
    load-bearing (design D1). It runs under the collection's proof-store lock, after the operation's
    claim has been re-confirmed, and before the adoption pass, before the output-writability
    classification, before any staging symlink exists and before any calendar submission. A member
    dropped here therefore never produces a proof at all, so the batch teardown has nothing of its
    to dispose of — and, critically, it is never offered to :func:`_adopt_or_verdict`, which for a
    byte-identical newcomer would happily adopt the BLOCKING row's proof and put two rows on one
    artifact.

    A deferral is never a failure: the member stays `pending` with its row untouched, the rest of
    the batch stamps normally, and the next pass retries it — succeeding once the healing sweep has
    relocated the blocker's proof to its own current relpath's canonical location.
    """
    candidates = await _slot_references(session, collection_id, [out for _e, _r, out in chunk])
    if not candidates:
        return chunk, 0
    keep: list[tuple[FileEntry, Path, Path]] = []
    blocked = 0
    for entry, real, out in chunk:
        blocker = _blocking_reference(candidates, out, own_id=entry.id)
        if blocker is None:
            keep.append((entry, real, out))
            continue
        blocked += 1
        blocker_id, blocker_relpath, blocker_path = blocker
        log.warning(
            "collection %s: deferring the stamp of %s — its proof would go to %s, which file row "
            "%s (%s) currently records as its own proof. Nothing was placed and the file stays "
            "queued; `cairn upgrade` relocates that proof to its file's current path, after which "
            "the next stamp pass proceeds",
            collection_id,
            entry.relpath,
            blocker_path,
            blocker_id,
            blocker_relpath,
        )
    return keep, blocked


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


async def _adoption_pass(
    chunk: list[tuple[FileEntry, Path, Path]], settings: Settings
) -> tuple[list[tuple[FileEntry, Path, Path]], list[str | None], int]:
    """Offer each member's existing canonical proof to :func:`_adopt_or_verdict` (design D1a).

    Returns ``(members still to stamp, their verdicts, adopted count)``. Extracted from
    :func:`stamp_pending` unchanged so it can run per BATCH, inside the proof-store lock and after
    the referenced-slot guard — the guard has to be the first canonical-slot decision, and adoption
    is a canonical-slot decision.
    """
    keep: list[tuple[FileEntry, Path, Path]] = []
    verdicts: list[str | None] = []
    adopted = 0
    for entry, real, out in chunk:
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
    return keep, verdicts, adopted


async def _stamp_one_batch(
    chunk: list[tuple[FileEntry, Path, Path]],
    chunk_verdicts: list[str | None],
    *,
    collection_id: int,
    calendars: list[str],
    staging: Path,
    store_root: Path,
    now: datetime,
    stamped: int,
    skipped: int,
    deferred: int,
) -> tuple[int, int, int]:
    """Place one batch of proofs and record the rows; return updated (stamped, skipped, deferred).

    Extracted from :func:`stamp_pending` unchanged, so the whole inspect → preserve → place →
    record sequence for a batch is one call that can be wrapped in the collection's proof-store lock
    (design D10). The row updates stay in the caller's session and are committed by its ``progress``
    callback, exactly as before.
    """
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
        log.warning("batch stamp failed for collection %s: %s", collection_id, exc)
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
    return stamped, skipped, deferred


async def stamp_pending(
    session: AsyncSession,
    collection: Collection,
    settings: Settings | None = None,
    *,
    progress: ProgressCb | None = None,
    run_id: int | None = None,
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

    Each batch's first canonical-slot decision is the **referenced-slot guard**
    (:func:`_defer_referenced_slots`, design D1): a member whose canonical output path is currently
    recorded as ``ots_path`` by a DIFFERENT row leaves the batch before anything else looks at that
    path. It stays ``pending``, is warned with the blocking row named, never fails the batch, and
    stamps normally on a later pass once the healing sweep has relocated the blocker's proof. This
    is what makes it impossible for a new file at a moved file's former path to displace the moved
    file's proof (GitHub #39) — at every entry point, and in the same scan that reconciled the move.

    Then, still before any staging symlink or calendar traffic, a pending file whose canonical path
    already holds a proof is offered to :func:`_adopt_or_verdict`: a proof that parses, commits to
    the row's own ``sha256`` AND whose anchor verifies right now is ADOPTED (recorded, no
    submission), and the backend's answer about any other existing proof is carried into placement
    as a verdict so a forged attestation cannot hold the canonical path (design D1/D1a). Whatever is
    on disk is preserved either way — :func:`ots._place_proof` never destroys a proof.

    The caller must hold the collection's single-operation claim (design D10). Inspect -> preserve ->
    place -> record is check-then-act, so two writers would be a lost-update machine; the claim is
    taken by the caller (a scan's own ``running`` run, ``run_stamp_backfill``, or the CLI) rather
    than here, because this function is called from inside a scan that already holds one.

    ``run_id`` names that claim, and every production caller passes it. It turns the claim from an
    assumption into a **fence**: before each batch is placed, the run is re-read from the datastore
    and, if it is no longer ``running`` (its lease aged out and something reclaimed it), the pass
    raises :class:`collections.LeaseLost` instead of writing into a proof store another operation
    now owns. Batches already placed STAND — they were placed while the lease was valid, their rows
    were committed by the per-batch ``progress`` write under that same lease, and unwinding proofs
    that exist on disk would destroy evidence to tidy up bookkeeping. The fence is checked between
    batches (a batch is one ``ots`` call and one calendar round-trip, so the check is free by
    comparison) and nothing is committed after it fires.

    A fence read alone is still check-then-act, though: a reclamation landing in the gap between
    "the lease is held" and the batch's ``os.replace`` puts two placers on one canonical path — and
    worse, a reclamation landing between the GUARD and the placement lets a replacement scan's move
    reconciliation newly reference a slot this batch is about to write, which is exactly the loss
    the guard exists to prevent (the calendar round-trip is minutes long, and a keepalive can fail
    on a perfectly live process). **So the window is closed by lock discipline, not by a
    point-in-time read** (design D1): this pass takes the collection's proof-store lock
    (:class:`ots.CollectionProofLock`, ``<proof_store>/<collection_id>/.lock``) ONCE and holds it
    continuously across its whole critical section — the guard, the adoption pass, staging,
    calendar submission, placement, and every post-guard state commit — releasing it only when the
    pass ends. :func:`collections.reclaim_stale_claim` (and the scheduler's reaper) probe that same
    lock non-blocking BEFORE reclaiming, and refuse while it is held: a held lock means the claim's
    holder is alive inside a proof critical section. A move reconciliation that would newly
    reference one of this pass's output slots can only commit under a replacement claim, and the
    probe rule makes that claim unobtainable while this pass runs.

    The lease fences remain, and every post-guard state commit is fenced — the adoption pass's own
    commit included, even when no placement chunk survives it. They are the guard for the two cases
    lock discipline cannot cover: a holder that CRASHED (the operating system releases its
    ``flock``, so its claim is reclaimed normally) and a proof store whose filesystem cannot lock at
    all (:class:`ots.CollectionProofLock` degrades there, and reclamation degrades with it). No
    re-query of the slots themselves substitutes for either mechanism.

    Holding the lock for the pass rather than per batch is deliberate: the guard's answer must stay
    true until the proof it authorises is on disk, and a per-batch hold reopens the window at every
    batch boundary. The cost is that a second placer waits out the pass instead of one round-trip —
    but the only legitimate second placer is an operation holding this collection's claim, which
    this pass holds.
    """
    settings = settings or get_settings()
    # Read the identity up front, as a plain local: the fence below rolls the session back, which
    # expires every ORM object in it — an attribute read after that would raise `MissingGreenlet`
    # from async code, and a message about a reclamation must never be the thing that crashes.
    collection_id = collection.id
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

    batch_size = max(1, settings.ots_stamp_batch_size)
    now = _utcnow()
    stamped = 0
    skipped = 0  # files whose proof path can never be written (e.g. name past the FS byte limit)
    deferred = 0  # same-digest anchored proofs nothing could confirm; nothing recorded, still pending
    blocked = 0  # members whose output slot is another row's recorded proof (design D1)
    batches = (len(work) + batch_size - 1) // batch_size

    async def _fence(batch_no: int, stage: str) -> None:
        """Stop the pass unless ``run_id`` still holds the collection's claim (design D10/D1).

        Called at three points: before the proof-store lock is taken (cheap, so a reclaimed pass
        does not queue for a resource it may not touch), immediately after it is taken (the
        reclamation may have landed while this pass waited), and once per batch **before that
        batch's state commit** — placement and adoption-only alike, since an adoption commit
        records a proof under a claim exactly as a placement does.
        """
        if run_id is None or await collections.lease_held(run_id):
            return
        log.warning(
            "collection %s: the operation claim (run %s) was RECLAIMED mid-stamp — stopping %s "
            "batch %d of %d. The %d proof(s) already placed stand; the rest stay pending",
            collection_id,
            run_id,
            stage,
            batch_no,
            batches,
            stamped,
        )
        # Discard anything this call has staged but not yet committed (the adoption pass's row
        # updates, if the fence fires before the first `progress` write): nothing commits after
        # the fence sees a reclamation. Proofs already ON DISK are untouched — they were placed
        # under a valid lease, `_place_proof` never destroys a proof, and the next pass re-reads
        # and adopts or re-records them.
        await session.rollback()
        raise collections.LeaseLost(
            f"the operation claim for collection {collection_id} (run {run_id}) was reclaimed "
            f"mid-stamp"
        )

    if not work:
        return 0

    # The fence, before this pass queues for a resource it may not be allowed to touch (design
    # D10). A caller that named its claim gets it checked; one that did not (a direct test call) is
    # unchanged.
    await _fence(1, "before taking the proof-store lock for")
    # …and then the lock, ONCE, for the whole critical section (design D1). Everything from the
    # first guard decision to the last state commit happens under it, so no reclamation can land
    # between the guard and the placement it authorised: reclamation probes this lock first and
    # refuses while it is held. `flock` lives on the open file description, so holding it across
    # `await`s (including the `asyncio.to_thread`-ed `ots` subprocess calls) is exactly right — the
    # hold belongs to the process, not to the thread that took it.
    lock = ots.CollectionProofLock(store_root, collection_id)
    try:
        await asyncio.to_thread(lock.acquire)
    except ots.OtsError as exc:
        # Someone else holds the lock (or it cannot be taken at all). Transient by construction:
        # nothing was placed, nothing was dropped to `none`, and every file stays `pending` for the
        # next pass.
        log.warning(
            "collection %s: the stamp pass placed nothing (%d file(s) still queued) — %s",
            collection_id,
            len(work),
            exc,
        )
        return 0
    try:
        # The claim, re-confirmed AFTER the lock: the reclamation may have landed while this pass
        # waited for it, and every decision below is made on the strength of this read.
        await _fence(1, "after taking the proof-store lock for")
        for start in range(0, len(work), batch_size):
            chunk = work[start : start + batch_size]
            batch_no = start // batch_size + 1
            # THE FIRST canonical-slot decision, under the lock and after the claim was
            # re-confirmed: nothing may be stamped into a slot another row records as its proof
            # (design D1). Deferred members leave the batch here — before adoption, before staging,
            # before the calendar sees them.
            chunk, blocked_now = await _defer_referenced_slots(session, collection_id, chunk)
            blocked += blocked_now
            # Adoption pass (design D1a): drop from the batch every pending file whose existing
            # canonical proof Cairn may stand behind, and remember the backend's verdict about the
            # ones it may not. It runs INSIDE the lock, after the guard, so it can never be offered
            # a slot the guard has already ruled out.
            chunk, chunk_verdicts, adopted_now = await _adoption_pass(chunk, settings)
            stamped += adopted_now
            # The state-commit fence, before ANY of this batch's state reaches the datastore —
            # whether that state comes from a placement or from adoption alone. An adoption-only
            # batch records a proof on a row exactly as a placement does, so skipping the fence
            # when no placement chunk survives would let a reclaimed claim commit it. The lock
            # above already excludes a live rival; this is the guard for a crashed holder (whose
            # `flock` the OS released) and for a store whose filesystem cannot lock.
            await _fence(batch_no, "at the state-commit fence of")
            if chunk:
                stamped, skipped, deferred = await _stamp_one_batch(
                    chunk,
                    chunk_verdicts,
                    collection_id=collection_id,
                    calendars=calendars,
                    staging=staging,
                    store_root=store_root,
                    now=now,
                    stamped=stamped,
                    skipped=skipped,
                    deferred=deferred,
                )
            if progress is not None:
                # Persist progress per batch (the callback commits) so the badge advances live.
                # Inside the lock: it is a post-guard state commit like any other.
                await progress(stamped)

        if skipped:
            log.warning(
                "collection %s: skipped %d file(s) with an unwritable proof path (set "
                "ots_state=none)",
                collection_id,
                skipped,
            )
        if deferred:
            log.warning(
                "collection %s: deferred %d proof placement(s) — the existing proof's anchor could "
                "not be checked, so both proofs were kept and the files stay pending for the next "
                "pass",
                collection_id,
                deferred,
            )
        if blocked:
            log.warning(
                "collection %s: deferred %d stamp(s) whose proof slot is still recorded by another "
                "file row (design D1) — those files stay queued and nothing was displaced; run "
                "`cairn upgrade` to relocate the blocking proofs to their files' current paths",
                collection_id,
                blocked,
            )
        await session.commit()
    finally:
        # Releasing is a non-blocking syscall on a descriptor the process owns, so it needs no
        # worker thread — and the lock lives on the file description, not on the thread that took
        # it, so releasing here is the same lock the worker thread acquired. In a `finally` so a
        # `LeaseLost` (or any other exception) can never leave the collection's proof lock held.
        lock.release()
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

    Claims the collection's single-operation slot with a ``running`` stamp run, THEN queues the
    `none`-state baseline (:func:`mark_unstamped_pending`) and records ``total`` — the number of
    files now pending, i.e. the work it will do, so the badge is exact — then stamps them via the
    batched :func:`stamp_pending`, advancing ``processed`` per batch. A stamp failure can never
    propagate: it is recorded as ``result='error'`` on the run. Returns the finalized run (a refused
    one comes back still ``running``, the one state a finished backfill can never be in).
    ``kind='stamp'`` runs never count toward scan freshness.

    Its guard-through-placement critical section is :func:`stamp_pending`'s, and the proof-store
    lock that closes the guard-to-placement window is held there, continuously, across the whole
    stamping body (design D1). Everything this function does outside that call — claiming the slot,
    queueing the baseline, recording ``total``, finalizing the run — happens BEFORE the first
    canonical-slot decision or AFTER the last one and mutates no proof, so it needs no hold of its
    own (and must not take one: the lock is not reentrant within a process).
    """
    settings = settings or get_settings()
    # Read the identity BEFORE the claim: a lost claim rolls back, which expires the ORM object,
    # and an async lazy refresh on the refusal path raises instead of reporting the refusal.
    collection_id = collection.id
    run = Run(
        collection_id=collection_id,
        kind="stamp",
        started=_utcnow(),
        result="running",
    )
    # Atomically claim the collection's single in-progress slot (partial unique index on a `running`
    # run) so a concurrent scan/stamp can't run a second writer over the same collection. A lost claim
    # means an op is already in flight — refuse this backfill rather than starting it.
    #
    # The claim comes FIRST, before `mark_unstamped_pending`, and that order is load-bearing:
    # queueing the baseline is itself a committed write to the collection's files, so doing it up
    # front meant a REFUSED backfill still flipped every `ots_state='none'` row to `pending` — a
    # mutation made outside the claim, which the operation actually holding the slot would then pick
    # up in its own stamp pass, while the caller was told "nothing was stamped" (design D10; a
    # refusal must change nothing).
    if await collections.claim_run(session, run) is None:
        log.info(
            "stamp backfill refused for collection %s — another operation already claimed it",
            collection_id,
        )
        return run

    # Inside the claim now: queue the `none`-state baseline and take the exact denominator for the
    # badge. `total` is briefly NULL (indeterminate progress) between the claim and this commit.
    await mark_unstamped_pending(session, collection)
    run.total = int(
        await session.scalar(
            select(func.count())
            .select_from(FileEntry)
            .where(FileEntry.collection_id == collection_id, FileEntry.ots_state == "pending")
        )
        or 0
    )
    await session.commit()

    run_id = run.id

    async def _progress(done: int) -> None:
        run.processed = done
        # Progress is also the claim's liveness signal: a long backfill must not look abandoned to
        # the startup reaper, which would revoke a claim this process still holds (design D10). The
        # keepalive below covers the gap BETWEEN two of these (a batch that stalls on a slow
        # calendar can outlast the abandonment interval on its own).
        run.heartbeat_at = _utcnow()
        await session.commit()

    result = "ok"
    stamped = 0
    # Keepalive around the whole stamping body: liveness must track the process, not the completion
    # of a batch. The fence lives inside `stamp_pending`, per batch.
    async with collections.run_keepalive(run_id):
        try:
            stamped = await stamp_pending(
                session, collection, settings, progress=_progress, run_id=run_id
            )
        except collections.LeaseLost:
            # The claim was reclaimed mid-backfill. Proofs already placed stand and their rows were
            # committed under the valid lease; nothing further is written here, and the terminal
            # write below is a no-op because the guarded UPDATE will not match a reclaimed run.
            log.warning(
                "stamp backfill for collection %s was RECLAIMED mid-run (run %s) — stopping; "
                "the run row keeps the state the reclamation wrote",
                collection_id,
                run_id,
            )
            result = "interrupted"
        except Exception:  # pragma: no cover - stamping must never fail the operation
            log.exception("stamp backfill failed for collection %s", collection_id)
            stamped = 0
            result = "error"

    # Terminal state only while this run is still the live claim (design D10) — a reclaimed run
    # keeps the `interrupted` state the reclamation wrote rather than being relabelled by the loser.
    finalized = await collections.finalize_if_held(
        session,
        run_id,
        result=result,
        stamped=stamped,
        processed=stamped,
        finished=_utcnow(),
    )
    if not finalized:
        log.warning(
            "stamp backfill run %s (collection %s) was RECLAIMED before it could finalize — "
            "leaving the terminal state the reclamation wrote",
            run_id,
            collection_id,
        )
    # The row was written by a Core UPDATE, so the ORM object still holds the pre-finalize values
    # and callers read `result`/`stamped` off it. One SELECT re-syncs it — and after a reclamation
    # it correctly reports `interrupted`, which is what actually happened.
    await session.refresh(run)
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


# --- The healing sweep: a proof's location follows its file (design D2-D5/D4b, #39) ------------
#
# The ONLY code path in Cairn that relocates a stored proof. It runs inside the daily upgrade pass
# and `cairn upgrade`, under the collection's single-operation claim, and it converges the pointers
# the stamp guard above protects: a move-reconciled row whose `ots_path` still names its FORMER
# relpath's canonical slot is brought to its current relpath's canonical slot.
#
# Three properties govern every branch:
#
#   * **corroborate before believing** — the proof at the recorded path must commit to what the row
#     records (`ots_digest`, or `sha256` for a row predating provenance). An already-misfiled
#     pointer is DETECTED and warned about, never laundered into a new location;
#   * **the pointer invariant** — at every moment, across a crash at any phase boundary, `ots_path`
#     names a location actually holding this row's proof. Hence: publish durably, THEN commit the
#     pointer with a fenced compare-and-set, THEN remove the source loss-proof;
#   * **never destroy** — a destination another row records defers, a byte-identical occupant is
#     adopted, anything else is archived. No input reaches a branch that discards proof bytes.


@dataclass(frozen=True)
class PointerWork:
    """The rows one sweep will operate on, surveyed before the run is claimed.

    ``stale`` — the recorded ``ots_path`` is not the canonical location for the row's current
    relpath. ``absent`` — the recorded ``ots_path`` names nothing on disk (design D4b).

    The two are classified INDEPENDENTLY and may overlap, because they name OPERATIONS, not rows. A
    row that is both — the shape a moved file whose old proof entry went missing leaves — receives
    two operations in one sweep: the restore that puts the proof back at the recorded path, then
    the relocation that carries it to the canonical one. ``items`` counts one per operation, which
    is exactly what the run's ``total`` means, and ``processed`` advances once per completed
    operation, so the two still meet.

    Classifying them disjointly ("absent only") was the bug: such a row was restored to its
    OBSOLETE location, the run ended ``ok``, and the pointer stayed non-canonical — still blocking
    a newcomer's stamp at that slot — until some later pass happened to pick it up.
    """

    stale: tuple[int, ...] = ()
    absent: tuple[int, ...] = ()

    @property
    def items(self) -> int:
        """Work items — one per operation the sweep will perform (the run's share of ``total``)."""
        return len(self.stale) + len(self.absent)


@dataclass
class SweepOutcome:
    """What one sweep did. ``items`` is the number of work items completed, one per admitted row."""

    items: int = 0
    relocated: int = 0  # proof brought to its file's current canonical location
    parked: int = 0  # cycle broken by relocating a proof to the holding location
    deferred: int = 0  # destination is another row's proof; retried on a later sweep
    refused: int = 0  # corroboration or the filesystem refused; nothing changed, warned
    restored: int = 0  # an absent recorded entry republished from the superseded archive


def _absent_recorded_entries(rows: list[tuple[int, str]]) -> set[int]:
    """Ids among ``(id, ots_path)`` whose recorded proof entry does not exist (one lstat each).

    ``os.path.lexists`` rather than ``exists``: a dangling symlink IS an entry, and removing or
    republishing over one is a decision for the destination rules, not for admission.
    """
    return {rid for rid, recorded in rows if not os.path.lexists(recorded)}


async def survey_pointer_work(
    session: AsyncSession, collection_id: int, settings: Settings | None = None
) -> PointerWork:
    """Survey the sweep's work for one collection: stale pointers + absent recorded entries.

    The existence of either shape is an independent admission reason: a collection with no
    incomplete proofs — a tripwire collection carrying historical proofs included — still claims,
    sweeps, and counts this work in its run's ``total``. It is called twice by
    :func:`upgrade_collection`: once as a cheap advisory pre-check (so a collection with nothing to
    do never claims a slot or writes a run row), and again UNDER the claim, where its answer is the
    authoritative work set and the run's ``total``. Only the second answer may be believed: between
    the two, a rival pass holding the slot can finish the very work this one surveyed.

    Staleness is decided by :func:`proof_path` and nothing else (design D6). SQL computes the same
    comparison as a pre-filter, but only the helper's verdict admits a row, so the two can never
    disagree about what gets relocated. The SQL form is a concatenation, never a ``LIKE``: a relpath
    containing ``%``, ``_`` or a quote is compared literally, exactly as the helper compares it.
    Every proof-bearing row is read anyway for the restore leg's one ``lstat``, which is trivial
    beside the pass's per-proof subprocess work.
    """
    settings = settings or get_settings()
    # `proof_path` builds `<store>/<collection_id>/<relpath>.ots`; this is the same expression in
    # SQL, so the column comparison below is the helper's comparison, spelled for the datastore.
    prefix = str(Path(settings.proof_store_path) / str(collection_id)) + os.sep
    canonical_sql = literal(prefix) + FileEntry.relpath + literal(".ots")
    rows = list(
        await session.execute(
            select(
                FileEntry.id,
                FileEntry.relpath,
                FileEntry.ots_path,
                (FileEntry.ots_path != canonical_sql).label("sql_stale"),
            ).where(
                FileEntry.collection_id == collection_id,
                FileEntry.ots_path.is_not(None),
            )
        )
    )
    absent = await asyncio.to_thread(
        _absent_recorded_entries, [(rid, recorded) for rid, _rel, recorded, _flag in rows]
    )
    # Independent of `absent`: a row can be both, and then it is two operations, not one (the
    # restore first, the relocation after it — see :class:`PointerWork`).
    stale = [
        rid
        for rid, relpath, recorded, sql_stale in rows
        if sql_stale and Path(recorded) != proof_path(settings, collection_id, relpath)
    ]
    return PointerWork(stale=tuple(stale), absent=tuple(sorted(absent)))


def _corroborate_source(entry: FileEntry, facts: ots.StoredProofFacts) -> str:
    """Does the proof at ``entry.ots_path`` provably belong to this row? (design D3)

    Returns ``'ok'`` or the reason it does not. The distinction between the two failure reasons is
    the whole point, because they license completely different sentences to the operator:

    * ``'provenance_mismatch'`` — the row RECORDS which digest the proof Cairn placed there commits
      to, and the proof on disk commits to something else. That is a detected misfiled pointer, and
      may be reported as one.
    * ``'legacy_ambiguous'`` — the row predates provenance (``ots_digest`` NULL), so the only
      witness is its last-scanned ``sha256``. Disagreement is NOT evidence of misfiling: a file
      legitimately modified after it was stamped, and then moved, has exactly this shape. It may
      only ever be reported as un-corroborated.

    Either way the sweep touches nothing — no relocation, no archiving, no row change. Believing an
    uncorroborated source would let the sweep carry a stranger's proof into the moved file's
    canonical slot, which is the one thing worse than leaving the pointer where it is.
    """
    if not facts.readable or not facts.digest:
        return "unreadable"
    recorded = (entry.ots_digest or "").strip().lower()
    if recorded:
        return "ok" if facts.digest == recorded else "provenance_mismatch"
    baseline = (entry.sha256 or "").strip().lower()
    if not baseline:
        return "no_baseline"
    return "ok" if facts.digest == baseline else "legacy_ambiguous"


@dataclass
class _SweepContext:
    """Everything one row's relocation needs, so the per-row helpers stay readable."""

    session: AsyncSession
    collection_id: int
    settings: Settings
    store_root: Path
    run_id: int | None


async def _sweep_fence(ctx: _SweepContext, stage: str) -> None:
    """Stop the sweep unless the operation's claim is still held (design D2/D10)."""
    if ctx.run_id is None or await collections.lease_held(ctx.run_id):
        return
    log.warning(
        "collection %s: the operation claim (run %s) was RECLAIMED mid-sweep — stopping %s; "
        "every pointer already committed is truthful and the rest are re-evaluated next sweep",
        ctx.collection_id,
        ctx.run_id,
        stage,
    )
    await ctx.session.rollback()  # nothing commits after the fence fires
    raise collections.LeaseLost(
        f"the operation claim for collection {ctx.collection_id} (run {ctx.run_id}) was reclaimed "
        f"mid-sweep"
    )


@asynccontextmanager
async def _sweep_lock(ctx: _SweepContext, stage: str) -> AsyncIterator[None]:
    """Hold the collection's proof-store lock across ALL phases of one relocation (design D2).

    The claim is re-confirmed AFTER the lock is taken, not before: a reclamation landing while this
    sweep waited for the lock would otherwise put it and the replacement claimant on one canonical
    path at once. Same discipline as the stamp and upgrade passes, at the same resource.
    """
    lock = ots.CollectionProofLock(ctx.store_root, ctx.collection_id)
    await asyncio.to_thread(lock.acquire)  # OtsError when someone else holds it
    try:
        await _sweep_fence(ctx, stage)
        yield
    finally:
        lock.release()


async def _commit_pointer(ctx: _SweepContext, entry: FileEntry, new_path: str) -> None:
    """Move ``entry.ots_path`` to ``new_path`` with a fenced compare-and-set (design D4 phase 4).

    One guarded UPDATE requiring every value the sweep corroborated to still be there — the row's
    ``relpath``, its current ``ots_path``, and (where recorded) its ``ots_digest`` — with the
    operation's run still ``running``. A single post-lock lease check does not cover a whole
    relocation: a keepalive can fail mid-copy on a slow store, so the commit re-establishes the
    claim at the instant it writes.

    Zero rows means the row changed beneath the sweep or the claim was reclaimed. Nothing may be
    committed on either reading: roll back, treat the claim as lost, stop. The proof published at
    the destination is inert — no row names it — and a later sweep re-evaluates from scratch.

    ``ots_path`` is the only column written, here or anywhere in the sweep. Note the NULL handling:
    ``ots_digest = NULL`` is never true in SQL, so a legacy row is fenced with ``IS NULL``.
    """
    # Read the identity up front as plain locals: the rollback below expires every ORM object in
    # the session, and an attribute read after that raises `MissingGreenlet` from async code — a
    # message about a lost claim must never be the thing that crashes.
    entry_id, relpath = entry.id, entry.relpath
    conditions = [
        FileEntry.id == entry_id,
        FileEntry.relpath == relpath,
        FileEntry.ots_path == entry.ots_path,
    ]
    if entry.ots_digest is None:
        conditions.append(FileEntry.ots_digest.is_(None))
    else:
        conditions.append(FileEntry.ots_digest == entry.ots_digest)
    if ctx.run_id is not None:
        conditions.append(exists().where(Run.id == ctx.run_id, Run.result == "running"))
    result = await ctx.session.execute(
        # `synchronize_session=False`: the guard values above are read off the ORM object, and
        # letting the UPDATE write back into the identity map would make a later statement's guard
        # read a value this statement invented. The explicit refresh below is the one place the
        # object learns its new pointer.
        update(FileEntry)
        .where(*conditions)
        .values(ots_path=new_path)
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        await ctx.session.rollback()
        raise collections.LeaseLost(
            f"collection {ctx.collection_id}: the pointer commit for file row {entry_id} "
            f"({relpath}) matched no row — the row changed beneath the sweep or the operation "
            f"claim (run {ctx.run_id}) was reclaimed; nothing was committed"
        )
    await ctx.session.commit()
    # The sessionmaker keeps objects alive across a commit, and a Core UPDATE does not touch the
    # identity map — so without this refresh the upgrade pass that runs next in the SAME session
    # would upgrade the proof at the row's OLD path.
    await ctx.session.refresh(entry, ["ots_path"])


async def _sweep_relocate(
    ctx: _SweepContext, entry_id: int, *, destination: Path | None = None
) -> str:
    """Converge one row's proof onto ``destination`` (default: its current canonical location).

    Returns the outcome kind: ``'relocated'``, ``'parked'`` (a cycle-breaking move to the holding
    location), ``'deferred'``, ``'refused'`` or ``'skipped'``. Only ``LeaseLost`` escapes; every
    filesystem and precondition failure is a per-row warning that leaves the row exactly as it was,
    pointer still truthful, and re-warns on the next sweep.
    """
    session = ctx.session
    entry = await session.get(FileEntry, entry_id)
    if entry is None or not entry.ots_path:
        return "skipped"
    src = Path(entry.ots_path)
    parking = destination is not None
    dst = destination or proof_path(ctx.settings, ctx.collection_id, entry.relpath)
    if not parking and src == dst:
        return "skipped"  # already converged (a concurrent pass, or a survey that raced a rescan)

    facts = await asyncio.to_thread(ots.read_proof_facts, src)
    reason = _corroborate_source(entry, facts)
    if reason != "ok":
        _warn_uncorroborated(ctx, entry, facts, reason)
        return "refused"

    # Rule (a) of the destination rules, read from the datastore because only the datastore knows
    # what a row records. The answer is captured as a predicate over a snapshot rather than a live
    # query: the relocation runs under this collection's operation claim AND its proof-store lock,
    # so no other Cairn writer can create a pointer at the destination while it is in flight, and
    # the predicate stays correct for the publication's EEXIST re-classification.
    candidates = await _slot_references(session, ctx.collection_id, [dst])
    blocker = _blocking_reference(candidates, dst, own_id=entry.id)
    blocked = {str(dst)} if blocker is not None else set()

    try:
        publication = await asyncio.to_thread(
            ots.publish_relocation,
            src,
            dst,
            store_root=ctx.store_root,
            collection_id=ctx.collection_id,
            referenced=lambda path: str(path) in blocked,
        )
    except ots.OtsPathError as exc:
        # A destination the filesystem refuses permanently. Deliberately NOT the stamp path's
        # drop-to-`none`: that would discard a placed proof's state, provenance and pointer to tidy
        # up a location problem. The proof stays where it is and the operator keeps hearing about it.
        log.warning(
            "collection %s: cannot relocate the proof for %s to %s — the filesystem refuses that "
            "name permanently (%s). The proof stays at %s with its state and provenance intact",
            ctx.collection_id,
            entry.relpath,
            dst,
            exc,
            src,
        )
        return "refused"
    except ots.OtsError as exc:
        log.warning(
            "collection %s: could not relocate the proof for %s from %s to %s (%s) — nothing was "
            "changed; the recorded pointer still names the proof and the next sweep retries",
            ctx.collection_id,
            entry.relpath,
            src,
            dst,
            exc,
        )
        return "refused"

    if publication.kind == "deferred":
        log.warning(
            "collection %s: deferring the relocation of %s's proof — %s is recorded as the proof "
            "of file row %s (%s), and no branch may place over, unlink or re-point another row's "
            "proof. The old pointer stays truthful and this is retried once that row's proof has "
            "moved on (later in this sweep, or on the next one)",
            ctx.collection_id,
            entry.relpath,
            dst,
            blocker[0] if blocker else "?",
            blocker[1] if blocker else "?",
        )
        return "deferred"

    # The proof is durably at the destination and still at the source. This is the only moment the
    # pointer may move, and it moves under a fence.
    await _commit_pointer(ctx, entry, str(dst))

    if publication.kind == "aliased":
        log.info(
            "collection %s: %s's proof was already in place at %s (the store treats it and %s as "
            "one entry); recorded the canonical spelling and removed nothing",
            ctx.collection_id,
            entry.relpath,
            dst,
            src,
        )
        return "parked" if parking else "relocated"

    try:
        await asyncio.to_thread(
            ots.finish_relocation,
            src,
            dst,
            store_root=ctx.store_root,
            collection_id=ctx.collection_id,
        )
    except ots.OtsError as exc:
        # Post-commit. The committed pointer is NOT rolled back to a location the proof may be
        # about to leave. What the operator needs to hear depends on what is actually at the
        # destination now, so it is read rather than assumed: telling them "nothing was lost, the
        # pointer is correct" when the pointer resolves to nothing is the false reassurance this
        # product exists to not give.
        if await asyncio.to_thread(os.path.lexists, str(dst)):
            log.warning(
                "collection %s: %s's proof is now recorded at %s, but its old copy at %s could not "
                "be cleared away (%s). Nothing was lost — the pointer is correct and the leftover "
                "copy is preserved rather than overwritten",
                ctx.collection_id,
                entry.relpath,
                dst,
                src,
                exc,
            )
        else:
            log.warning(
                "collection %s: %s's proof is now recorded at %s, but that entry is ABSENT — the "
                "loss-proof removal took it and the immediate restoration could not run (%s). "
                "Nothing was discarded: the corroborated copy is in the store's .superseded/%s/ "
                "archive, and the next sweep admits this row through its restore leg and "
                "republishes the proof at that path",
                ctx.collection_id,
                entry.relpath,
                dst,
                exc,
                ctx.collection_id,
            )
        return "parked" if parking else "relocated"

    log.info(
        "collection %s: relocated %s's proof %s -> %s",
        ctx.collection_id,
        entry.relpath,
        src,
        dst,
    )
    return "parked" if parking else "relocated"


def _warn_uncorroborated(
    ctx: _SweepContext, entry: FileEntry, facts: ots.StoredProofFacts, reason: str
) -> None:
    """Say exactly what the evidence supports about an uncorroborated source, and no more (D3)."""
    if reason == "provenance_mismatch":
        log.warning(
            "collection %s: NOT relocating %s's proof — Cairn recorded placing a proof committing "
            "to %s at %s, but the proof there commits to %s. That pointer is misfiled: some other "
            "stamp took the location over. Nothing was moved, archived or changed. The proof Cairn "
            "placed, if it was displaced, is preserved under the store's .superseded/%s/ archive",
            ctx.collection_id,
            entry.relpath,
            entry.ots_digest,
            entry.ots_path,
            facts.digest,
            ctx.collection_id,
        )
        return
    if reason == "legacy_ambiguous":
        log.warning(
            "collection %s: NOT relocating %s's proof — this row predates recorded proof "
            "provenance, and the proof at %s commits to %s while the file's last scanned digest is "
            "%s. That cannot be corroborated without historical provenance (a file modified after "
            "it was stamped, and then moved, looks exactly like this), so the proof is left where "
            "it is and nothing is claimed about it",
            ctx.collection_id,
            entry.relpath,
            entry.ots_path,
            facts.digest,
            entry.sha256,
        )
        return
    log.warning(
        "collection %s: NOT relocating %s's proof — %s, so nothing about the proof at %s is "
        "established and every byte is left exactly where it is",
        ctx.collection_id,
        entry.relpath,
        (
            "the row records neither proof provenance nor a scanned digest to corroborate against"
            if reason == "no_baseline"
            else "the proof could not be read, or carries no file digest"
        ),
        entry.ots_path,
    )


async def _sweep_restore(ctx: _SweepContext, entry_id: int) -> str:
    """Republish a row's proof at its recorded path when that entry has gone missing (design D4b).

    The shape a crash inside loss-proof removal leaves behind — pointer committed to the canonical
    path, an aliased unlink took the destination with it, the process died before restoration — and,
    generally, the shape of any proof file lost to the store. Because such a pointer is canonical by
    spelling, staleness would never select it again: without this leg the break is silent until an
    operator happens to verify that one file.

    The archive copy must pass the row's OWN corroboration rules before it is republished; without
    one, the sweep warns loudly and changes nothing. No row field is written on either path — the
    pointer already names the path being repaired.
    """
    session = ctx.session
    entry = await session.get(FileEntry, entry_id)
    if entry is None or not entry.ots_path:
        return "skipped"
    recorded = Path(entry.ots_path)
    if await asyncio.to_thread(os.path.lexists, str(recorded)):
        return "skipped"  # it came back (a concurrent pass, or a survey that raced a placement)

    digest = (entry.ots_digest or entry.sha256 or "").strip().lower()
    copy = (
        await asyncio.to_thread(
            ots.find_archived_proof, ctx.store_root, ctx.collection_id, digest
        )
        if digest
        else None
    )
    if copy is None:
        log.warning(
            "collection %s: the proof recorded for %s at %s is MISSING from the store, and the "
            "superseded archive holds no copy this row's own records corroborate. Nothing was "
            "changed. The file's notarization cannot be verified until the proof is restored from "
            "a backup or the file is re-stamped",
            ctx.collection_id,
            entry.relpath,
            recorded,
        )
        return "refused"
    try:
        await asyncio.to_thread(
            ots.republish_proof,
            copy,
            recorded,
            store_root=ctx.store_root,
            collection_id=ctx.collection_id,
        )
    except ots.OtsError as exc:
        log.warning(
            "collection %s: the proof recorded for %s at %s is missing and the corroborated copy "
            "%s could not be republished (%s) — nothing was changed",
            ctx.collection_id,
            entry.relpath,
            recorded,
            copy,
            exc,
        )
        return "refused"
    log.warning(
        "collection %s: RESTORED the missing proof for %s at %s from the corroborated archive copy "
        "%s (digest %s)",
        ctx.collection_id,
        entry.relpath,
        recorded,
        copy,
        digest,
    )
    return "restored"


async def _cycle_victim(ctx: _SweepContext, deferred_ids: list[int]) -> int | None:
    """Pick the ONE row whose proof is parked to break a cycle, or ``None`` if none is eligible.

    Every id here was deferred **solely by the reference rule**: corroboration passed (a row that
    failed it never reached the destination rules) and no other rule refused it (a permanently
    refused destination is reported as ``refused``, never as ``deferred``, exactly so this selection
    can read the outcome and be right). The one further exclusion is a row already sitting in the
    holding location — parking it again would be a no-op that never frees a canonical slot.
    """
    for entry_id in deferred_ids:
        entry = await ctx.session.get(FileEntry, entry_id)
        if entry is None or not entry.ots_path:
            continue
        if Path(entry.ots_path) == ots.holding_slot(ctx.store_root, entry_id):
            continue
        return entry_id
    return None


async def heal_pointers(
    session: AsyncSession,
    collection: Collection,
    settings: Settings | None = None,
    *,
    work: PointerWork | None = None,
    progress: ProgressCb | None = None,
    run_id: int | None = None,
) -> SweepOutcome:
    """Relocate this collection's stored proofs to their files' current canonical locations.

    The restore leg runs first (a pointer naming nothing is repaired before anything is moved), then
    the stale set is worked until it converges — so a row that is BOTH (its recorded entry absent
    AND that entry no longer canonical) is restored and then relocated within this one sweep, each
    operation counted as its own work item, instead of being restored to a location it is only
    going to leave. Deferrals are re-tried within the pass as earlier
    relocations vacate slots, so a chain (A→B and C→A in one scan) converges in one sweep. A CYCLE —
    two files whose paths were swapped, each row's destination being the other's recorded proof —
    cannot converge by deferral at all: when a full pass makes no progress and reference-deferred
    rows remain, one eligible member's proof is parked in the holding location with a truthful
    committed pointer, which frees a canonical slot for the rest; the parked proof reaches its own
    canonical slot on a later sweep.

    Each admitted row is counted exactly once, whatever its outcome — relocated, parked, deferred or
    refused — so the run's ``processed`` can reach its ``total`` without double- or under-counting.
    """
    settings = settings or get_settings()
    ctx = _SweepContext(
        session=session,
        collection_id=collection.id,
        settings=settings,
        store_root=Path(settings.proof_store_path),
        run_id=run_id,
    )
    if work is None:
        work = await survey_pointer_work(session, ctx.collection_id, settings)
    outcome = SweepOutcome()
    if not work.items:
        return outcome

    async def _completed(kind: str) -> None:
        outcome.items += 1
        if kind in ("relocated", "parked", "deferred", "restored"):
            setattr(outcome, kind, getattr(outcome, kind) + 1)
        elif kind == "refused":
            outcome.refused += 1
        if progress is not None:
            await progress(outcome.items)

    try:
        for entry_id in work.absent:
            await _sweep_fence(ctx, "before the next proof restore")
            async with _sweep_lock(ctx, "after taking the proof-store lock to restore a proof"):
                kind = await _sweep_restore(ctx, entry_id)
            await _completed(kind)

        remaining = list(work.stale)
        while remaining:
            progressed = False
            deferred_ids: list[int] = []
            for entry_id in remaining:
                await _sweep_fence(ctx, "before the next proof relocation")
                async with _sweep_lock(
                    ctx, "after taking the proof-store lock to relocate a proof"
                ):
                    kind = await _sweep_relocate(ctx, entry_id)
                if kind == "deferred":
                    deferred_ids.append(entry_id)
                    continue
                progressed = True
                await _completed(kind)
            if not deferred_ids:
                break
            if progressed:
                # Slots were vacated this round; the deferred rows may now have somewhere to go.
                remaining = deferred_ids
                continue
            victim = await _cycle_victim(ctx, deferred_ids)
            if victim is None:
                # Nothing eligible to park (every deferred row is itself refused by another rule, or
                # already parked). Leave the cycle deferred, warned, and re-tried by the next sweep.
                log.warning(
                    "collection %s: %d proof relocation(s) are blocked in a cycle no member may "
                    "safely break — each destination is another row's recorded proof and every "
                    "member is refused by another rule. Nothing was changed",
                    ctx.collection_id,
                    len(deferred_ids),
                )
                for _ in deferred_ids:
                    await _completed("deferred")
                break
            async with _sweep_lock(ctx, "after taking the proof-store lock to break a cycle"):
                kind = await _sweep_relocate(
                    ctx, victim, destination=ots.holding_slot(ctx.store_root, victim)
                )
            log.warning(
                "collection %s: breaking a proof-relocation cycle by parking file row %s's proof "
                "at %s — its canonical slot is freed for the rest of the cycle and the parked "
                "proof reaches its own canonical location on a later sweep",
                ctx.collection_id,
                victim,
                ots.holding_slot(ctx.store_root, victim),
            )
            await _completed(kind)
            remaining = [entry_id for entry_id in deferred_ids if entry_id != victim]
    except ots.OtsError as exc:
        # The proof-store lock is held elsewhere (or cannot be taken at all). Transient by
        # construction: nothing was placed and every remaining row keeps its truthful pointer. Stop
        # rather than paying one lock timeout per row for a resource someone else holds.
        log.warning(
            "collection %s: stopping the proof relocation sweep after %d item(s) — %s",
            ctx.collection_id,
            outcome.items,
            exc,
        )

    if outcome.relocated or outcome.parked or outcome.restored:
        log.info(
            "collection %s: healing sweep relocated %d proof(s), parked %d, restored %d, deferred "
            "%d, refused %d",
            ctx.collection_id,
            outcome.relocated,
            outcome.parked,
            outcome.restored,
            outcome.deferred,
            outcome.refused,
        )
    return outcome


async def upgrade_incomplete(
    session: AsyncSession,
    collection: Collection | None = None,
    settings: Settings | None = None,
    *,
    progress: ProgressCb | None = None,
    run_id: int | None = None,
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

    ``run_id`` (passed by :func:`upgrade_collection`) fences the pass the same way
    :func:`stamp_pending` is fenced: ``ots upgrade`` REWRITES the ``.ots`` file in place, so a pass
    whose lease was reclaimed is a second writer over another operation's proofs. The claim is
    re-read before each proof; a lost one raises :class:`collections.LeaseLost` and the proofs
    already upgraded (and already committed by ``progress``) stand.

    And the same resource lock closes the same check-then-act gap: each rewrite happens inside the
    proof-store lock for **that row's** collection (:class:`ots.CollectionProofLock`, so a
    fleet-wide pass locks each collection only while it is working on it), with the lease re-read
    after the lock is taken. A pass whose claim was reclaimed while it waited for the lock aborts
    there instead of rewriting a proof the new claimant is placing (design D10). The lock is held
    per proof, never across the pass, so the daily multi-hour upgrade can delay another placer by
    one ``ots upgrade`` at most.
    """
    settings = settings or get_settings()
    store_root = Path(settings.proof_store_path)
    stmt = select(FileEntry).where(FileEntry.ots_state == "incomplete")
    if collection is not None:
        stmt = stmt.where(FileEntry.collection_id == collection.id)
    files = list(await session.scalars(stmt))

    upgraded = still = processed = 0

    async def _fence(stage: str) -> None:
        """Stop the pass unless ``run_id`` still holds its claim — before AND after the lock."""
        if run_id is None or await collections.lease_held(run_id):
            return
        log.warning(
            "the operation claim (run %s) was RECLAIMED mid-upgrade — stopping %s after %d "
            "proof(s); those already upgraded stand and the rest stay incomplete",
            run_id,
            stage,
            processed,
        )
        await session.rollback()  # nothing commits after the fence fires
        raise collections.LeaseLost(
            f"the operation claim (run {run_id}) was reclaimed mid-upgrade"
        )

    for entry in files:
        # The fence, before this proof is rewritten (design D10). One SELECT against a pass whose
        # per-proof cost is a subprocess spawn plus calendar traffic.
        await _fence("before the next proof")
        processed += 1
        complete = False
        if not entry.ots_path or not Path(entry.ots_path).exists():
            log.warning("incomplete proof has no .ots on disk: %s", entry.ots_path)
        else:
            # The fence AT the resource: `ots upgrade` rewrites the `.ots` in place, so this pass
            # and a replacement claimant placing over the same path must not overlap. Locked per
            # ROW's collection, so a fleet-wide pass never holds two collections at once.
            lock = ots.CollectionProofLock(store_root, entry.collection_id)
            try:
                await asyncio.to_thread(lock.acquire)
            except ots.OtsError as exc:
                # Transient: nothing was rewritten and every remaining proof stays `incomplete`.
                # Stop rather than paying one timeout per proof for a resource someone else holds.
                # This row was counted as processed a moment ago and was not: hand the count back.
                processed -= 1
                log.warning("stopping the upgrade pass after %d proof(s) — %s", processed, exc)
                break
            try:
                await _fence("after taking the proof-store lock")
                try:
                    # Off the event loop: `ots upgrade` spawns a process and contacts the calendars.
                    complete = await asyncio.to_thread(ots.upgrade, entry.ots_path)
                except ots.OtsError as exc:
                    log.warning("upgrade failed for %s: %s", entry.ots_path, exc)
                if entry.ots_digest is None:
                    # Corroborated provenance backfill (design D3). Offline, never raises, and
                    # cannot change the upgrade outcome decided above.
                    facts = await asyncio.to_thread(ots.read_proof_facts, entry.ots_path)
                    _backfill_provenance(entry, facts)
            finally:
                lock.release()
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
    # No work of any kind (nothing incomplete, no pointers to heal, no absent recorded entries), so
    # no run row survives -- whether that was true at the pre-check or became true under the claim.
    idle: bool = False
    sweep: SweepOutcome = field(default_factory=SweepOutcome)  # what the healing sweep did


async def _count_incomplete(session: AsyncSession, collection_id: int) -> int:
    """How many of this collection's proofs are still waiting for a Bitcoin attestation."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(FileEntry)
            .where(FileEntry.collection_id == collection_id, FileEntry.ots_state == "incomplete")
        )
        or 0
    )


async def _upgrade_work_exists(
    session: AsyncSession, collection_id: int, settings: Settings | None
) -> bool:
    """Whether any of the upgrade pass's three admission reasons holds right now (advisory)."""
    if await _count_incomplete(session, collection_id):
        return True
    return bool((await survey_pointer_work(session, collection_id, settings)).items)


async def _discard_unstarted_run(
    session: AsyncSession, run: Run, collection_id: int
) -> None:
    """Delete a claim row whose work vanished before it began — guarded, so a reclaim is respected.

    The DELETE re-asserts ``result='running'``: if the claim was reclaimed between the commit and
    this call, the reclamation's ``interrupted`` row is the true record of what happened and must
    stand. The ORM object is expunged either way, so nothing in this session can flush it back.
    """
    run_id = run.id
    result = await session.execute(delete(Run).where(Run.id == run_id, Run.result == "running"))
    await session.commit()
    session.expunge(run)
    if not result.rowcount:
        log.warning(
            "collection %s: the upgrade claim (run %s) had no work left to do and was RECLAIMED "
            "before it could be discarded — leaving the terminal state the reclamation wrote",
            collection_id,
            run_id,
        )
        return
    log.debug(
        "collection %s: the upgrade's work was completed by another pass between the pre-check and "
        "the claim; the empty run row was discarded",
        collection_id,
    )


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

    The pass has THREE independent admission reasons, each of which claims the collection on its
    own: incomplete proofs to upgrade, stale pointers to heal, and rows whose recorded proof entry
    is absent from the store. A collection with none of the three records no run at all (no empty
    daily rows); a tripwire collection carrying historical proofs is admitted by the last two
    exactly like any other. ``total`` is one work item per operation the pass will perform, and the
    healing sweep runs BEFORE the proof upgrades so a relocated proof is upgraded at its new
    canonical location in the same pass. ``kind='upgrade'`` runs never feed scan freshness, so this
    can never refresh the dead-man's switch.

    **The work set is surveyed under the claim, not before it.** The pre-claim look is advisory —
    it only decides whether to try for the slot at all — because the pass that currently holds the
    slot may complete the surveyed work before this one claims it: a run created on the older
    answer would state a ``total`` for work that no longer exists (and, at the limit, record an
    empty run in violation of "no work -> no run"). The authoritative survey happens immediately
    after the claim; if it comes back empty the provisional claim row is discarded.

    **The run's terminal numbers are what actually happened.** ``processed`` is the shared counter
    both halves advance, never the admission total, and an ``ok`` pass that did not reach its total
    (the sweep or the upgrade loop stopped early rather than wait out a proof-store lock someone
    else holds) finalizes ``partial`` — the run-health vocabulary for "completed, work skipped".
    """
    settings = settings or get_settings()
    # Identity read up front: a lost claim rolls back and expires the ORM object, and a lazy
    # refresh on the refusal path would raise rather than report the refusal.
    collection_id, collection_name = collection.id, collection.name
    result = UpgradePass(collection_id=collection_id)
    # `blocking_run` reclaims a claim whose holder has stopped heartbeating before reporting it as
    # held — otherwise a killed `cairn stamp` would refuse every later upgrade of this collection.
    if await collections.blocking_run(session, collection_id) is not None:
        result.refused = True
        return result
    # An ADVISORY pre-check only, exactly like `blocking_run` above: it exists so a collection with
    # nothing to do never claims a slot or writes a run row. Its numbers are NOT used — a rival
    # pass holding the slot can complete this very work between here and the claim below, and a run
    # created on the strength of a pre-claim survey states a `total` for work that no longer exists.
    if not await _upgrade_work_exists(session, collection_id, settings):
        result.idle = True
        return result

    run = Run(
        collection_id=collection_id,
        kind="upgrade",
        started=_utcnow(),
        result="running",
    )
    # The `blocking_run` pre-check above is only advisory; this commit (guarded by the partial unique
    # index) is the race-free claim.
    if await collections.claim_run(session, run) is None:
        result.refused = True
        return result
    result.run = run

    run_id = run.id
    # THE AUTHORITATIVE SURVEY, taken under the claim. Nothing else may hold the collection's slot
    # now, so this answer cannot be completed out from under the run: it is both the work set the
    # pass will execute and the `total` it will be measured against. (`total` is briefly NULL —
    # indeterminate progress on the badge — between the claim and the commit below, exactly as the
    # stamp backfill's is.)
    incomplete = await _count_incomplete(session, collection_id)
    pointer_work = await survey_pointer_work(session, collection_id, settings)
    total_items = incomplete + pointer_work.items
    if not total_items:
        # The work was completed by whoever held the slot between the pre-check and this claim.
        # There is nothing to record, and "no work of any kind -> no run" is a rule about what the
        # pass FOUND, not about when it looked: the provisional claim row is discarded so the daily
        # pass does not leave an empty run behind it.
        await _discard_unstarted_run(session, run, collection_id)
        result.run = None
        result.idle = True
        return result
    run.total = total_items
    await session.commit()
    run_result = "ok"
    upgraded_count = 0
    # Tracked as plain locals: a fence hit rolls the session back, expiring `run`, so the finalizing
    # values must never be read back off the ORM object.
    processed_count = 0

    async def _progress(done: int) -> None:
        nonlocal processed_count
        processed_count = done
        run.processed = done
        # Same liveness contract as the stamp backfill: an upgrade over tens of thousands of proofs
        # is exactly the long-running CLI claim the reaper must not revoke (design D10).
        run.heartbeat_at = _utcnow()
        await session.commit()

    # Keepalive around the whole pass (one slow proof must not starve the lease); the fence is
    # inside `upgrade_incomplete`, before each proof is rewritten.
    async with collections.run_keepalive(run_id):
        try:
            # The healing sweep FIRST, so a proof relocated this pass is upgraded at its new
            # canonical location rather than at the one it is about to leave.
            result.sweep = await heal_pointers(
                session,
                collection,
                settings,
                work=pointer_work,
                progress=_progress,
                run_id=run_id,
            )
            swept = result.sweep.items

            async def _upgrade_progress(done: int) -> None:
                # One counter across both halves of the run: the sweep's items come first, so
                # `processed` advances by exactly one per completed work item throughout.
                await _progress(swept + done)

            outcome = await upgrade_incomplete(
                session, collection, settings, progress=_upgrade_progress, run_id=run_id
            )
            result.upgraded = outcome.get("upgraded", 0)
            result.still_incomplete = outcome.get("still_incomplete", 0)
            upgraded_count = result.upgraded
        except collections.LeaseLost:
            log.warning(
                "upgrade for collection %s (%s) was RECLAIMED mid-run (run %s) — stopping; proofs "
                "already upgraded stand and the run row keeps the reclamation's state",
                collection_id,
                collection_name,
                run_id,
            )
            run_result = "interrupted"
        except Exception:
            log.exception("upgrade failed for collection %s (%s)", collection_id, collection_name)
            run_result = "error"

    # `processed` is the ACTUAL shared counter, never the admission total: a pass that stopped
    # early — the healing sweep swallowing a contended proof-store lock, `upgrade_incomplete`
    # breaking on the same — leaves items undone, and writing `total` over the count would report
    # them as finished. An `ok` run that did not reach its total is `partial`, the run-health
    # vocabulary's word for "this pass completed, work was skipped": honest on the card, and the
    # next pass simply picks the rest up. No retry loop belongs here — whoever holds that lock is
    # doing the work this pass would have waited for.
    if run_result == "ok" and processed_count < total_items:
        run_result = "partial"
        log.warning(
            "upgrade for collection %s (%s) completed %d of %d work item(s) — the rest were "
            "skipped this pass (typically the collection's proof-store lock held elsewhere, which "
            "the pass refuses to wait out) and are retried by the next pass",
            collection_id,
            collection_name,
            processed_count,
            total_items,
        )
    finalized = await collections.finalize_if_held(
        session,
        run_id,
        result=run_result,
        upgraded=upgraded_count,
        processed=processed_count,
        finished=_utcnow(),
    )
    if not finalized:
        log.warning(
            "upgrade run %s (collection %s) was RECLAIMED before it could finalize — leaving the "
            "terminal state the reclamation wrote",
            run_id,
            collection_id,
        )
    await session.refresh(run)
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
