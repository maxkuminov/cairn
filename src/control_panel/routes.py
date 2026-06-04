"""Cairn control-panel routes: pages + htmx partial endpoints.

Server-rendered Jinja2 + htmx in the locked Slate design. Single-user mode resolves the implicit
user (``ensure_implicit_user``) and scopes every query by ``user_id`` so the same code becomes
multi-user-correct once login lands. Mutating endpoints are CSRF-protected. File search /
pagination is mandatory server-side (a collection can hold ~186k files).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..csrf import generate_csrf_token, verify_csrf
from ..database import get_session
from ..models.db import Collection, Event, FileEntry, Run, User
from ..services import app_settings as app_settings_svc
from ..services import collections as collections_svc
from ..services import proofs as proofs_svc
from ..services import scanner as scanner_svc

router = APIRouter(tags=["panel"])
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))


# --- legacy URL compatibility ----------------------------------------------------------------
# "Corpus" was renamed to "collection" (rename-corpus-to-collection). Old bookmarks and the
# Uptime-Kuma poll may still hit `/corpus/...` (and the old `/corpora` list); 308-redirect them to
# the new `/collection...` URLs so nothing breaks. 308 preserves the method and body. These match
# only the retired prefixes, so they never shadow a live route.
@router.api_route("/corpus", methods=["GET", "POST"], include_in_schema=False)
@router.api_route("/corpus/{rest:path}", methods=["GET", "POST"], include_in_schema=False)
async def _legacy_corpus_redirect(request: Request, rest: str = "") -> RedirectResponse:
    rest = rest.strip("/")
    # Bare `/corpus` (or `/corpus/`) maps to the collections list; a sub-path maps to its
    # `/collection/...` equivalent. Never emit a trailing-slash target (it would slash-redirect to
    # a bare `/collection`, which is not a route → 405).
    target = "/collections" if not rest else f"/collection/{rest}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=308)


@router.api_route("/corpora", methods=["GET", "POST"], include_in_schema=False)
async def _legacy_corpora_redirect() -> RedirectResponse:
    return RedirectResponse("/collections", status_code=308)

# Single source of truth for the file-list page size, shared by the collection-detail page and the
# htmx file-table endpoint (and the pager's "Page X of Y" math in the template).
PAGE_SIZE = 50

# --- cadence labels (seconds -> human) ------------------------------------------------------
CADENCE_OPTIONS = [
    ("300", "Every 5 minutes"),
    ("900", "Every 15 minutes"),
    ("3600", "Hourly"),
    ("86400", "Nightly"),
    ("604800", "Weekly"),
]
_CADENCE_LABELS = dict(CADENCE_OPTIONS)

# Deep-verify (full re-hash) cadence choices; 0 disables it for the collection.
VERIFY_CADENCE_OPTIONS = [
    ("0", "Disabled"),
    ("86400", "Nightly"),
    ("604800", "Weekly"),
    ("2592000", "Monthly"),
]


def _cadence_label(seconds: int) -> str:
    return _CADENCE_LABELS.get(str(seconds), f"Every {seconds}s")


# --- formatting helpers ---------------------------------------------------------------------


def humanize_size(num: int | None) -> str:
    n = float(num or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"  # pragma: no cover


def humanize_count(num: int | None) -> str:
    n = int(num or 0)
    if n > 9999:
        return f"{n // 1000}k"
    return f"{n:,}"


def humanize_delta(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min ago"
    if seconds < 86400:
        h = seconds // 3600
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = seconds // 86400
    return f"{d} day{'s' if d != 1 else ''} ago"


def humanize_date(dt: datetime | None) -> str | None:
    """Absolute calendar date, e.g. "30 May 2026" (no leading-zero day, portable). ``None`` passes
    through so the template can fall back to another timestamp."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.day} {dt.strftime('%b %Y')}"


# --- user / context -------------------------------------------------------------------------


async def current_user(session: AsyncSession = Depends(get_session)) -> User:
    """Resolve the implicit single user (scope anchor). Multi-user adds the login wall later."""
    user = await session.scalar(select(User).order_by(User.id).limit(1))
    if user is None:  # pragma: no cover - lifespan bootstraps this
        raise HTTPException(status_code=500, detail="no user provisioned")
    return user


def _mode(request: Request) -> str:
    return "dark" if request.cookies.get("cairn_mode") == "dark" else "light"


async def _collection_counts(session: AsyncSession, collection_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(FileEntry.status, func.count())
        .where(FileEntry.collection_id == collection_id)
        .group_by(FileEntry.status)
    )
    counts = {"ok": 0, "new": 0, "modified": 0, "missing": 0}
    for status, n in rows:
        counts[status] = n
    return counts


async def _ots_counts(session: AsyncSession, collection_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(FileEntry.ots_state, func.count())
        .where(FileEntry.collection_id == collection_id)
        .group_by(FileEntry.ots_state)
    )
    out = {"none": 0, "pending": 0, "incomplete": 0, "complete": 0}
    for state, n in rows:
        out[state] = n
    return out


def _collection_status(counts: dict[str, int]) -> str:
    # `new` (added) files are informational and born-acknowledged (streamline-event-acknowledgement):
    # a newly-tracked, unmodified file is the happy path, not something to warn about. Only an
    # alarming WORM `modified` raises "attention"; a `missing` file raises "alert". A collection whose
    # only non-ok files are `new` reads "All clear" (a scan never promotes new→ok; baseline via accept
    # to move them into the "Verified OK" count, but it is not required for the collection to be healthy).
    if counts["missing"] > 0:
        return "alert"
    if counts["modified"] > 0:
        return "attention"
    return "ok"


_STATUS_META = {
    "ok": ("All clear", "var(--ok)", "checkCircle", "ok"),
    "attention": ("Attention", "var(--warn)", "alert", "warn"),
    "alert": ("Alert", "var(--danger)", "alert", "danger"),
}

# --- live operation status (scan / stamp / upgrade) -----------------------------------------

_OP_LABELS = {"scan": "Scanning", "stamp": "Stamping", "upgrade": "Upgrading proofs"}


def _op_view(run: Run) -> dict[str, Any]:
    """Render a running :class:`Run` as the live-badge context (label, progress, elapsed).

    ``total`` set → an exact/estimated percentage and bar; for a ``scan`` the percentage is capped
    at 99 so it never reads "done" before the run finishes (the total is only an estimate). ``total``
    NULL → indeterminate (no percentage), showing the running count and elapsed time instead.
    """
    processed = run.processed or 0
    total = run.total
    pct = None
    if total and total > 0:
        raw = (100 * processed) // total
        pct = min(99, raw) if run.kind == "scan" else min(100, raw)
    return {
        "kind": run.kind,
        "label": _OP_LABELS.get(run.kind, "Working"),
        "processed_h": f"{processed:,}",
        "total_h": f"{total:,}" if total else None,
        "pct": pct,
        "deep": bool(run.deep),
        "started": humanize_delta(run.started),
    }


async def _op_status_c(session: AsyncSession, collection: Collection) -> dict[str, Any]:
    """Build the ``c`` context for ``partials/op_status.html``: resting status + any running op."""
    counts = await _collection_counts(session, collection.id)
    meta = _STATUS_META[_collection_status(counts)]
    run = await collections_svc.active_run(session, collection.id)
    return {
        "id": collection.id,
        "status_kind": meta[3],
        "status_icon": meta[2],
        "status_label": meta[0],
        "op": _op_view(run) if run else None,
    }


# Background operation tasks: a manual scan / stamp-all runs in its own session off the request, so
# the panel returns immediately and the live badge polls. The module-level set keeps a strong
# reference so the task is not garbage-collected mid-flight (asyncio holds only weak refs).
_BG_TASKS: set[asyncio.Task[Any]] = set()

OperationFn = Callable[[AsyncSession, Collection], Awaitable[Any]]


async def _run_operation(collection_id: int, op: OperationFn) -> None:
    """Run ``op(session, collection)`` in a fresh session; swallow + log errors (no request to surface)."""
    from ..database import get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            collection = await session.get(Collection, collection_id)
            if collection is not None:
                await op(session, collection)
    except Exception:  # pragma: no cover - defensive; the run row records the error
        logging.getLogger("cairn.panel").exception(
            "background operation failed for collection %s", collection_id
        )


def _launch_operation(collection_id: int, op: OperationFn) -> None:
    """Fire a background operation and retain a reference until it completes."""
    task = asyncio.create_task(_run_operation(collection_id, op))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _collection_view(session: AsyncSession, collection: Collection) -> dict[str, Any]:
    counts = await _collection_counts(session, collection.id)
    ots = await _ots_counts(session, collection.id)
    total_files = sum(counts.values())
    total_size = await session.scalar(
        select(func.coalesce(func.sum(FileEntry.size), 0)).where(
            FileEntry.collection_id == collection.id
        )
    )
    last_run = await session.scalar(
        select(Run)
        .where(
            Run.collection_id == collection.id,
            Run.kind == "scan",
            Run.result.in_(("ok", "partial")),
        )
        .order_by(Run.finished.desc().nulls_last())
        .limit(1)
    )
    status = _collection_status(counts)
    meta = _STATUS_META[status]
    excludes = json.loads(collection.exclude_globs_json or "[]")
    active = await collections_svc.active_run(session, collection.id)
    return {
        "id": collection.id,
        "name": collection.name,
        "op": _op_view(active) if active else None,
        "root": collection.root,
        "mode": collection.mode,
        "ots": collection.ots_mode,
        "cadence": _cadence_label(collection.hash_cadence_seconds),
        "cadence_seconds": collection.hash_cadence_seconds,
        "excludes": excludes,
        "owner": collection.owner.username if collection.owner else "—",
        "counts": counts,
        "ots_counts": ots,
        "file_count": total_files,
        "file_count_h": humanize_count(total_files),
        "size_bytes": int(total_size or 0),
        "size": humanize_size(total_size),
        "status": status,
        "status_label": meta[0],
        "status_color": meta[1],
        "status_icon": meta[2],
        "status_kind": meta[3],
        "issues": counts["modified"] + counts["missing"],
        "last_scan": humanize_delta(last_run.finished) if last_run else "never",
        "last_scan_full": (
            last_run.finished.strftime("%Y-%m-%d %H:%M UTC") if last_run and last_run.finished
            else "no completed scans yet"
        ),
    }


async def _base_context(
    request: Request, session: AsyncSession, user: User, page: str
) -> dict[str, Any]:
    """Shell context: sidebar collections, alert badge, user block, mode, CSRF token."""
    collections = await collections_svc.list_collections(session, user_id=user.id)
    sidebar = []
    total_missing = 0
    for c in collections:
        counts = await _collection_counts(session, c.id)
        total_missing += counts["missing"]
        status = _collection_status(counts)
        sidebar.append(
            {
                "id": c.id,
                "name": c.name,
                "dot_color": _STATUS_META[status][1],
                "is_alert": status == "alert",
                "file_count_h": humanize_count(sum(counts.values())),
            }
        )
    return {
        "page": page,
        "mode": _mode(request),
        "username": user.username,
        "is_admin": user.is_admin,
        "user_email": f"{user.username}@localhost",
        "sidebar_collections": sidebar,
        "alert_count": total_missing,
        "csrf_token": generate_csrf_token(request),
    }


# --- mode toggle ----------------------------------------------------------------------------


@router.get("/mode/toggle")
async def mode_toggle(request: Request):
    current = _mode(request)
    new = "light" if current == "dark" else "dark"
    target = request.headers.get("referer") or "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("cairn_mode", new, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


# --- health pill (htmx poll) ----------------------------------------------------------------


@router.get("/health-pill", response_class=HTMLResponse)
async def health_pill(request: Request, session: AsyncSession = Depends(get_session)):
    from ..services.scheduler import compute_health

    settings = get_settings()
    report = await compute_health(session, settings)
    return templates.TemplateResponse(
        request, "partials/health_pill.html", {"status": report.status}
    )


# --- dashboard ------------------------------------------------------------------------------


async def _event_view(session: AsyncSession, event: Event) -> dict[str, Any]:
    relpath = "—"
    if event.file_id is not None:
        fe = await session.get(FileEntry, event.file_id)
        if fe is not None:
            relpath = fe.relpath
    collection = await session.get(Collection, event.collection_id)
    stamped = False
    if event.file_id is not None:
        fe = await session.get(FileEntry, event.file_id)
        stamped = bool(fe and fe.ots_state in ("incomplete", "complete"))
    return {
        "id": event.id,
        "kind": event.kind,
        "relpath": relpath,
        # Free-text context (set for `moved` events: "old → new path").
        "detail": event.detail,
        "collection_name": collection.name if collection else "—",
        "at": humanize_delta(event.detected_at),
        "acked": event.acknowledged_at is not None,
        "stamped": stamped,
    }


async def _event_feed(session: AsyncSession, collection_ids: list[int]) -> dict[str, Any]:
    """Recent-events feed + live counts for the dashboard and its htmx refreshes.

    ``open_events`` (the "need action" pill) and ``alert_count`` (the sidebar badge) are real
    COUNT queries over ALL of the user's events/files, not just the 20 rendered rows, so both stay
    accurate past the feed cap. Auto-acknowledged ``added``/``restored`` events render in the feed
    but never count toward ``open_events``.
    """
    events: list[Event] = []
    open_events = 0
    alert_count = 0
    if collection_ids:
        events = list(
            await session.scalars(
                select(Event)
                .where(Event.collection_id.in_(collection_ids))
                .order_by(Event.detected_at.desc())
                .limit(20)
            )
        )
        open_events = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.collection_id.in_(collection_ids), Event.acknowledged_at.is_(None))
        )
        alert_count = await session.scalar(
            select(func.count())
            .select_from(FileEntry)
            .where(FileEntry.collection_id.in_(collection_ids), FileEntry.status == "missing")
        )
    return {
        "events": [await _event_view(session, e) for e in events],
        "open_events": int(open_events or 0),
        "alert_count": int(alert_count or 0),
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    collections = await collections_svc.list_collections(session, user_id=user.id)
    views = [await _collection_view(session, c) for c in collections]

    total_files = sum(v["file_count"] for v in views)
    total_size = sum(v["size_bytes"] for v in views)
    total_missing = sum(v["counts"]["missing"] for v in views)
    total_modified = sum(v["counts"]["modified"] for v in views)
    total_anchored = sum(v["ots_counts"]["complete"] for v in views)
    total_pending = sum(
        v["ots_counts"]["pending"] + v["ots_counts"]["incomplete"] for v in views
    )

    collection_ids = [c.id for c in collections]
    feed = await _event_feed(session, collection_ids)
    event_views = feed["events"]
    open_events = feed["open_events"]

    last_run = None
    if collection_ids:
        last_run = await session.scalar(
            select(Run)
            .where(Run.collection_id.in_(collection_ids), Run.finished.is_not(None))
            .order_by(Run.finished.desc())
            .limit(1)
        )
    last_collection = ""
    last_activity_sub = "no scans yet"
    if last_run is not None:
        c = await session.get(Collection, last_run.collection_id)
        last_collection = c.name if c else ""
        last_activity_sub = f"{last_collection} scan" if last_collection else "last scan"
        if last_run.moved:
            last_activity_sub += f" · {last_run.moved} moved"

    ctx = await _base_context(request, session, user, "dashboard")
    ctx.update(
        {
            "collections": views,
            "events": event_views,
            "open_events": open_events,
            "tiles": {
                "files": humanize_count(total_files),
                "files_sub": f"{len(views)} collections · {humanize_size(total_size)}",
                "issues": total_missing + total_modified,
                "issues_sub": f"{total_missing} missing · {total_modified} modified",
                "issues_color": "var(--danger)"
                if (total_missing + total_modified) > 0
                else "var(--ok)",
                "anchored": humanize_count(total_anchored),
                "anchored_sub": (
                    f"{total_pending} pending confirmation"
                    if total_pending else "all confirmed"
                ),
                "last_activity": humanize_delta(last_run.finished) if last_run else "—",
                "last_activity_sub": last_activity_sub,
            },
        }
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.post("/events/{event_id}/ack", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def ack_event(
    event_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    view: str = Query("dashboard"),
):
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    collection = await session.get(Collection, event.collection_id)
    if collection is None or collection.user_id != user.id:
        raise HTTPException(status_code=404, detail="event not found")
    if event.acknowledged_at is None:
        event.acknowledged_at = datetime.now(timezone.utc)
        event.acknowledged_by = user.id
        await session.commit()

    # Recompute the global open-count + missing badge for the OOB swaps shared by both views.
    collections = await collections_svc.list_collections(session, user_id=user.id)
    collection_ids = [c.id for c in collections]
    open_events = 0
    total_missing = 0
    if collection_ids:
        open_events = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.collection_id.in_(collection_ids), Event.acknowledged_at.is_(None))
        )
        total_missing = await session.scalar(
            select(func.count())
            .select_from(FileEntry)
            .where(FileEntry.collection_id.in_(collection_ids), FileEntry.status == "missing")
        )

    if view == "review":
        # Acknowledged from the review page: swap that row in place and refresh the collection's
        # "need action" pill (#review-open-pill) plus the global sidebar badge.
        fe = await session.get(FileEntry, event.file_id) if event.file_id else None
        item = _review_item(fe, collection.root, event) if fe is not None else None
        review_open = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.collection_id == collection.id, Event.acknowledged_at.is_(None))
        )
        return templates.TemplateResponse(
            request,
            "partials/review_ack_row.html",
            {
                "it": item,
                "review_open": int(review_open or 0),
                "alert_count": int(total_missing or 0),
            },
        )

    view_ctx = await _event_view(session, event)
    return templates.TemplateResponse(
        request,
        "partials/event_ack.html",
        {"e": view_ctx, "open_events": int(open_events or 0), "alert_count": int(total_missing or 0)},
    )


@router.post("/events/ack-all", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def ack_all_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Bulk-acknowledge every open event in the current user's collections, then re-render the feed.

    Ack-only (sets ``acknowledged_at``/``by``) — it never re-baselines files; that stays with
    ``accept``. Scoped by the user's collection ids so it can never touch another user's events.
    """
    collections = await collections_svc.list_collections(session, user_id=user.id)
    collection_ids = [c.id for c in collections]
    if collection_ids:
        await session.execute(
            update(Event)
            .where(Event.collection_id.in_(collection_ids), Event.acknowledged_at.is_(None))
            .values(acknowledged_at=datetime.now(timezone.utc), acknowledged_by=user.id)
        )
        await session.commit()

    feed = await _event_feed(session, collection_ids)
    return templates.TemplateResponse(
        request, "partials/events_feed.html", {**feed, "user": user}
    )


@router.get("/collections", response_class=HTMLResponse)
async def collections_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Dedicated collections list page (the left-nav 'Collections' target)."""
    collections = await collections_svc.list_collections(session, user_id=user.id)
    views = [await _collection_view(session, c) for c in collections]
    ctx = await _base_context(request, session, user, "collections")
    ctx["collections"] = views
    return templates.TemplateResponse(request, "collections.html", ctx)


@router.post("/scan", dependencies=[Depends(verify_csrf)])
async def scan_all(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Out-of-cadence scan of all the user's collections, then back to the dashboard.

    Each collection is launched as its own background operation (mirroring :func:`collection_scan`) so the
    request returns immediately and the live badges poll — scanning the whole fleet inline would
    block the request for minutes and time out.
    """
    collections = await collections_svc.list_collections(session, user_id=user.id)
    for c in collections:
        # Honour the single-writer guard: never start a second writer on a collection that already has
        # an operation (a manual background scan/stamp or a scheduler pass) in flight.
        if await collections_svc.active_run(session, c.id) is not None:
            continue
        _launch_operation(c.id, lambda s, cps: scanner_svc.scan_collection(s, cps))
    return RedirectResponse("/", status_code=303)


# --- collection detail --------------------------------------------------------------------------


async def _get_owned_collection(session: AsyncSession, collection_id: int, user: User) -> Collection:
    collection = await session.get(Collection, collection_id)
    if collection is None or collection.user_id != user.id:
        raise HTTPException(status_code=404, detail="collection not found")
    return collection


def _file_view(fe: FileEntry) -> dict[str, Any]:
    return {
        "id": fe.id,
        "relpath": fe.relpath,
        "name": fe.relpath.rsplit("/", 1)[-1],
        "size": humanize_size(fe.size),
        "status": fe.status,
        "ots": fe.ots_state,
        "checked": humanize_delta(fe.last_checked),
        # Absolute dates for the prominent timestamp column; the template falls back from the
        # notarization date to the last-changed date so no row is ever dateless.
        "notarized_at": humanize_date(fe.ots_stamped_at),
        "modified_at": humanize_date(fe.last_changed),
    }


def _collection_form_ctx(existing: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "existing": existing,
        "cadence_options": CADENCE_OPTIONS,
        "verify_cadence_options": VERIFY_CADENCE_OPTIONS,
        "default_excludes": "**/.thumbnails/**\n**/*.tmp",
    }


# Literal `/collection/*` GET routes MUST be declared before `/collection/{collection_id}` so paths like
# `new` and `validate-root` are not parsed as an integer collection id (would 422).
@router.get("/collection/new", response_class=HTMLResponse)
async def collection_new(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    ctx = await _base_context(request, session, user, "addCollection")
    ctx.update(_collection_form_ctx(None))
    return templates.TemplateResponse(request, "collection_form.html", ctx)


@router.get("/collection/validate-root", response_class=HTMLResponse)
async def validate_root(request: Request, path: str = Query("")):
    result = collections_svc.validate_root(path)
    return templates.TemplateResponse(
        request, "partials/root_validation.html", {"r": result, "has_value": bool(path.strip())}
    )


@router.get("/collection/{collection_id}", response_class=HTMLResponse)
async def collection_detail(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    collection = await _get_owned_collection(session, collection_id, user)
    view = await _collection_view(session, collection)
    rows, total = await collections_svc.query_files(
        session,
        collection_id,
        page=0,
        page_size=PAGE_SIZE,
        sort=collections_svc.DEFAULT_SORT,
        direction=collections_svc.DEFAULT_DIRECTION,
    )
    # Render the tree root (default view) server-side so the page needs no extra request on load.
    tree_folders = await collections_svc.browse_tree(session, collection_id, "")
    tree_rows, tree_total = await collections_svc.query_files(
        session,
        collection_id,
        prefix="",
        page=0,
        page_size=PAGE_SIZE,
        sort=collections_svc.DEFAULT_SORT,
        direction=collections_svc.DEFAULT_DIRECTION,
    )
    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": view,
            "files": [_file_view(f) for f in rows],
            "files_total": total,
            "files_shown": len(rows),
            "q": "",
            "filter": "all",
            "page": 0,
            "page_size": PAGE_SIZE,
            "sort": collections_svc.DEFAULT_SORT,
            "dir": collections_svc.DEFAULT_DIRECTION,
        }
    )
    ctx.update(_tree_ctx(collection, tree_folders, tree_rows, tree_total, "", 0))
    return templates.TemplateResponse(request, "collection_detail.html", ctx)


@router.get("/collection/{collection_id}/files", response_class=HTMLResponse)
async def collection_files(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    q: str = Query(""),
    filter: str = Query("all"),
    page: int = Query(0, ge=0),
    sort: str = Query(collections_svc.DEFAULT_SORT),
    dir: str = Query(collections_svc.DEFAULT_DIRECTION),
):
    collection = await _get_owned_collection(session, collection_id, user)
    rows, total = await collections_svc.query_files(
        session,
        collection_id,
        q=q or None,
        status_filter=filter,
        page=page,
        page_size=PAGE_SIZE,
        sort=sort,
        direction=dir,
    )
    # Echo back the resolved sort/dir (query_files falls unknown values back to the default) so the
    # header carets and the pager/search/filter triggers all stay in sync.
    sort = sort if sort in collections_svc.SORT_COLUMNS else collections_svc.DEFAULT_SORT
    dir = dir if dir in ("asc", "desc") else collections_svc.DEFAULT_DIRECTION
    ctx = {
        "c": {"id": collection.id, "ots": collection.ots_mode, "file_count": 0},
        "files": [_file_view(f) for f in rows],
        "files_total": total,
        "files_shown": len(rows),
        "q": q,
        "filter": filter,
        "page": page,
        "page_size": PAGE_SIZE,
        "sort": sort,
        "dir": dir,
        "csrf_token": generate_csrf_token(request),
    }
    # Pull the collection's full file count for the "Showing N of TOTAL" baseline (no filter).
    full_total = await session.scalar(
        select(func.count()).select_from(FileEntry).where(FileEntry.collection_id == collection_id)
    )
    ctx["c"]["file_count"] = int(full_total or 0)
    return templates.TemplateResponse(request, "partials/file_table.html", ctx)


def _tree_ctx(
    collection: Collection,
    folders: list[collections_svc.TreeFolder],
    rows: list[FileEntry],
    total: int,
    prefix: str,
    page: int,
) -> dict[str, Any]:
    """Context for ``partials/file_tree.html`` — one directory level (subfolders + immediate files).

    Tree-specific keys are namespaced (``tree_*``) so the partial can be rendered alongside the flat
    list's ``files``/``page`` context on the same collection page without clobbering it. The caller
    supplies ``c`` (the full collection view on the page, or a minimal ``{id, ots}`` for the endpoint).
    """
    return {
        "has_ots": collection.ots_mode != "none",
        "tree_folders": [
            {
                "name": f.name,
                "prefix": f.prefix,
                "file_count_h": humanize_count(f.file_count),
                "issues": f.issue_count,
            }
            for f in folders
        ],
        "tree_files": [_file_view(r) for r in rows],
        "tree_prefix": prefix,
        "tree_total": total,
        "tree_shown": len(rows),
        "tree_page": page,
        "page_size": PAGE_SIZE,
    }


@router.get("/collection/{collection_id}/tree", response_class=HTMLResponse)
async def collection_tree(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    prefix: str = Query(""),
    page: int = Query(0, ge=0),
):
    """One directory level of the folder tree: immediate subfolders + directly-contained files.

    Lazy expand — each call fetches exactly one level (subfolders never pre-expand). Files at a
    level page exactly like the flat list (reusing ``query_files`` scoped to the prefix).
    """
    collection = await _get_owned_collection(session, collection_id, user)
    norm = collections_svc.normalize_prefix(prefix)
    folders = await collections_svc.browse_tree(session, collection_id, norm)
    rows, total = await collections_svc.query_files(
        session,
        collection_id,
        prefix=norm,
        page=page,
        page_size=PAGE_SIZE,
        sort=collections_svc.DEFAULT_SORT,
        direction=collections_svc.DEFAULT_DIRECTION,
    )
    ctx = {"c": {"id": collection.id, "ots": collection.ots_mode}, **_tree_ctx(collection, folders, rows, total, norm, page)}
    return templates.TemplateResponse(request, "partials/file_tree.html", ctx)


@router.get("/collection/{collection_id}/op-status", response_class=HTMLResponse)
async def collection_op_status(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    was_running: int = Query(0),
):
    """Poll target for the live operation badge (scan/stamp/upgrade) on the collection + dashboard.

    Returns the in-progress badge (which carries the poll trigger) while a run is in flight, and the
    resting status pill (no trigger → polling stops) once it finishes. We send ``HX-Refresh`` so the
    page resolves its stat row + file view to the final state — but **only** on a running→idle
    transition (``was_running`` set by the running badge's poll URL). A freshly-launched op may not
    have committed its ``running`` row before the first 4s poll; refreshing then would reload the
    page and cancel polling, dropping the in-flight badge — so the first poll of a just-started op
    (``was_running`` unset) never refreshes, it just keeps polling.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    c = await _op_status_c(session, collection)
    response = templates.TemplateResponse(request, "partials/op_status.html", {"c": c})
    if c["op"] is None and was_running:
        response.headers["HX-Refresh"] = "true"
    return response


@router.post("/collection/{collection_id}/scan", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def collection_scan(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Start an integrity scan in the background and return the live status badge immediately.

    Refuses to start a second operation while one is already running for this collection (SQLite is
    single-writer); in that case it just re-renders the current in-progress badge.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    if await collections_svc.active_run(session, collection_id) is not None:
        c = await _op_status_c(session, collection)
        return templates.TemplateResponse(
            request, "partials/op_status.html", {"c": c, "already_running": True}
        )
    _launch_operation(collection_id, lambda s, cps: scanner_svc.scan_collection(s, cps))
    c = await _op_status_c(session, collection)
    return templates.TemplateResponse(
        request, "partials/op_status.html", {"c": c, "just_started": True}
    )


@router.post("/collection/{collection_id}/accept", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def collection_accept(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    collection = await _get_owned_collection(session, collection_id, user)
    await scanner_svc.accept_collection(session, collection, user.id)
    return RedirectResponse(f"/collection/{collection_id}", status_code=303)


# --- issue review + recovery ----------------------------------------------------------------
# The focused "what happened to my files, and what do I do now" view. Reuses the existing file
# query + acknowledge/accept services; the only new surface is the read-side review item and a
# couple of collection-scoped action routes that land back on the review page.

# Bound the work the page does for a collection with a very large issue set (e.g. a whole deleted
# folder): render at most this many detailed rows, and copy at most this many paths into the
# recovery clipboard (with a "+N more" note). The accurate full count still comes from the counts.
REVIEW_ROW_LIMIT = 500
REVIEW_COPY_LIMIT = 2000


def _review_item(fe: FileEntry, root: str, event: Event | None) -> dict[str, Any]:
    """One review row: the file, what happened to it, and the open event (if any) to acknowledge."""
    rel_dir = fe.relpath.rsplit("/", 1)[0] if "/" in fe.relpath else ""
    open_event = event if (event is not None and event.acknowledged_at is None) else None
    detected_src = event.detected_at if event is not None else fe.last_changed
    return {
        "id": fe.id,
        "relpath": fe.relpath,
        "name": fe.relpath.rsplit("/", 1)[-1],
        "dir": rel_dir,
        "abs_path": str(Path(root) / fe.relpath),
        "status": fe.status,
        "size": humanize_size(fe.size),
        "last_seen": humanize_delta(fe.last_checked),
        "last_seen_full": (
            fe.last_checked.strftime("%Y-%m-%d %H:%M UTC") if fe.last_checked else "unknown"
        ),
        "detected": humanize_delta(detected_src),
        "notarized": fe.ots_state in ("incomplete", "complete"),
        "event_id": open_event.id if open_event else None,
        "acked": open_event is None,
    }


async def _latest_events_by_file(
    session: AsyncSession, collection_id: int, file_ids: list[int]
) -> dict[int, Event]:
    """Map each file id to its most recent event (the open one, if any, drives Acknowledge)."""
    out: dict[int, Event] = {}
    if not file_ids:
        return out
    rows = await session.scalars(
        select(Event)
        .where(Event.collection_id == collection_id, Event.file_id.in_(file_ids))
        .order_by(Event.detected_at.desc())
    )
    for e in rows:
        out.setdefault(e.file_id, e)  # first seen = latest by detected_at
    return out


@router.get("/collection/{collection_id}/review", response_class=HTMLResponse)
async def collection_review(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Focused review of a collection's missing + modified files, with recovery guidance."""
    collection = await _get_owned_collection(session, collection_id, user)
    view = await _collection_view(session, collection)

    files = list(
        await session.scalars(
            select(FileEntry)
            .where(
                FileEntry.collection_id == collection_id,
                FileEntry.status.in_(("missing", "modified")),
            )
            # missing first, then modified; stable by path within each.
            .order_by(case((FileEntry.status == "missing", 0), else_=1), FileEntry.relpath)
            .limit(REVIEW_ROW_LIMIT)
        )
    )
    events = await _latest_events_by_file(session, collection_id, [f.id for f in files])
    items = [_review_item(f, collection.root, events.get(f.id)) for f in files]

    copy_relpaths = list(
        await session.scalars(
            select(FileEntry.relpath)
            .where(
                FileEntry.collection_id == collection_id,
                FileEntry.status.in_(("missing", "modified")),
            )
            .order_by(FileEntry.relpath)
            .limit(REVIEW_COPY_LIMIT)
        )
    )
    review_open = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.collection_id == collection_id, Event.acknowledged_at.is_(None))
    )

    total_issues = view["issues"]
    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": view,
            "items": items,
            "total_issues": total_issues,
            "shown": len(items),
            "truncated": total_issues > len(items),
            "root": collection.root,
            "copy_relpaths": "\n".join(copy_relpaths),
            "copy_count": len(copy_relpaths),
            "copy_truncated": total_issues > len(copy_relpaths),
            "review_open": int(review_open or 0),
        }
    )
    return templates.TemplateResponse(request, "collection_review.html", ctx)


@router.post(
    "/collection/{collection_id}/review/accept",
    dependencies=[Depends(verify_csrf)],
)
async def collection_review_accept(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Re-baseline from the review page (new/modified → ok, missing removed), stay on review."""
    collection = await _get_owned_collection(session, collection_id, user)
    await scanner_svc.accept_collection(session, collection, user.id)
    return RedirectResponse(f"/collection/{collection_id}/review", status_code=303)


@router.post(
    "/collection/{collection_id}/review/ack-all",
    dependencies=[Depends(verify_csrf)],
)
async def collection_review_ack_all(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Acknowledge every open event in THIS collection (ack-only, no re-baseline), stay on review."""
    collection = await _get_owned_collection(session, collection_id, user)
    await session.execute(
        update(Event)
        .where(Event.collection_id == collection.id, Event.acknowledged_at.is_(None))
        .values(acknowledged_at=datetime.now(timezone.utc), acknowledged_by=user.id)
    )
    await session.commit()
    return RedirectResponse(f"/collection/{collection_id}/review", status_code=303)


@router.post("/collection/{collection_id}/stamp-all", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def collection_stamp_all(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """On-demand backfill: stamp every currently-unstamped file in this (perfile) collection.

    Owner/admin-scoped via :func:`_get_owned_collection`. Runs the backfill **asynchronously** as a
    typed ``kind='stamp'`` run (:func:`proofs.run_stamp_backfill`) and returns the live status badge
    immediately. Refuses a second operation while one is already running for this collection.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    if collection.ots_mode != "perfile":
        raise HTTPException(status_code=400, detail="stamp-all is only for per-file collections")
    if await collections_svc.active_run(session, collection_id) is not None:
        c = await _op_status_c(session, collection)
        return templates.TemplateResponse(
            request, "partials/op_status.html", {"c": c, "already_running": True}
        )
    _launch_operation(collection_id, lambda s, cps: proofs_svc.run_stamp_backfill(s, cps))
    c = await _op_status_c(session, collection)
    return templates.TemplateResponse(
        request, "partials/op_status.html", {"c": c, "just_started": True}
    )


# --- add / edit collection ----------------------------------------------------------------------


@router.get("/collection/{collection_id}/edit", response_class=HTMLResponse)
async def collection_edit(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    collection = await _get_owned_collection(session, collection_id, user)
    alert = json.loads(collection.alert_json or "{}")
    email_cfg = alert.get("email", {}) if isinstance(alert, dict) else {}
    existing = {
        "id": collection.id,
        "name": collection.name,
        "root": collection.root,
        "mode": collection.mode,
        "ots": collection.ots_mode,
        "cadence_seconds": str(collection.hash_cadence_seconds),
        "verify_cadence_seconds": str(collection.verify_cadence_seconds),
        "auto_baseline_new": collection.auto_baseline_new,
        "excludes": "\n".join(json.loads(collection.exclude_globs_json or "[]")),
        "email_enabled": bool(email_cfg.get("enabled")),
        "email_to": ", ".join(email_cfg.get("to") or []),
    }
    ctx = await _base_context(request, session, user, "addCollection")
    ctx.update(_collection_form_ctx(existing))
    return templates.TemplateResponse(request, "collection_form.html", ctx)


def _parse_excludes(raw: str) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _safe_cadence(raw: str, default: int) -> int:
    """Parse a cadence form value to a non-negative int, falling back to ``default``."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _build_alert(email_enabled: bool, email_to: str) -> dict[str, Any]:
    """Translate the form's Email toggle + recipient into the collection alert_json shape.

    Only the implemented email channel is persisted; the planned channels stay shown-disabled.
    """
    recipients = [a.strip() for a in (email_to or "").split(",") if a.strip()]
    return {"email": {"enabled": bool(email_enabled) and bool(recipients), "to": recipients}}


@router.post("/collection", dependencies=[Depends(verify_csrf)])
async def collection_create(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    name: str = Form(...),
    root: str = Form(...),
    mode: str = Form("worm"),
    ots: str = Form("perfile"),
    cadence: str = Form("86400"),
    verify_cadence: str = Form("604800"),
    auto_baseline: str = Form("off"),
    excludes: str = Form(""),
    email_enabled: bool = Form(False),
    email_to: str = Form(""),
):
    validation = collections_svc.validate_root(root)
    if not name.strip() or not validation.ok:
        raise HTTPException(status_code=400, detail="invalid collection name or root")
    collection = await collections_svc.create_collection(
        session,
        user_id=user.id,
        name=name.strip(),
        root=root,
        mode=mode if mode in ("worm", "churn") else "worm",
        ots_mode=ots if ots in ("none", "perfile") else "none",
        hash_cadence_seconds=_safe_cadence(cadence, 86400),
        verify_cadence_seconds=_safe_cadence(verify_cadence, 604800),
        auto_baseline_new=(auto_baseline == "on"),
        exclude_globs=_parse_excludes(excludes),
        alert=_build_alert(email_enabled, email_to),
    )
    return RedirectResponse(f"/collection/{collection.id}", status_code=303)


@router.post("/collection/{collection_id}", dependencies=[Depends(verify_csrf)])
async def collection_update(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    name: str = Form(...),
    root: str = Form(...),
    mode: str = Form("worm"),
    ots: str = Form("perfile"),
    cadence: str = Form("86400"),
    verify_cadence: str = Form("604800"),
    auto_baseline: str = Form("off"),
    excludes: str = Form(""),
    email_enabled: bool = Form(False),
    email_to: str = Form(""),
):
    collection = await _get_owned_collection(session, collection_id, user)
    validation = collections_svc.validate_root(root)
    if not name.strip() or not validation.ok:
        raise HTTPException(status_code=400, detail="invalid collection name or root")
    await collections_svc.update_collection(
        session,
        collection,
        name=name.strip(),
        root=root,
        mode=mode if mode in ("worm", "churn") else "worm",
        ots_mode=ots if ots in ("none", "perfile") else "none",
        hash_cadence_seconds=_safe_cadence(cadence, 86400),
        verify_cadence_seconds=_safe_cadence(verify_cadence, 604800),
        auto_baseline_new=(auto_baseline == "on"),
        exclude_globs=_parse_excludes(excludes),
        alert=_build_alert(email_enabled, email_to),
    )
    return RedirectResponse(f"/collection/{collection_id}", status_code=303)


# --- verify ---------------------------------------------------------------------------------


async def _anchored_query(session: AsyncSession, user: User, q: str | None, limit: int):
    stmt = (
        select(FileEntry, Collection)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(
            Collection.user_id == user.id,
            FileEntry.ots_state.in_(("incomplete", "complete")),
        )
    )
    if q:
        stmt = stmt.where(
            FileEntry.relpath.like(f"%{collections_svc._escape_like(q)}%", escape="\\")
        )
    stmt = stmt.order_by(FileEntry.ots_stamped_at.desc().nulls_last()).limit(limit)
    return list(await session.execute(stmt))


def _anchored_view(fe: FileEntry, collection: Collection) -> dict[str, Any]:
    return {
        "id": fe.id,
        "filename": Path(fe.relpath).name,
        "relpath": fe.relpath,
        "collection": collection.name,
        "state": fe.ots_state,
    }


@router.get("/learn", response_class=HTMLResponse)
async def learn_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    ctx = await _base_context(request, session, user, "learn")
    return templates.TemplateResponse(request, "learn.html", ctx)


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    file: int | None = Query(None),
):
    ctx = await _base_context(request, session, user, "verify")
    total_anchored = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(Collection.user_id == user.id, FileEntry.ots_state.in_(("incomplete", "complete")))
    )
    ctx["total_anchored"] = int(total_anchored or 0)
    recent = await _anchored_query(session, user, None, 5)
    ctx["recent"] = [_anchored_view(fe, c) for fe, c in recent]
    ctx["preselect"] = None
    if file is not None:
        fe = await session.get(FileEntry, file)
        if fe is not None:
            collection = await session.get(Collection, fe.collection_id)
            if collection is not None and collection.user_id == user.id:
                ctx["preselect"] = file
    return templates.TemplateResponse(request, "verify.html", ctx)


@router.get("/verify/search", response_class=HTMLResponse)
async def verify_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    q: str = Query(""),
):
    matches = await _anchored_query(session, user, q.strip() or None, 50)
    return templates.TemplateResponse(
        request,
        "partials/verify_results.html",
        {
            "results": [_anchored_view(fe, c) for fe, c in matches],
            "q": q.strip(),
            "match_count": len(matches),
            "csrf_token": generate_csrf_token(request),
        },
    )


@router.post("/verify", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def verify_run(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    file_id: int = Form(...),
):
    from ..services import ots as ots_svc

    fe = await session.get(FileEntry, file_id)
    if fe is None:
        raise HTTPException(status_code=404, detail="file not found")
    collection = await session.get(Collection, fe.collection_id)
    if collection is None or collection.user_id != user.id:
        raise HTTPException(status_code=404, detail="file not found")

    settings = get_settings()
    # Re-hash from the read-only store. We MUST verify the proof against the *live* bytes — never
    # fall back to the stored digest, or a deleted/unreadable file would trivially "verify" (the
    # proof was built over that exact digest), the worst false-assurance an integrity tool can give.
    digest = None
    live_unavailable = None  # set to a reason string when the live file can't be hashed
    source = Path(collection.root) / fe.relpath
    if source.is_file():
        try:
            # Off the event loop: re-hashing a (possibly multi-GB) file must not block the panel.
            digest = await asyncio.to_thread(scanner_svc.sha256_file, source)
        except OSError as exc:
            live_unavailable = f"file is unreadable ({exc.strerror or exc})"
    else:
        live_unavailable = "file is missing from disk"

    result = None
    if fe.ots_path and digest:
        try:
            # Verification makes a network round-trip (explorer fetch or node RPC) and may
            # re-parse the proof — keep all of it off the event loop.
            result = await asyncio.to_thread(
                ots_svc.verify,
                fe.ots_path,
                digest,
                backend=settings.verify_backend,
                explorer_url=settings.explorer_url,
                node_rpc_url=settings.node_rpc_url,
            )
        except ots_svc.OtsError as exc:  # pragma: no cover - surfaced as a failed verdict
            result = ots_svc.VerifyResult(
                verified=False, state=fe.ots_state, message=str(exc)
            )

    if settings.verify_backend == "node":
        verified_via = f"{settings.node_rpc_url or 'Bitcoin node'} (node RPC)"
    else:
        host = settings.explorer_url.replace("https://", "").replace("http://", "").rstrip("/")
        verified_via = f"{host} (explorer lookup)"

    if live_unavailable is not None:
        # No live bytes to verify against — distinct danger state, never a green VERIFIED.
        verdict = "danger"
        title = "File unavailable — cannot verify"
    elif result is not None and result.verified:
        verdict = "ok"
        title = "Proof verified"
    elif result is not None and result.state in ("incomplete", "pending"):
        verdict = "warn"
        title = "Proof pending confirmation"
    else:
        verdict = "danger"
        title = "Could not verify"

    # The Bitcoin block hash is not available from `ots verify` (only the height). Do NOT
    # fabricate it — an integrity tool must never show invented provenance. A real block hash
    # requires an explorer/node block lookup (a later refinement); until then it stays absent.
    block_hash = result.block_hash if result else None

    ctx = {
        "file_id": file_id,
        "filename": Path(fe.relpath).name,
        "relpath": fe.relpath,
        "collection": collection.name,
        "sha256": digest or "(unknown)",
        "verdict": verdict,
        "title": title,
        "verified": bool(result and result.verified),
        "existed_by": result.existed_by if result else None,
        "block_height": result.block_height if result else None,
        "block_hash": block_hash,
        "calendars": result.calendars if result else [],
        "verified_via": verified_via,
        "message": (
            live_unavailable
            if live_unavailable is not None
            else (result.message if result else "no proof stored for this file")
        ),
        "csrf_token": generate_csrf_token(request),
    }
    return templates.TemplateResponse(request, "partials/verify_result.html", ctx)


@router.get("/verify/export/{file_id}")
async def verify_export(
    file_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    fe = await session.get(FileEntry, file_id)
    if fe is None:
        raise HTTPException(status_code=404, detail="file not found")
    collection = await session.get(Collection, fe.collection_id)
    if collection is None or collection.user_id != user.id:
        raise HTTPException(status_code=404, detail="file not found")
    # Serve only the `.ots` proof — never the watched file's bytes. The proof is what a third party
    # needs to verify "existed by date"; the panel deliberately won't exfiltrate the source file
    # (the CLI `cairn export`, an on-host operator tool, bundles the file when that's wanted).
    if not fe.ots_path:
        raise HTTPException(
            status_code=409,
            detail=f"no proof stored for {fe.relpath!r}; stamp it before exporting",
        )
    proof = Path(fe.ots_path)
    if not proof.is_file():
        raise HTTPException(status_code=409, detail=f"proof missing on disk: {proof}")
    return FileResponse(
        path=str(proof),
        filename=Path(fe.relpath).name + ".ots",
        media_type="application/octet-stream",
    )


# --- settings -------------------------------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    tab: str = Query("notifications"),
    saved: str | None = Query(None),
    test: str | None = Query(None),
    msg: str | None = Query(None),
):
    settings = get_settings()
    # Show the *effective* config (DB overrides env) so the form reflects what alerts would use.
    eff = await app_settings_svc.effective_settings(session, settings)
    ctx = await _base_context(request, session, user, "settings")
    explorer_host = settings.explorer_url.replace("https://", "").replace("http://", "").rstrip("/")
    ctx.update(
        {
            "tab": tab if tab in ("notifications", "verify", "admin") else "notifications",
            "is_admin_tab_available": user.is_admin and settings.auth_mode == "multi",
            "can_edit_smtp": user.is_admin,
            "healthz_url": "https://cairn.example.com/healthz",
            "email_provider": eff.email_provider,
            # Editable form values (blank when unset, never a placeholder string).
            "smtp_host": eff.smtp_host or "",
            "smtp_port": eff.smtp_port,
            "smtp_user": eff.smtp_user or "",
            "smtp_from": eff.smtp_from or "",
            "smtp_starttls": eff.smtp_starttls,
            "smtp_password_set": await app_settings_svc.smtp_password_is_set(session),
            "smtp_saved": saved == "1",
            "smtp_test": test if test in ("ok", "err") else None,
            "smtp_test_msg": msg or "",
            "verify_backend": settings.verify_backend,
            "explorer_host": explorer_host,
            "calendars": [
                c.replace("https://", "").replace("http://", "").rstrip("/")
                for c in settings.ots_calendars
            ],
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


def _require_admin(user: User) -> None:
    """Global SMTP config is app-wide: only admins may edit it (the sole user is admin in single mode)."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin only")


@router.post("/settings/smtp", dependencies=[Depends(verify_csrf)])
async def settings_smtp_save(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_encryption: str = Form("starttls"),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
):
    _require_admin(user)
    await app_settings_svc.save_smtp(
        session,
        host=smtp_host,
        port=_safe_cadence(smtp_port, 587),
        starttls=smtp_encryption == "starttls",
        user=smtp_user,
        from_=smtp_from,
        provider="local",
        # Blank password field = keep the stored secret unchanged.
        password=smtp_password if smtp_password else None,
    )
    return RedirectResponse("/settings?tab=notifications&saved=1", status_code=303)


@router.post("/settings/smtp/test", dependencies=[Depends(verify_csrf)])
async def settings_smtp_test(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    test_to: str = Form(""),
):
    from urllib.parse import quote

    from ..notify.base import Alert
    from ..notify.smtp import SmtpNotifier

    _require_admin(user)
    recipient = test_to.strip()
    if not recipient:
        return RedirectResponse(
            "/settings?tab=notifications&test=err&msg=" + quote("Enter a recipient address"),
            status_code=303,
        )
    eff = await app_settings_svc.effective_settings(session, get_settings())
    alert = Alert(
        collection_name="Cairn test",
        summary="test alert",
        paths=["This is a test email from Cairn — your SMTP settings work."],
        detected_at=datetime.now(timezone.utc),
    )
    try:
        await SmtpNotifier(recipients=[recipient], settings=eff).send(alert)
        return RedirectResponse(
            "/settings?tab=notifications&test=ok&msg=" + quote(f"Sent to {recipient}"),
            status_code=303,
        )
    except Exception as exc:  # NotifierError or transport error — surface it to the operator
        return RedirectResponse(
            "/settings?tab=notifications&test=err&msg=" + quote(str(exc)[:200]),
            status_code=303,
        )
