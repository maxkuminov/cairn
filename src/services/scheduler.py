"""Background scan scheduler + health-freshness query (DESIGN.md §5).

A single ``asyncio`` task runs a tick loop: on startup it scans every collection and runs the OTS
upgrade pass, then it wakes every ``scan_interval_seconds`` to scan the collections whose per-collection
``hash_cadence_seconds`` has elapsed (sequentially, cheapest-first — the scanner is the single
writer) and, once
every ``upgrade_interval_seconds``, runs the upgrade pass again. A per-collection error is logged and
never crashes the loop; the loop stops cleanly when its ``stop_event`` is set.

Freshness is derived purely from ``kind='scan'`` rows in the ``runs`` table
(:func:`compute_health`), so it is correct even for ``scheduler_enabled=false`` deployments where
external ``cairn scan`` invocations write the runs. The upgrade pass records its own
``kind='upgrade'`` run (with live progress); because freshness ignores non-scan kinds, that run can
never falsely refresh a dead collection's dead-man's switch.

**Single-writer audit (design D10).** Every proof-mutating pass here already runs inside the
collection's DB-enforced operation claim, and that is deliberate: proof placement is check-then-act
(inspect the canonical path -> preserve -> place -> record), so two writers would silently destroy a
proof. :func:`run_due_scans` claims via ``scan_collection``'s own ``claim_run``; :func:`run_daily_upgrade`
delegates to ``proofs.upgrade_collection``, which claims a ``kind='upgrade'`` run of its own. No
lock belongs inside ``_place_proof``, ``stamp_pending`` or ``upgrade_incomplete`` — the claim wraps
them from the caller, and ``stamp_pending`` is called from inside a scan that already holds one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..database import get_sessionmaker
from ..models.db import Collection, FileEntry, Run
from . import proofs, scanner
from .collections import (
    RUN_HEARTBEAT_TIMEOUT_SECONDS,  # noqa: F401  -- re-exported: callers read it from here
    abandoned_claim_clause,
    as_aware,
    blocking_run,
    claim_is_live,
    heartbeat_cutoff,
    list_collections,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

log = logging.getLogger("cairn.scheduler")

# Per-collection first-run offset so a fleet of collections does not all fire on the very first tick.
STAGGER_SECONDS = 1.0

# The abandonment interval for an operation claim (``RUN_HEARTBEAT_TIMEOUT_SECONDS``) is defined in
# ``services.collections`` next to the claim itself and re-exported here: the reaper below and
# ``collections.reclaim_stale_claim`` MUST apply the same threshold, or the two reclamation paths
# would disagree about which claims are dead.

CollectionState = Literal["fresh", "pending", "stale"]
HealthStatus = Literal["ok", "degraded"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# One definition, shared with the claim predicate it is read alongside (``collections.as_aware``).
_as_aware = as_aware


# --- Freshness model ------------------------------------------------------------------------


@dataclass
class CollectionHealth:
    # The collection's own id, so a reader (the panel's cards, an external monitor) can match a
    # freshness record to the collection it describes without matching on `name`, which no
    # constraint makes unique across owners.
    id: int
    name: str
    state: CollectionState
    # Age of the newest COMPLETED (`ok`/`partial`) scan; None when the collection has never
    # completed one — including a collection that is fresh only because a scan is in flight
    # (leg (b) below). An unfinished run has no "last scan" age to report, and reporting its
    # elapsed time under that name would state a completion that has not happened.
    last_scan_age_seconds: float | None


@dataclass
class HealthReport:
    status: HealthStatus
    collections: list[CollectionHealth] = field(default_factory=list)


def _threshold(collection: Collection, settings: Settings) -> int:
    """Freshness window: ``max(2 × cadence, floor)`` (the floor stops fast collections flapping)."""
    return max(2 * collection.hash_cadence_seconds, settings.health_freshness_floor_seconds)


async def compute_health(
    session: AsyncSession, settings: Settings, user_id: int | None = None
) -> HealthReport:
    """Classify each collection's scan freshness and roll it up to an overall status.

    ``user_id`` scopes the report to one owner's collections; ``None`` (the default) is fleet-wide.
    ``/healthz`` calls it fleet-wide — it monitors the *installation*, and a machine-facing
    dead-man's switch that silently skipped one owner's collections would have a hole in it exactly
    where nobody is looking. The **panel** passes the viewer's id, because the health pill names a
    number and then sends the operator to an owner-scoped ``/collections`` list: a fleet-global
    count rendered above that list is a number with no referent on the page it links to (design D5).

    Freshness counts ``kind='scan'`` runs only — ``stamp``/``upgrade`` runs are deliberately ignored
    so they cannot refresh the dead-man's switch. A collection is **fresh** on either of two legs:

    - **(a) a completed scan is recent** — the newest ``kind='scan'`` run with a result of ``ok`` or
      ``partial`` finished within ``threshold = max(2 × hash_cadence_seconds, freshness_floor)``;
    - **(b) a scan is in flight and demonstrably alive** — a ``kind='scan'`` run is ``running`` and
      its last reported progress (``heartbeat_at``, falling back to ``started``) is within
      :data:`RUN_HEARTBEAT_TIMEOUT_SECONDS`, *regardless of the cadence window*.

    Leg (b) is the fix for audit issue #5 and must not be quietly removed: before it, a scan that
    legitimately ran longer than its own freshness window aged out its OWN freshness while it was
    still working, and ``/healthz`` flapped ``degraded`` for a collection that was being scanned at
    that very moment. A false ``degraded`` on a dead-man's switch is not a harmless conservative
    default — it is the alarm that teaches the operator to ignore the alarm.

    **But leg (b) cannot simply trust ``result='running'``.** That column is not evidence of life: a
    process killed mid-scan leaves its ``running`` row behind until something reclaims it, and a
    recently-started one would read *fresh* — a crashed scanner reporting healthy, precisely the
    false negative the switch exists to prevent. So the leg is gated on the same liveness test the
    reclamation paths use (:func:`reap_orphaned_runs`, :func:`collections.reclaim_stale_claim`) with
    the same constant: if the switch and the reaper applied different thresholds there would be a
    window in which a run is dead to one and alive to the other, which is a state nobody can reason
    about. A live scan is therefore fresh for as long as it keeps heartbeating, and goes stale
    within one lease interval of the process dying.

    Neither leg fresh → ``pending`` **only** while the collection is inside its startup grace AND
    has no ``kind='scan'`` run at all (the grace covers a never-scanned collection, nothing else —
    a first scan that terminated ``error``/``interrupted`` inside the window is ``stale``, because
    something *did* run and it did not complete); otherwise ``stale``.

    ``last_scan_age_seconds`` describes the newest **completed** scan and is ``None`` when there is
    none — so a collection fresh only by leg (b) reports ``fresh`` with a ``None`` age, which is
    exactly true: it is being scanned, and no scan has finished yet.

    Overall status is ``degraded`` if any collection is ``stale``, else ``ok``. Datastore
    reachability is the ``/healthz`` caller's concern (``error``), not this function's.
    """
    now = _utcnow()
    rows: list[CollectionHealth] = []
    any_stale = False

    for collection in await list_collections(session, user_id=user_id):
        threshold = _threshold(collection, settings)

        # Leg (a) — the newest COMPLETED scan. Dated from `finished`; `started` only as a fallback
        # for a row whose finish was never written (it cannot be selected here without a result, so
        # this is belt-and-braces).
        completed = await session.scalar(
            select(Run)
            .where(
                Run.collection_id == collection.id,
                Run.kind == "scan",
                Run.result.in_(("ok", "partial")),
            )
            .order_by(Run.finished.desc().nulls_last())
            .limit(1)
        )
        age: float | None = None
        if completed is not None:
            age = (now - _as_aware(completed.finished or completed.started)).total_seconds()

        # Leg (b) — an in-flight scan, but only one that is still reporting progress. The liveness
        # test is evaluated in Python on the same `coalesce(heartbeat_at, started)` the reaper uses,
        # so both sides of the lease read one value by one rule.
        running = await session.scalar(
            select(Run)
            .where(
                Run.collection_id == collection.id,
                Run.kind == "scan",
                Run.result == "running",
            )
            .order_by(Run.started.desc())
            .limit(1)
        )
        # THE shared predicate (`collections.claim_is_live`), not a second spelling of it: health
        # and both reclaimers must agree about the exact abandonment boundary, or a claim can be
        # reclaimed out from under a collection this switch is still calling fresh.
        live_scan = running is not None and claim_is_live(running, now)

        if (age is not None and age <= threshold) or live_scan:
            state: CollectionState = "fresh"
        else:
            # The startup grace covers a collection nothing has ever scanned. A collection whose
            # only scan run ended `error`/`interrupted` is NOT in grace — a scan was attempted and
            # did not complete, which is a report, not an absence of one.
            never_scanned = completed is None and running is None and not await session.scalar(
                select(Run.id)
                .where(Run.collection_id == collection.id, Run.kind == "scan")
                .limit(1)
            )
            created_age = (now - _as_aware(collection.created_at)).total_seconds()
            state = "pending" if (never_scanned and created_age <= threshold) else "stale"

        rows.append(
            CollectionHealth(
                id=collection.id,
                name=collection.name,
                state=state,
                last_scan_age_seconds=age,
            )
        )
        if state == "stale":
            any_stale = True

    return HealthReport(status="degraded" if any_stale else "ok", collections=rows)


async def _stale_run_ids(
    session: AsyncSession, now: datetime
) -> tuple[list[int], int]:
    """Ids of the ``running`` runs whose claim is abandoned at ``now``, plus the count still running.

    Split out from :func:`reap_orphaned_runs` so the read and the guarded write are separately
    visible (and separately testable), exactly as :func:`collections._stale_claim_id` is on the
    claim path: between the two, a live process may heartbeat, and the write must lose that race.
    """
    running = list(await session.scalars(select(Run).where(Run.result == "running")))
    stale = [run.id for run in running if not claim_is_live(run, now)]
    return stale, len(running)


async def reap_orphaned_runs(session: AsyncSession) -> int:
    """Mark every STALE ``result='running'`` run as ``interrupted`` (finished now); return the count.

    Called on startup **and on every scheduler tick** — an abandoned claim created after startup
    (a SIGKILLed ``cairn stamp``) must not have to wait for a restart to be tidied up, and a
    long-idle collection should not have to wait for someone to attempt an operation on it either.
    It is the fleet-wide sweep; :func:`collections.reclaim_stale_claim` is the in-band counterpart
    that runs at the moment of a blocked claim (so CLI-only deployments, which run no scheduler at
    all, still recover). Both apply the same liveness test and write the same terminal state, and
    both are idempotent, so running them concurrently is harmless.

    A run still ``running`` is usually one orphaned by a crash/kill
    mid-flight: left alone it freezes the live status badge at "in progress" forever and (via the
    concurrency guard) blocks every new operation on that collection. Reaping clears the stale
    indicator and unblocks the collection. The terminal state is ``interrupted`` (not ``error``) so a
    benign restart-induced interruption — e.g. a deploy killing a long scan mid-flight — is not
    conflated with a genuine scan failure. Like ``error``, an ``interrupted`` run does not refresh
    scan freshness: :func:`compute_health` counts a completed ``ok``/``partial`` ``kind='scan'`` run,
    or a ``running`` one that is still heartbeating — and a run reaped for having stopped
    heartbeating fails both tests before and after it is relabelled.

    **"Usually" is why this is not a bulk update.** The claim is cross-PROCESS (design D10): a
    ``cairn stamp`` or ``cairn upgrade`` invoked from cron can legitimately hold a collection's claim
    while the web process restarts. Clearing it because *this* process just started would revoke a
    LIVE claim, letting the scheduler or panel start a second writer over the same proofs — the
    concurrent check-then-act that the claim exists to prevent, reintroduced by the cleanup meant to
    protect it. So liveness decides, not process startup: a run is reaped only when it has reported
    no progress for :data:`RUN_HEARTBEAT_TIMEOUT_SECONDS`, measured on ``heartbeat_at`` (refreshed by
    every scan batch, stamp batch and upgraded proof) and falling back to ``started`` for a run that
    has not yet reported any (including rows written before migration ``0011``).

    The cost is bounded and one-directional: a genuinely dead run keeps its collection claimed for up
    to the threshold, during which the scheduler simply skips that collection and the badge still
    reads "in progress" — while the dead-man's switch is untouched, since :func:`compute_health`
    applies this very liveness test to the same row: a dead run confers no freshness whether or not
    the reaper has got to it yet, so the two never disagree about a collection's state. A collection
    that is late by a threshold's worth of minutes is a delay; a second proof writer is evidence loss.

    **The UPDATE re-asserts staleness, so a concurrent heartbeat wins.** The selection above and the
    write below are separate statements, and a live holder may report progress in between: writing on
    the strength of the earlier read alone would revoke a lease the reader had just proved alive —
    the same race :func:`collections.reclaim_stale_claim` guards against, and the same loss (a second
    proof writer). Re-asserting the full stale condition (``result='running'`` AND liveness
    ``<= cutoff``) inside the WHERE makes the decision atomic with the write: a heartbeat that lands
    first fails the predicate, that row is simply not matched, and the returned count reflects what
    was actually reaped rather than what was selected.
    """
    now = _utcnow()
    cutoff = heartbeat_cutoff(now)
    stale, live = await _stale_run_ids(session, now)
    if not stale:
        if live:
            log.debug(
                "%d in-progress run(s) still heartbeating — left claimed, not reaped", live
            )
        return 0
    result = await session.execute(
        update(Run)
        .where(
            Run.id.in_(stale),
            Run.result == "running",
            abandoned_claim_clause(cutoff),
        )
        .values(result="interrupted", finished=now)
    )
    await session.commit()
    return result.rowcount or 0


# --- Due-collection selection -------------------------------------------------------------------


async def collection_costs(session: AsyncSession) -> dict[int, tuple[int, int]]:
    """Per-collection estimated scan cost ``(total_bytes, file_count)`` over non-missing tracked files.

    One grouped aggregate over ``files``: total bytes is the dominant cost of a deep (full re-hash)
    pass, file count the cost of a quick stat-only pass. ``missing`` rows are excluded — a gone file
    has no bytes to read, so it must not inflate the estimate. A collection with no tracked rows is
    simply absent from the map (callers default it to ``(0, 0)``). Cheap enough to run every tick
    for a homelab-scale fleet.
    """
    rows = await session.execute(
        select(
            FileEntry.collection_id,
            func.coalesce(func.sum(FileEntry.size), 0),
            func.count(),
        )
        .where(FileEntry.status != "missing")
        .group_by(FileEntry.collection_id)
    )
    return {cid: (int(total), int(count)) for cid, total, count in rows}


def due_collections(
    collections: list[Collection],
    next_due: dict[int, float],
    now: float,
    cost: dict[int, tuple[int, int]] | None = None,
) -> list[Collection]:
    """Return collections whose ``next_due`` (default 0 = due now) has passed, in a deterministic order.

    ``now`` and ``next_due`` values are monotonic seconds (``time.monotonic``). When ``cost`` (from
    :func:`collection_costs`) is given, due collections are ordered cheapest-first — ascending by
    ``(total_bytes, file_count, id)`` — so quick collections finish promptly and a long large-collection
    scan lands at the end of the pass instead of blocking the collections behind it; the trailing ``id``
    makes the order total and stable across ticks. When ``cost`` is omitted the order follows the
    input order (which :func:`list_collections` keeps stable by ``id``).
    """
    due = [c for c in collections if next_due.get(c.id, 0.0) <= now]
    if cost is None:
        return due
    return sorted(due, key=lambda c: (*cost.get(c.id, (0, 0)), c.id))


def _deep_owed(collection: Collection, now_wall: datetime) -> bool:
    """True if a deep (full re-hash) verify pass is due for this collection.

    Wall-clock (not monotonic) so an overdue deep pass survives a restart. ``verify_cadence_seconds``
    of 0 disables deep verify; a collection never deep-scanned (``last_full_scan_at`` is None) is owed.
    """
    if collection.verify_cadence_seconds <= 0:
        return False
    last = collection.last_full_scan_at
    if last is None:
        return True
    return (now_wall - _as_aware(last)).total_seconds() >= collection.verify_cadence_seconds


# --- Scan + upgrade passes ------------------------------------------------------------------


async def run_due_scans(
    session: AsyncSession, next_due: dict[int, float], now: float
) -> int:
    """Scan every due collection sequentially; defer each by its cadence. Returns the count scanned.

    Due collections are scanned cheapest-first (ascending estimated cost — see :func:`collection_costs` /
    :func:`due_collections`) so quick collections complete promptly and a long large-collection scan lands at
    the end of the pass rather than blocking the collections behind it.

    A failure scanning one collection is logged and skipped — its ``next_due`` is still advanced so a
    persistently broken collection does not monopolise every tick — and remaining collections still run.

    A collection whose deep-verify cadence has elapsed is scanned in deep mode (a full re-hash that
    catches silent bit-rot). A deep pass is a superset of a quick pass, so it *replaces* the quick
    pass that tick. At most one deep pass runs per tick — a long re-hash must not starve the other
    collections's freshness — so any further owed collections fall back to a quick pass and go deep on a
    later tick. ``last_full_scan_at`` advances only after a deep pass completes successfully.
    """
    collections = await list_collections(session)
    cost = await collection_costs(session)
    now_wall = _utcnow()
    deep_used = False  # at most one deep pass per tick (starvation guard)
    scanned = 0
    for collection in due_collections(collections, next_due, now, cost):
        # Skip a collection that already has an operation in flight (a manual scan or stamp backfill) —
        # the scanner is the single writer. Leave next_due unchanged so it is reconsidered next tick.
        if await blocking_run(session, collection.id) is not None:
            log.info("skip scan for collection %s — operation already in progress", collection.id)
            continue
        deep = (not deep_used) and _deep_owed(collection, now_wall)
        if deep:
            deep_used = True
        try:
            await scanner.scan_collection(session, collection, deep=deep)
            scanned += 1
            if deep:
                collection.last_full_scan_at = now_wall
                await session.commit()
        except Exception:
            log.exception("scan failed for collection %s (%s)", collection.id, collection.name)
        finally:
            next_due[collection.id] = now + collection.hash_cadence_seconds
    return scanned


async def run_daily_upgrade(session: AsyncSession) -> int:
    """Run the OTS upgrade pass across all collections; return the total proofs upgraded.

    The per-collection body (claim a typed ``kind='upgrade'`` run, upgrade, thread progress,
    finalize) lives in :func:`proofs.upgrade_collection` so the scheduler and ``cairn upgrade``
    cannot drift apart — this loop is the fleet iteration and the skip log, nothing more.

    Because :func:`compute_health` keys freshness on ``kind='scan'`` runs only, an ``upgrade`` run
    never refreshes the dead-man's switch — which is why we can record a real run instead of the old
    "amend the latest scan run" workaround. A collection with no incomplete proofs records nothing
    (no empty daily runs), and a collection that already has an operation in flight is skipped so we
    never start a second writer on it.
    """
    total = 0
    for collection in await list_collections(session):
        outcome = await proofs.upgrade_collection(session, collection)
        if outcome.refused:
            log.info(
                "skip upgrade for collection %s — operation already in progress",
                outcome.collection_id,
            )
            continue
        total += outcome.upgraded
    return total


# --- The loop -------------------------------------------------------------------------------


async def scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """Run the background scan + upgrade loop until ``stop_event`` is set.

    On start: scan every collection once and run the upgrade pass (so a freshly-started instance
    clears any backlog). Then wake every ``scan_interval_seconds`` to scan due collections and, once
    ``upgrade_interval_seconds`` has elapsed, run the upgrade pass again. Each iteration is wrapped
    so a single error never kills the loop, and ``stop_event`` is awaited as the wait timeout so
    shutdown is prompt rather than waiting out a full tick. Each tick also reaps abandoned run
    claims first (:func:`reap_orphaned_runs`), so a claim orphaned after startup — a killed CLI
    stamp, a crashed scan — is released without waiting for a restart.
    """
    settings: Settings = get_settings()
    tick = max(0.01, settings.scan_interval_seconds)
    sessionmaker = get_sessionmaker()

    # Startup: scan EVERY collection once + run the upgrade pass. The startup pass leaves ``next_due``
    # empty so every collection is due-now (``due_collections`` defaults a missing entry to 0.0 ≤ now); the
    # stagger is for the steady-state loop only and is seeded afterwards from each collection's cadence
    # (so subsequent ticks spread out and a fleet does not all re-fire on the same tick).
    next_due: dict[int, float] = {}
    try:
        async with sessionmaker() as session:
            now = time.monotonic()
            await run_due_scans(session, next_due, now)
            await run_daily_upgrade(session)
    except Exception:  # pragma: no cover - defensive; one bad startup must not crash the task
        log.exception("scheduler startup pass failed")
        next_due = {}

    last_upgrade = time.monotonic()

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=tick)
        except (TimeoutError, asyncio.TimeoutError):
            pass  # normal tick wake
        if stop_event.is_set():
            break

        try:
            async with sessionmaker() as session:
                now = time.monotonic()
                # Tidy up claims abandoned SINCE startup (a killed CLI stamp, a crashed scan) before
                # selecting due work: the startup reap alone would leave such a collection wedged
                # until the next restart, since its claim still satisfies the one-run-per-collection
                # index. Cheap (one indexed read over `running` rows) and idempotent.
                reaped = await reap_orphaned_runs(session)
                if reaped:
                    log.warning("reaped %d abandoned run claim(s)", reaped)
                await run_due_scans(session, next_due, now)
                if now - last_upgrade >= settings.upgrade_interval_seconds:
                    await run_daily_upgrade(session)
                    last_upgrade = now
        except Exception:  # one bad iteration must never crash the loop
            log.exception("scheduler tick failed")

    log.info("scheduler loop stopped")
