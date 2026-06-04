"""Collection creation / lookup helpers.

Minimal for the scanner phase: create a collection owned by a user over an existing directory.
Root-jailing under an admin-provisioned base and per-user scoping arrive with multi-user mode.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import Collection, FileEntry, Run


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


async def claim_run(session: AsyncSession, run: Run) -> Run | None:
    """Atomically claim the single in-progress slot for a collection by committing ``run`` as ``running``.

    The partial unique index ``uq_runs_one_running_per_collection`` (``collection_id`` WHERE
    ``result='running'``) makes this the race-free counterpart to :func:`active_run`: a cheap
    ``active_run`` pre-check is only advisory, but committing the ``running`` row here is the actual
    claim. If a near-simultaneous op (a manual scan + a scheduler tick, or two POSTs) already holds
    the slot, the INSERT violates the index, the commit raises :class:`IntegrityError`, and we
    roll back and return ``None`` — the caller must treat that as "already running" and abort. On
    success the committed run (visible to the badge/freshness immediately) is returned.
    """
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
