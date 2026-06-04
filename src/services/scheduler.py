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
from .collections import active_run, claim_run, list_collections

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI

log = logging.getLogger("cairn.scheduler")

# Per-collection first-run offset so a fleet of collections does not all fire on the very first tick.
STAGGER_SECONDS = 1.0

CollectionState = Literal["fresh", "pending", "stale"]
HealthStatus = Literal["ok", "degraded"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: datetime) -> datetime:
    """Treat a naive datetime (SQLite round-trips timezone-aware values as naive) as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# --- Freshness model ------------------------------------------------------------------------


@dataclass
class CollectionHealth:
    name: str
    state: CollectionState
    last_scan_age_seconds: float | None  # None when the collection has no successful run yet


@dataclass
class HealthReport:
    status: HealthStatus
    collections: list[CollectionHealth] = field(default_factory=list)


def _threshold(collection: Collection, settings: Settings) -> int:
    """Freshness window: ``max(2 × cadence, floor)`` (the floor stops fast collections flapping)."""
    return max(2 * collection.hash_cadence_seconds, settings.health_freshness_floor_seconds)


async def compute_health(session: AsyncSession, settings: Settings) -> HealthReport:
    """Classify each collection's scan freshness and roll it up to an overall status.

    Per collection, the newest **scan** run (``kind='scan'``) defines freshness against
    ``threshold = max(2 × hash_cadence_seconds, freshness_floor)`` — ``stamp``/``upgrade`` runs are
    deliberately ignored so they cannot refresh the dead-man's switch:

    - ``fresh``   — a successful (``ok``/``partial``) run finished within the threshold, **or** a
      scan is actively ``running`` and started within the threshold (a long scan must not age out
      its own freshness while it is still progressing);
    - ``pending`` — no scan run yet, but the collection was created within the threshold
      (startup grace, so a freshly-added collection does not immediately trip the switch);
    - ``stale``   — otherwise (including a ``running`` scan that started longer ago than the
      threshold — a genuinely stalled run still trips the switch).

    Overall status is ``degraded`` if any collection is ``stale``, else ``ok``. Datastore
    reachability is the ``/healthz`` caller's concern (``error``), not this function's.
    """
    now = _utcnow()
    rows: list[CollectionHealth] = []
    any_stale = False

    for collection in await list_collections(session):
        threshold = _threshold(collection, settings)
        # Consider the newest scan run regardless of result: a completed (ok/partial) run is dated
        # from its finish, but an in-flight ``running`` scan keeps the collection fresh from its start
        # so a scan running longer than the threshold cannot trip the switch against itself.
        latest = await session.scalar(
            select(Run)
            .where(
                Run.collection_id == collection.id,
                Run.kind == "scan",
                Run.result.in_(("ok", "partial", "running")),
            )
            .order_by(Run.started.desc())
            .limit(1)
        )

        if latest is not None:
            ref = _as_aware(latest.started if latest.result == "running" else (latest.finished or latest.started))
            age = (now - ref).total_seconds()
            state: CollectionState = "fresh" if age <= threshold else "stale"
            rows.append(CollectionHealth(name=collection.name, state=state, last_scan_age_seconds=age))
        else:
            created_age = (now - _as_aware(collection.created_at)).total_seconds()
            state = "pending" if created_age <= threshold else "stale"
            rows.append(
                CollectionHealth(name=collection.name, state=state, last_scan_age_seconds=None)
            )

        if state == "stale":
            any_stale = True

    return HealthReport(status="degraded" if any_stale else "ok", collections=rows)


async def reap_orphaned_runs(session: AsyncSession) -> int:
    """Mark any leftover ``result='running'`` run as ``interrupted`` (finished now); return the count.

    Called once on startup: a restarted process cannot still be running an operation, so a run
    still ``running`` was orphaned by a crash/kill mid-flight. Left alone it would freeze the live
    status badge at "in progress" forever and (via the concurrency guard) block a new operation on
    that collection. Reaping clears the stale indicator and unblocks the collection. The terminal state is
    ``interrupted`` (not ``error``) so a benign restart-induced interruption — e.g. a deploy killing
    a long scan mid-flight — is not conflated with a genuine scan failure. Like ``error``, an
    ``interrupted`` run does not refresh scan freshness (:func:`compute_health` keys on ``ok``/
    ``partial`` only).
    """
    result = await session.execute(
        update(Run)
        .where(Run.result == "running")
        .values(result="interrupted", finished=_utcnow())
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
        if await active_run(session, collection.id) is not None:
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

    For a collection that actually has incomplete proofs to process we open a typed ``kind='upgrade'``
    run whose ``total`` is the incomplete count (known up front → exact progress) and advance its
    ``processed`` as proofs are examined. Because :func:`compute_health` keys freshness on
    ``kind='scan'`` runs only, this ``upgrade`` run never refreshes the dead-man's switch — which is
    why we can finally record a real run instead of the old "amend the latest scan run" workaround.
    A collection with no incomplete proofs records nothing (no empty daily runs), and a collection that
    already has an operation in flight is skipped so we never start a second writer on it.
    """
    total = 0
    for collection in await list_collections(session):
        if await active_run(session, collection.id) is not None:
            log.info("skip upgrade for collection %s — operation already in progress", collection.id)
            continue
        incomplete = await session.scalar(
            select(func.count())
            .select_from(FileEntry)
            .where(FileEntry.collection_id == collection.id, FileEntry.ots_state == "incomplete")
        )
        if not incomplete:
            continue  # no work → no run row

        run = Run(
            collection_id=collection.id,
            kind="upgrade",
            started=_utcnow(),
            result="running",
            total=int(incomplete),
        )
        # Atomically claim the collection's single in-progress slot. The active_run pre-check above is
        # only advisory; this commit (guarded by the partial unique index) is the race-free claim,
        # so a manual scan/stamp that started in the same window can't run alongside this upgrade.
        if await claim_run(session, run) is None:
            log.info("skip upgrade for collection %s — operation already in progress", collection.id)
            continue

        async def _progress(done: int, _run: Run = run) -> None:
            _run.processed = done
            await session.commit()

        try:
            result = await proofs.upgrade_incomplete(session, collection, progress=_progress)
            run.upgraded = result.get("upgraded", 0)
            run.processed = int(incomplete)
            run.result = "ok"
        except Exception:
            log.exception("upgrade failed for collection %s (%s)", collection.id, collection.name)
            run.result = "error"
        run.finished = _utcnow()
        await session.commit()
        total += run.upgraded
    return total


# --- The loop -------------------------------------------------------------------------------


async def scheduler_loop(app: FastAPI, stop_event: asyncio.Event) -> None:
    """Run the background scan + upgrade loop until ``stop_event`` is set.

    On start: scan every collection once and run the upgrade pass (so a freshly-started instance
    clears any backlog). Then wake every ``scan_interval_seconds`` to scan due collections and, once
    ``upgrade_interval_seconds`` has elapsed, run the upgrade pass again. Each iteration is wrapped
    so a single error never kills the loop, and ``stop_event`` is awaited as the wait timeout so
    shutdown is prompt rather than waiting out a full tick.
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
                await run_due_scans(session, next_due, now)
                if now - last_upgrade >= settings.upgrade_interval_seconds:
                    await run_daily_upgrade(session)
                    last_upgrade = now
        except Exception:  # one bad iteration must never crash the loop
            log.exception("scheduler tick failed")

    log.info("scheduler loop stopped")
