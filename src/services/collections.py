"""Collection creation / lookup helpers.

Minimal for the scanner phase: create a collection owned by a user over an existing directory.
Root-jailing under an admin-provisioned base and per-user scoping arrive with multi-user mode.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_sessionmaker
from ..models.db import Collection, FileEntry, Run, utcnow

log = logging.getLogger("cairn.collections")

# How long an in-progress run may go without reporting progress before its claim is treated as
# abandoned and may be reclaimed (by the scheduler's reaper, or in-band by the next claim attempt --
# see :func:`claim_run`). It must comfortably exceed the gap between two progress writes of the
# slowest real operation (a scan batch of 500 files, a stamp batch, one upgraded proof) and stay
# short enough that a crash does not strand a collection for long. Fifteen minutes is both, by a
# wide margin. Lives here, next to the claim it governs, and is re-exported by ``scheduler``.
RUN_HEARTBEAT_TIMEOUT_SECONDS = 15 * 60


async def create_collection(
    session: AsyncSession,
    *,
    user_id: int,
    name: str,
    root: str,
    mode: str = "worm",
    ots_mode: str = "none",
    hash_cadence_seconds: int = 900,
    verify_cadence_seconds: int = 604800,
    auto_baseline_new: bool = False,
    exclude_globs: Iterable[str] | None = None,
    alert: dict | None = None,
) -> Collection:
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"Collection root does not exist or is not a directory: {resolved}"
        )
    collection = Collection(
        user_id=user_id,
        name=name,
        root=str(resolved),
        mode=mode,
        ots_mode=ots_mode,
        hash_cadence_seconds=hash_cadence_seconds,
        verify_cadence_seconds=verify_cadence_seconds,
        auto_baseline_new=auto_baseline_new,
        exclude_globs_json=json.dumps(list(exclude_globs or [])),
        alert_json=json.dumps(alert) if alert else "{}",
    )
    session.add(collection)
    await session.commit()
    await session.refresh(collection)
    return collection


async def list_collections(session: AsyncSession, user_id: int | None = None) -> list[Collection]:
    stmt = select(Collection).order_by(Collection.id)
    if user_id is not None:
        stmt = stmt.where(Collection.user_id == user_id)
    return list(await session.scalars(stmt))


async def active_run(session: AsyncSession, collection_id: int) -> Run | None:
    """Return the in-progress run (``result='running'``) for a collection, or ``None``.

    The single source of truth for "is an operation in progress for this collection?" — reused by the
    panel's concurrency guard (refuse a second scan/stamp), the scheduler (skip an in-flight
    collection), and the live operation-status badge. SQLite is single-writer, so at most one run is
    ``running`` at a time; the newest is returned if (defensively) more than one exists.
    """
    return await session.scalar(
        select(Run)
        .where(Run.collection_id == collection_id, Run.result == "running")
        .order_by(Run.started.desc())
        .limit(1)
    )


async def _stale_claim_id(
    session: AsyncSession, collection_id: int, cutoff: datetime
) -> int | None:
    """Id of this collection's ``running`` run whose last reported liveness predates ``cutoff``.

    Split out from :func:`reclaim_stale_claim` so the read and the guarded write are separately
    visible (and separately testable): between the two, a live process may heartbeat, and the write
    must lose that race.
    """
    return await session.scalar(
        select(Run.id).where(
            Run.collection_id == collection_id,
            Run.result == "running",
            func.coalesce(Run.heartbeat_at, Run.started) <= cutoff,
        )
    )


async def reclaim_stale_claim(collection_id: int) -> bool:
    """Reclaim this collection's operation slot if the claim holding it is abandoned. Returns success.

    An abandoned claim is a ``running`` run whose ``coalesce(heartbeat_at, started)`` is older than
    :data:`RUN_HEARTBEAT_TIMEOUT_SECONDS` — the same liveness test, and the same terminal state
    (``interrupted``, ``finished`` set), the scheduler's reaper uses. This is the *in-band* path:
    the reaper only runs where a scheduler (or a web startup) runs, so a claim orphaned by a
    SIGKILLed ``cairn stamp`` would otherwise wedge its collection until the service restarts, and
    forever in a CLI-only deployment.

    **The UPDATE is guarded, so a concurrent live heartbeat wins.** The read above and the write
    below are separate statements, so the holder may report progress in between — exactly the case
    where reclaiming would admit a second proof writer (design D10). Re-asserting the full stale
    condition (``result='running'`` AND liveness ``<= cutoff``) inside the UPDATE's WHERE makes the
    decision atomic with the write: a heartbeat that lands first fails the predicate, the UPDATE
    matches zero rows, and we report failure so the caller refuses. The claim is never taken from a
    process that is still working; the worst case is a delay of one abandonment interval.

    It runs in its **own session** so it cannot disturb the caller's: the panel routes and CLI
    commands that reach it through :func:`blocking_run` hold loaded ORM objects, and a rollback in
    their session would expire those objects into an async lazy refresh that raises. A datastore
    failure here is swallowed and reported as "not reclaimed" — refusing an operation is the
    behaviour that shipped before this path existed, while raising would break the scan or route
    that merely asked whether the collection was busy.
    """
    now = utcnow()
    cutoff = now - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS)
    try:
        async with get_sessionmaker()() as session:
            stale_id = await _stale_claim_id(session, collection_id, cutoff)
            if stale_id is None:
                return False
            result = await session.execute(
                update(Run)
                .where(
                    Run.id == stale_id,
                    Run.result == "running",
                    func.coalesce(Run.heartbeat_at, Run.started) <= cutoff,
                )
                .values(result="interrupted", finished=now)
            )
            await session.commit()
    except SQLAlchemyError:
        # Fail SAFE, not open: an unreclaimed claim is the behaviour that shipped before, while a
        # raised exception here would turn a routine refusal into a broken scan/route.
        log.warning(
            "collection %s: stale-claim reclamation failed — treating the claim as held",
            collection_id,
            exc_info=True,
        )
        return False
    if not result.rowcount:
        log.info(
            "collection %s: stale claim (run %s) heartbeated concurrently — not reclaimed",
            collection_id,
            stale_id,
        )
        return False
    log.warning(
        "collection %s: reclaimed abandoned claim (run %s marked interrupted)",
        collection_id,
        stale_id,
    )
    return True


async def blocking_run(session: AsyncSession, collection_id: int) -> Run | None:
    """The run that would block a new operation on this collection — after reclaiming an abandoned one.

    The gate in front of every operation (panel routes, ``cairn scan``, ``cairn upgrade``, the
    scheduler pass) is an advisory :func:`active_run` pre-check, and it answers *before*
    :func:`claim_run` is ever reached — so reclaiming only inside the claim would leave the gate
    reporting "already in progress" forever for a collection whose claim holder is long dead. The
    gates ask this instead: an in-progress run that has stopped heartbeating is reclaimed here and
    the collection reads free. :func:`active_run` stays the plain read for display surfaces (the
    status badge), which must not write.
    """
    run = await active_run(session, collection_id)
    if run is None:
        return None
    if not await reclaim_stale_claim(collection_id):
        return run
    return await active_run(session, collection_id)


async def claim_run(session: AsyncSession, run: Run) -> Run | None:
    """Atomically claim the single in-progress slot for a collection by committing ``run`` as ``running``.

    The partial unique index ``uq_runs_one_running_per_collection`` (``collection_id`` WHERE
    ``result='running'``) makes this the race-free counterpart to :func:`active_run`: a cheap
    ``active_run`` pre-check is only advisory, but committing the ``running`` row here is the actual
    claim. If a near-simultaneous op (a manual scan + a scheduler tick, or two POSTs) already holds
    the slot, the INSERT violates the index, the commit raises :class:`IntegrityError`, and we
    roll back — the caller must treat ``None`` as "already running" and abort. On success the
    committed run (visible to the badge/freshness immediately) is returned.

    The claim also stamps ``heartbeat_at``: the claim is a LEASE, and a lease with no liveness signal
    is indistinguishable from a corpse — a reaper would revoke a claim a live second process is
    still working under (design D10). Every long operation refreshes it as it progresses.

    **A blocked claim reconciles before it refuses.** A lease reaped only by a background reaper is
    a lease that never expires where no reaper runs: a SIGKILLed ``cairn stamp`` leaves a committed
    ``running`` row that satisfies the index forever, wedging every later scan/stamp/upgrade on that
    collection until the web service restarts — and permanently in a CLI-only deployment. So a
    blocked claim asks :func:`reclaim_stale_claim` whether the blocker is abandoned, and retries
    **once** if it was. A blocker that is still heartbeating is refused exactly as before: liveness
    decides, never impatience. One retry is enough — the retry either wins the freed slot or loses
    it to another claimant, and losing to a live claimant is the correct answer, not a reason to
    loop.
    """
    if await _attempt_claim(session, run) is not None:
        return run
    if not await reclaim_stale_claim(run.collection_id):
        return None
    return await _attempt_claim(session, run)


async def _attempt_claim(session: AsyncSession, run: Run) -> Run | None:
    """One INSERT of ``run`` as the collection's ``running`` claim; ``None`` if the slot is held."""
    run.heartbeat_at = utcnow()
    # A rolled-back INSERT leaves the ORM object transient again; clear any primary key the failed
    # flush assigned so the retry inserts a fresh row rather than re-proposing a taken id.
    run.id = None
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None
    return run


async def get_collection_by_name(
    session: AsyncSession, name: str, user_id: int | None = None
) -> Collection | None:
    stmt = select(Collection).where(Collection.name == name)
    if user_id is not None:
        stmt = stmt.where(Collection.user_id == user_id)
    return await session.scalar(stmt)


async def update_collection(
    session: AsyncSession,
    collection: Collection,
    *,
    name: str,
    root: str,
    mode: str,
    ots_mode: str,
    hash_cadence_seconds: int,
    verify_cadence_seconds: int | None = None,
    auto_baseline_new: bool | None = None,
    exclude_globs: Iterable[str] | None = None,
    alert: dict | None = None,
) -> Collection:
    """Update an existing collection in place. Re-validates the root path."""
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(
            f"Collection root does not exist or is not a directory: {resolved}"
        )
    collection.name = name
    collection.root = str(resolved)
    collection.mode = mode
    collection.ots_mode = ots_mode
    collection.hash_cadence_seconds = hash_cadence_seconds
    if verify_cadence_seconds is not None:
        collection.verify_cadence_seconds = verify_cadence_seconds
    if auto_baseline_new is not None:
        collection.auto_baseline_new = auto_baseline_new
    collection.exclude_globs_json = json.dumps(list(exclude_globs or []))
    if alert is not None:
        collection.alert_json = json.dumps(alert)
    await session.commit()
    await session.refresh(collection)
    return collection


@dataclass
class RootValidation:
    """Result of validating a candidate collection root path."""

    ok: bool
    resolved: str
    message: str = ""


def validate_root(path: str) -> RootValidation:
    """Validate a candidate root path for use as a collection root.

    Single-user mode: accept any path that resolves to an existing directory. (The
    admin-provisioned mounted base / jailing arrives with multi-user mode.) Returns a
    structured result so the panel can render the live-validation indicator and the server can
    re-validate on submit with the same logic.
    """
    if not path or not path.strip():
        return RootValidation(ok=False, resolved="", message="Enter a root path.")
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
        return RootValidation(ok=False, resolved=path, message=f"Invalid path: {exc}")
    if not resolved.exists():
        return RootValidation(
            ok=False, resolved=str(resolved), message="Path does not exist — rejected."
        )
    if not resolved.is_dir():
        return RootValidation(
            ok=False, resolved=str(resolved), message="Path is not a directory — rejected."
        )
    return RootValidation(ok=True, resolved=str(resolved), message="Path resolves to a directory.")


# Status sets the "Issues" filter resolves to (modified or missing files).
_ISSUE_STATUSES = ("modified", "missing")

# Sortable file columns: a stable query-param key -> ORM column. Whitelisted so the ORDER BY is
# injection-proof and the URL/query-param surface stays small and stable. Unknown keys fall back
# to the default below.
SORT_COLUMNS = {
    "path": FileEntry.relpath,
    "size": FileEntry.size,
    "modified": FileEntry.last_changed,
    "notarized": FileEntry.ots_stamped_at,
    "checked": FileEntry.last_checked,
}
# Newest-activity-first: the most recently changed files appear on load.
DEFAULT_SORT = "modified"
DEFAULT_DIRECTION = "desc"
# Nullable sort keys push NULLs last regardless of direction, so never-stamped / never-changed
# files don't dominate the top of a descending sort.
_NULLABLE_SORTS = {"modified", "notarized", "checked"}


def _escape_like(s: str) -> str:
    """Escape LIKE wildcards so a path prefix is matched literally (``\\`` is the escape char)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_prefix(prefix: str | None) -> str:
    """Canonicalise a tree prefix to ``""`` (root) or ``"a/b/"`` (trailing slash, no leading).

    Strips surrounding slashes and any ``..`` traversal segments (defence-in-depth — the prefix
    only ever filters the index, never touches the filesystem), then re-appends a trailing slash so
    it composes directly with a child segment.
    """
    if not prefix:
        return ""
    parts = [p for p in prefix.strip("/").split("/") if p and p != ".." and p != "."]
    return ("/".join(parts) + "/") if parts else ""


def _files_base_query(
    collection_id: int, q: str | None, status_filter: str, prefix: str | None = None
):
    """Build the shared WHERE clause for the paginated file query + count.

    When ``prefix`` is given the query is scoped to files **directly within** that directory level
    (an anchored ``LIKE prefix||'%'`` range-scan of the ``(collection_id, relpath)`` index, plus the
    remainder after the prefix containing no ``/`` — so subfolder contents are excluded). This is
    the tree view's per-folder file list; it pages exactly like the flat list.
    """
    stmt = select(FileEntry).where(FileEntry.collection_id == collection_id)
    # ``prefix is None`` = flat list (no tree scoping). ``prefix == ""`` = tree ROOT level: still
    # restrict to immediate files (no ``/`` in the path), just with an empty anchored prefix.
    if prefix is not None:
        if prefix:
            stmt = stmt.where(
                FileEntry.relpath.like(_escape_like(prefix) + "%", escape="\\")
            )
        stmt = stmt.where(
            func.instr(func.substr(FileEntry.relpath, len(prefix) + 1), "/") == 0
        )
    if q:
        stmt = stmt.where(FileEntry.relpath.like(f"%{_escape_like(q)}%", escape="\\"))
    if status_filter == "issues":
        stmt = stmt.where(FileEntry.status.in_(_ISSUE_STATUSES))
    elif status_filter in ("new", "ok", "modified", "missing"):
        stmt = stmt.where(FileEntry.status == status_filter)
    return stmt


async def query_files(
    session: AsyncSession,
    collection_id: int,
    *,
    q: str | None = None,
    status_filter: str = "all",
    prefix: str | None = None,
    page: int = 0,
    page_size: int = 50,
    sort: str = DEFAULT_SORT,
    direction: str = DEFAULT_DIRECTION,
) -> tuple[list[FileEntry], int]:
    """Server-side paginated/filtered/searched/sorted file query.

    Returns ``(rows, total)`` where ``rows`` is at most ``page_size`` entries (LIMIT/OFFSET) and
    ``total`` is the count matching the same filter. The full file set is never materialized — a
    collection may hold ~186k files.

    ``prefix`` (when set) scopes the query to files directly within that directory level (the tree
    view's per-folder file list — see :func:`_files_base_query`).

    ``sort`` is resolved through :data:`SORT_COLUMNS` (unknown -> :data:`DEFAULT_SORT`) and
    ``direction`` to asc/desc (unknown -> :data:`DEFAULT_DIRECTION`). ``relpath`` is always
    appended as a stable secondary key so LIMIT/OFFSET paging is deterministic across requests
    even when the primary key ties.
    """
    base = _files_base_query(collection_id, q, status_filter, prefix)
    total = await session.scalar(
        select(func.count()).select_from(base.subquery())
    )

    if sort not in SORT_COLUMNS:
        sort = DEFAULT_SORT
    if direction not in ("asc", "desc"):
        direction = DEFAULT_DIRECTION
    col = SORT_COLUMNS[sort]
    primary = col.asc() if direction == "asc" else col.desc()
    if sort in _NULLABLE_SORTS:
        primary = primary.nulls_last()

    order = [primary]
    if col is not FileEntry.relpath:
        order.append(FileEntry.relpath.asc())

    rows = list(
        await session.scalars(
            base.order_by(*order).limit(page_size).offset(page * page_size)
        )
    )
    return rows, int(total or 0)


@dataclass
class TreeFolder:
    """One immediate subfolder of a tree level: its name, full child prefix, and roll-ups."""

    name: str
    prefix: str  # the child level's prefix, e.g. "2024/jan/"
    file_count: int  # files anywhere beneath this folder
    issue_count: int  # of those, how many are modified/missing (drives the issue dot)


async def browse_tree(
    session: AsyncSession, collection_id: int, prefix: str = ""
) -> list[TreeFolder]:
    """Return the immediate subfolders of one directory level, derived from ``relpath`` in SQL.

    For files under ``prefix`` (``""`` = collection root, else ``"2024/jan/"``) whose remaining path
    *contains* a ``/``, the first segment names an immediate subfolder. We ``GROUP BY`` that
    segment to get each subfolder's recursive file count and an issue roll-up (how many beneath it
    are ``modified``/``missing``). The scan is a single anchored ``LIKE prefix||'%'`` range over the
    ``(collection_id, relpath)`` index with SQLite-side aggregation — the full file set is never
    materialized (a collection may hold ~186k files). Immediate files at this level (no ``/`` in the
    remainder) are fetched separately via the paginated :func:`query_files` with ``prefix=``.
    """
    prefix = normalize_prefix(prefix)
    plen = len(prefix)
    remainder = func.substr(FileEntry.relpath, plen + 1)
    slash_pos = func.instr(remainder, "/")
    segment = func.substr(remainder, 1, slash_pos - 1)
    issue = func.sum(
        case((FileEntry.status.in_(_ISSUE_STATUSES), 1), else_=0)
    )

    stmt = (
        select(segment.label("name"), func.count().label("n"), issue.label("issues"))
        .where(FileEntry.collection_id == collection_id)
        .where(slash_pos > 0)
        .group_by(segment)
        .order_by(segment)
    )
    if prefix:
        stmt = stmt.where(
            FileEntry.relpath.like(_escape_like(prefix) + "%", escape="\\")
        )

    rows = await session.execute(stmt)
    return [
        TreeFolder(
            name=name,
            prefix=f"{prefix}{name}/",
            file_count=int(n or 0),
            issue_count=int(issues or 0),
        )
        for name, n, issues in rows
    ]
