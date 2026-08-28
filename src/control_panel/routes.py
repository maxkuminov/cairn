"""Cairn control-panel routes: pages + htmx partial endpoints.

Server-rendered Jinja2 + htmx in the locked Slate design. Single-user mode resolves the implicit
user (``ensure_implicit_user``) and scopes every query by ``user_id`` so the same code becomes
multi-user-correct once login lands. Mutating endpoints are CSRF-protected. File search /
pagination is mandatory server-side (a collection can hold ~186k files).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, literal, null, select, text, union_all, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..csrf import generate_csrf_token, verify_csrf
from ..database import get_session
from ..models.db import Collection, Event, FileEntry, Run, User
from ..services import app_settings as app_settings_svc
from ..services import collections as collections_svc
from ..services import proofs as proofs_svc
from ..services import scanner as scanner_svc
from ..services.panel_url import panel_link

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
    """Proof-state tallies for a collection: raw totals plus the *active* (stampable) population.

    The unqualified keys (``none``/``pending``/``incomplete``/``complete``) are raw counts over
    every row and are for raw display only. Every **coverage claim** must be computed from the
    ``*_active`` keys, which carry ``status != 'missing'`` — exactly the population
    ``mark_unstamped_pending`` queues and "Stamp all" acts on. Counting confirmed proofs over all
    files while dividing by a missing-free denominator is how one missing file with a complete proof
    reports ``1 / 1`` coverage of a collection where nothing present is confirmed (design D5). By
    construction ``complete_active + incomplete_active + pending_active + none_active == stampable``.
    """
    rows = await session.execute(
        select(FileEntry.ots_state, func.count())
        .where(FileEntry.collection_id == collection_id)
        .group_by(FileEntry.ots_state)
    )
    out = {"none": 0, "pending": 0, "incomplete": 0, "complete": 0}
    for state, n in rows:
        out[state] = n

    active = await session.execute(
        select(FileEntry.ots_state, func.count())
        .where(FileEntry.collection_id == collection_id, FileEntry.status != "missing")
        .group_by(FileEntry.ots_state)
    )
    for key in ("none", "pending", "incomplete", "complete"):
        out[f"{key}_active"] = 0
    for state, n in active:
        out[f"{state}_active"] = n
    return out


async def _alert_badge_count(session: AsyncSession, collection_ids: list[int]) -> int:
    """The sidebar alert badge's number: files that are missing **or** modified (design D3).

    One definition for four render sites (``base.html`` and the three OOB-swap partials), fed by
    three call sites (``_base_context``, ``_event_feed``, ``ack_event``). The badge used to count
    ``missing`` only while the dashboard tile beside it counted ``missing + modified``, so the two
    disagreed; three independent inline queries is how that drifted.
    """
    if not collection_ids:
        return 0
    n = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .where(
            FileEntry.collection_id.in_(collection_ids),
            FileEntry.status.in_(("missing", "modified")),
        )
    )
    return int(n or 0)


async def _open_event_count(session: AsyncSession, collection_id: int) -> int:
    """Count of a collection's unacknowledged events — the population ``accept_collection`` clears.

    Used by the collection view (the D7 render gate) and by the D14 fingerprint header, so the gate
    and the guard can never disagree about what "no open events" means.
    """
    n = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.collection_id == collection_id, Event.acknowledged_at.is_(None))
    )
    return int(n or 0)


def _collection_status(counts: dict[str, int]) -> str:
    # `new` (added) files are informational and born-acknowledged (streamline-event-acknowledgement):
    # a newly-tracked, unmodified file is the happy path, not something to warn about. Only an
    # alarming WORM `modified` raises "attention"; a `missing` file raises "alert". A collection whose
    # only non-ok files are `new` reads "All clear" (a scan never promotes new→ok; baseline via accept
    # to move them into the "Verified OK" count, but it is not required for the collection to be healthy).
    # A collection with no files at all is not healthy — it is watching nothing. Its `counts` dict
    # sums to zero exactly in that case, so no extra argument is needed (design D5 / #31). A root
    # that is a typo or a failed bind mount scans `ok` forever; reporting it green is the failure.
    if sum(counts.values()) == 0:
        return "empty"
    if counts["missing"] > 0:
        return "alert"
    if counts["modified"] > 0:
        return "attention"
    return "ok"


# (label, colour, icon, pill kind). `attention` (WORM modified) keeps the warning triangle;
# `alert` (missing) uses `minusCircle`, matching every other place a missing file is drawn — the
# two used to share one icon (design D6). `empty` is muted and explicitly not green (design D5);
# `folder` is already in the icon set and is not spoken for by another status.
_STATUS_META = {
    "ok": ("All clear", "var(--ok)", "checkCircle", "ok"),
    "attention": ("Attention", "var(--warn)", "alert", "warn"),
    "alert": ("Alert", "var(--danger)", "minusCircle", "danger"),
    "empty": ("No files indexed", "var(--text-3)", "folder", "muted"),
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


def _pending_line(pending_active: int, incomplete_active: int) -> str | None:
    """Name the two not-yet-confirmed proof states separately; never sum them (design D13).

    ``pending`` = queued locally, not yet submitted to a calendar (a backlog, possibly stuck);
    ``incomplete`` = submitted, waiting on Bitcoin. Adding them together and calling the total
    "pending confirmation" reports a file that was never submitted as awaiting confirmation.
    Whichever half is zero is dropped; both zero returns ``None``.
    """
    parts = []
    if pending_active > 0:
        parts.append(f"{pending_active:,} queued")
    if incomplete_active > 0:
        parts.append(f"{incomplete_active:,} pending confirmation")
    return " · ".join(parts) if parts else None


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
    # Coverage arithmetic over ONE population: files that could be stamped (design D5).
    stampable = total_files - counts["missing"]
    complete_active = ots["complete_active"]
    # "all confirmed" is a single comparison over the identity in `_ots_counts`, so it cannot drift
    # out of step with the four counts the way four separate conditions can. `stampable > 0` is what
    # keeps a zero-file collection from reporting a green completeness claim (#31).
    all_confirmed = complete_active == stampable and stampable > 0
    open_events = await _open_event_count(session, collection.id)
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
        "stampable": stampable,
        "anchored_ratio": f"{complete_active:,} / {stampable:,}",
        "all_confirmed": all_confirmed,
        "not_stamped": ots["none_active"],
        "pending_line": _pending_line(ots["pending_active"], ots["incomplete_active"]),
        "is_empty": total_files == 0,
        # The collection's unacknowledged-event count: the D7 render gate for "Baseline new files"
        # and (recomputed under the write lock) part of the D14 fingerprint header.
        "open_events": open_events,
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
    for c in collections:
        counts = await _collection_counts(session, c.id)
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
        # Copy that describes who can open a panel URL has to know which mode is running: in
        # `single` there is no login wall at all, so a claim about owner-scoping would be false.
        "auth_mode": get_settings().auth_mode,
        "username": user.username,
        "is_admin": user.is_admin,
        "user_email": f"{user.username}@localhost",
        "sidebar_collections": sidebar,
        "alert_count": await _alert_badge_count(session, [c.id for c in collections]),
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

    ``open_events`` (the "unreviewed" pill) and ``alert_count`` (the sidebar badge) are real
    COUNT queries over ALL of the user's events/files, not just the 20 rendered rows, so both stay
    accurate past the feed cap. Auto-acknowledged ``added``/``restored`` events render in the feed
    but never count toward ``open_events``. The badge comes from :func:`_alert_badge_count` so it
    counts the same population as the dashboard tile beside it (design D3).
    """
    events: list[Event] = []
    open_events = 0
    alert_count = await _alert_badge_count(session, collection_ids)
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
    return {
        "events": [await _event_view(session, e) for e in events],
        "open_events": int(open_events or 0),
        "alert_count": alert_count,
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
    total_new = sum(v["counts"]["new"] for v in views)
    total_issues = total_missing + total_modified

    # Fleet-wide proof coverage is computed strictly over `perfile` collections — numerator AND
    # denominator (design D5 / task 3.7). Tripwire collections stamp nothing and their stamp route
    # refuses them, so folding their files in ships a "not stamped" count no operator action can
    # clear; dropping them from one half only would restore a false "all confirmed".
    notarized = [v for v in views if v["ots"] == "perfile"]
    total_anchored = sum(v["ots_counts"]["complete_active"] for v in notarized)
    fleet_stampable = sum(v["stampable"] for v in notarized)
    fleet_none = sum(v["ots_counts"]["none_active"] for v in notarized)
    fleet_pending_line = _pending_line(
        sum(v["ots_counts"]["pending_active"] for v in notarized),
        sum(v["ots_counts"]["incomplete_active"] for v in notarized),
    )
    scope_note = (
        f"across {len(notarized)} notarized collection"
        f"{'' if len(notarized) == 1 else 's'}"
    )
    if not notarized:
        anchored_sub = "no notarized collections"
    elif fleet_stampable == 0:
        anchored_sub = f"No files indexed yet · {scope_note}"
    elif total_anchored == fleet_stampable:
        anchored_sub = f"all confirmed · {scope_note}"
    else:
        parts = [f"{total_anchored:,} / {fleet_stampable:,} confirmed"]
        if fleet_pending_line:
            parts.append(fleet_pending_line)
        if fleet_none > 0:
            parts.append(f"{fleet_none:,} not stamped")
        parts.append(scope_note)
        anchored_sub = " · ".join(parts)

    # The biggest, reddest number on the panel used to be an inert <div> (#18). It becomes a link to
    # the page that can act on it: exactly one affected collection -> that collection's review page;
    # several -> the collections list. NEVER `/review` — it is a 404 until #27 (design D4).
    affected = [v for v in views if v["issues"] > 0]
    issues_href = None
    if total_issues > 0:
        issues_href = (
            f"/collection/{affected[0]['id']}/review" if len(affected) == 1 else "/collections"
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
                "issues": total_issues,
                "issues_sub": f"{total_missing} missing · {total_modified} modified",
                "issues_color": "var(--danger)" if total_issues > 0 else "var(--ok)",
                "issues_href": issues_href,
                "new": total_new,
                "new_sub": "watched, not yet baselined",
                "anchored": humanize_count(total_anchored),
                "anchored_sub": anchored_sub,
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
    alert_count = await _alert_badge_count(session, collection_ids)
    if collection_ids:
        open_events = await session.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.collection_id.in_(collection_ids), Event.acknowledged_at.is_(None))
        )

    if view == "review":
        # Acknowledged from the review page: swap that row in place and refresh the collection's
        # "unreviewed" pill (#review-open-pill) plus the global sidebar badge.
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
                "alert_count": alert_count,
            },
        )

    view_ctx = await _event_view(session, event)
    return templates.TemplateResponse(
        request,
        "partials/event_ack.html",
        {"e": view_ctx, "open_events": int(open_events or 0), "alert_count": alert_count},
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


VIEW_MODES = ("tree", "list")
FILE_FILTERS = ("all", "issues", "new", "ok")


@router.get("/collection/{collection_id}", response_class=HTMLResponse)
async def collection_detail(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    view: str | None = Query(None),
    filter: str = Query("all"),
):
    """Collection detail. ``view`` / ``filter`` are optional deep-link parameters (design D11).

    Both are whitelist-validated and both default to today's render. ``filter`` is threaded into the
    **initial list-view** query — a checked "Issues" radio over an unfiltered list is worse than no
    radio at all — while the tree query stays unfiltered (filtering a directory listing would make
    folder counts disagree with the rows beneath them). Because of that, a non-``all`` filter with no
    explicit ``view`` implies the list view; an explicit ``view=tree`` is honoured as given.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    status_filter = filter if filter in FILE_FILTERS else "all"
    if view in VIEW_MODES:
        view_mode = view
    else:
        view_mode = "list" if status_filter != "all" else "tree"
    cview = await _collection_view(session, collection)
    rows, total = await collections_svc.query_files(
        session,
        collection_id,
        status_filter=status_filter,
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
    # The D14 hidden field is minted only in the one state that renders the baseline form (D7).
    # `cview`'s three numbers are a cheap *pre*-filter — they only decide whether it is worth
    # reading the population at all, so an ordinary detail render of a large collection never
    # materializes its `new` set. The gate that actually decides to render the form, and the
    # fingerprint that form carries, both come from the SAME single-statement read: publishing a
    # form for one snapshot while hashing another is the whole failure mode D14 guards against.
    show_baseline = False
    population_fp = ""
    if cview["issues"] == 0 and cview["open_events"] == 0 and cview["counts"]["new"] > 0:
        pop = await _read_population(session, collection, "baseline-new")
        show_baseline = bool(pop.files) and pop.issues == 0 and not pop.open_events
        if show_baseline:
            population_fp = _population_fingerprint(collection, pop)
    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": cview,
            "files": [_file_view(f) for f in rows],
            "files_total": total,
            "files_shown": len(rows),
            "q": "",
            "filter": status_filter,
            "view": view_mode,
            "show_baseline": show_baseline,
            "population_fp": population_fp,
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


# --- accept-family population fingerprint (design D14) ---------------------------------------
# `accept_collection` is one unscoped verb that rewrites baselines, DELETEs `missing` rows and
# acknowledges every open event on the collection. Both routes that call it render a page first and
# act at submit time, and the scheduler can complete a scan in between — so the button labelled
# "Baseline 40 new files" can become a deletion of `missing` rows the operator never saw. Neither a
# render-time visibility rule nor a submit-time recount closes that: the recount is a separate
# statement from the accept, and the same scan can claim, run and commit in the gap.
#
# The fix is one mechanism used identically by both routes: each form carries a hidden SHA-256 of a
# canonical description of the population it claims to act on, and the POST recomputes it INSIDE the
# same write transaction as the accept's own reads and writes. SQLite's single-writer serialization
# is what makes the check-and-act atomic.

# US separates fields, RS separates records. The byte-length prefix on `relpath` is what makes the
# encoding injective: a filename may legally contain any byte but `/` and NUL — US and RS included —
# so without the length two different populations could be made to hash equal. Framing by length
# removes that by construction rather than by escaping.
_FP_US = "\x1f"
_FP_RS = "\x1e"
# GS/FS do the same framing one level down, inside the header's open-event component: GS between
# event records, FS between an event's fields. Distinct characters (rather than reusing US/RS) keep
# the component unambiguous no matter what an event `kind` or an ISO timestamp ever contains.
_FP_GS = "\x1d"
_FP_FS = "\x1c"

# Which file statuses each form's fingerprint covers.
_FP_SCOPES: dict[str, tuple[str, ...]] = {
    "baseline-new": ("new",),
    "review-accept": ("missing", "modified"),
}

# SQLite result codes that mean "someone else holds the writer lock", i.e. drift — not a broken
# datastore. Everything else propagates.
_FP_LOCK_ERRNAMES = frozenset({"SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT", "SQLITE_LOCKED"})
_FP_LOCK_MESSAGES = ("database is locked", "database table is locked", "database is busy")

# The statement that acquires (or upgrades to) the writer transaction: a no-op write against the
# collection's own row. SQLite holds the lock until commit or rollback, so from here nothing else
# can commit and the guard's reads describe the state the accept will act on.
_FP_WRITE_LOCK_SQL = text("UPDATE collections SET name = name WHERE id = :id")


async def _take_write_lock(session: AsyncSession, collection_id: int) -> None:
    """Escalate this session's transaction to a writer (see :data:`_FP_WRITE_LOCK_SQL`)."""
    await session.execute(_FP_WRITE_LOCK_SQL, {"id": collection_id})


def _is_lock_contention(exc: OperationalError) -> bool:
    """True when an ``OperationalError`` is SQLite refusing the writer lock, not a broken datastore.

    Classified narrowly: the driver exception's ``sqlite_errorname`` where the runtime provides it
    (Python >= 3.11), falling back to the message text. A corrupt or misconfigured datastore must
    never be reported to the operator as "the collection changed since the page loaded".
    """
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "sqlite_errorname", None)
    if name:
        return name in _FP_LOCK_ERRNAMES
    msg = str(orig or exc).lower()
    return any(m in msg for m in _FP_LOCK_MESSAGES)


class _PopFile(NamedTuple):
    """One file row of an accept-family population.

    Carries BOTH the fields the guard hashes (``id``/``relpath``/``status``/``sha256``/
    ``first_seen``) and the fields the review page renders, because they come from one fetch: see
    :func:`_read_population`. Attribute names match :class:`~src.models.db.FileEntry`, so
    :func:`_review_item` renders either.
    """

    id: int
    relpath: str
    status: str
    sha256: str | None
    first_seen: datetime | None
    size: int | None
    last_checked: datetime | None
    last_changed: datetime | None
    ots_state: str


class _PopEvent(NamedTuple):
    """One open event, by identity and generation — never by tally (see `_population_fingerprint`)."""

    id: int
    kind: str
    detected_at: datetime | None


class _Population(NamedTuple):
    """Everything an accept-family form displays and hashes, read as ONE statement.

    ``files`` is sorted by ``relpath`` (total order — unique within a collection), ``open_events``
    by ``id``. ``issues`` is the ``missing`` + ``modified`` count and is read only for the
    ``baseline-new`` scope (0 elsewhere, where it is neither hashed nor asserted).
    ``total_files`` is the collection's whole file population (every status) from this same
    snapshot: it is what tells "nothing is wrong" apart from "nothing is watched", and reading it
    anywhere else would let the two disagree (see :func:`collection_review`).
    """

    scope: str
    files: list[_PopFile]
    open_events: list[_PopEvent]
    issues: int
    total_files: int


async def _read_population(
    session: AsyncSession, collection: Collection, scope: str
) -> _Population:
    """Read the whole population for ``scope`` in a SINGLE SQL statement.

    The mint side and the recount side both go through here, so the two can never encode the same
    state differently — and, on the mint side, the rows the page *renders* are sliced from the very
    list that is hashed. That is not a tidiness point. Python's ``sqlite3`` runs in legacy
    transaction mode: a ``SELECT`` does not open a transaction, so two consecutive ``SELECT``s on
    one connection are two independent snapshots and a scanner can commit between them. A review
    page that read its visible rows in one statement and minted the fingerprint in another could
    therefore publish a fingerprint for a population it never displayed — an unchanged POST would
    then validate and delete a ``missing`` row the operator never saw, which is precisely the
    accident this guard exists to prevent. One statement is one snapshot, by construction.

    The union is the price of that: the file rows, the open-event rows and (for ``baseline-new``)
    the issue count live in different tables/shapes, so they are padded to one 10-column row shape
    and tagged in the first column. SQLite evaluates the compound statement inside a single implicit
    read transaction; SQLAlchemy takes result types from the first leg, so the events' ``detected_at``
    is decoded by ``first_seen``'s ``DateTime`` processor (same column type, same storage format).
    """
    statuses = _FP_SCOPES[scope]
    files_q = select(
        literal("f").label("part"),
        FileEntry.id.label("n1"),
        FileEntry.relpath.label("t1"),
        FileEntry.status.label("t2"),
        FileEntry.sha256.label("t3"),
        FileEntry.first_seen.label("d1"),
        FileEntry.size.label("n2"),
        FileEntry.last_checked.label("d2"),
        FileEntry.last_changed.label("d3"),
        FileEntry.ots_state.label("t4"),
    ).where(
        FileEntry.collection_id == collection.id,
        FileEntry.status.in_(statuses),
    )
    events_q = select(
        literal("e"),
        Event.id,
        Event.kind,
        null(),
        null(),
        Event.detected_at,
        null(),
        null(),
        null(),
        null(),
    ).where(Event.collection_id == collection.id, Event.acknowledged_at.is_(None))
    # The collection's entire file population, counted in the same snapshot as the rows above so a
    # concurrent accept cannot empty the collection between "are there issues?" and "is this
    # collection watching anything at all?" — the interleaving that rendered a false green "All
    # clear" for a just-emptied collection. Counted, never materialized: only its zero-ness is used.
    total_q = (
        select(
            literal("t"),
            func.count(),
            null(),
            null(),
            null(),
            null(),
            null(),
            null(),
            null(),
            null(),
        )
        .select_from(FileEntry)
        .where(FileEntry.collection_id == collection.id)
    )
    legs = [files_q, events_q, total_q]
    if scope == "baseline-new":
        # The zero-issue assertion the detail form makes, hashed and re-asserted. Counted rather
        # than materialized: on a collection that has both `new` files and a large issue set the
        # form is not shown at all, and fetching those rows would be pure waste.
        legs.append(
            select(
                literal("c"),
                func.count(),
                null(),
                null(),
                null(),
                null(),
                null(),
                null(),
                null(),
                null(),
            )
            .select_from(FileEntry)
            .where(
                FileEntry.collection_id == collection.id,
                FileEntry.status.in_(("missing", "modified")),
            )
        )

    files: list[_PopFile] = []
    events: list[_PopEvent] = []
    issues = 0
    total_files = 0
    for row in (await session.execute(union_all(*legs))).all():
        part = row[0]
        if part == "f":
            files.append(_PopFile(*row[1:10]))
        elif part == "e":
            events.append(_PopEvent(row[1], row[2], row[5]))
        elif part == "t":
            total_files = int(row[1] or 0)
        else:
            issues = int(row[1] or 0)

    files.sort(key=lambda f: f.relpath)
    events.sort(key=lambda e: e.id)
    return _Population(
        scope=scope,
        files=files,
        open_events=events,
        issues=issues,
        total_files=total_files,
    )


def _population_fingerprint(collection: Collection, pop: _Population) -> str:
    """Hex SHA-256 of D14's canonical encoding of one :func:`_read_population` snapshot.

    Pure: it hashes the rows it is handed and reads nothing. That is what lets the mint side hash
    exactly the rows it rendered, and the recount side re-derive the same encoding from its own
    single read under the write lock — one code path, two callers.

    Preimage = header + RS + RS.join(records), UTF-8 encoded.

    * header — ``{scope}US{collection_id}US{created_at}USopen_events={events}`` and, for
      ``baseline-new`` only, a further ``USissues={n}``. Both of that scope's zero assertions
      therefore travel *inside* the hash.
    * ``events`` — GS-joined, id-ordered ``{id}FS{kind}FS{detected_at}`` for every event with
      ``acknowledged_at IS NULL``; empty when there are none.
    * record — ``{id}US{len(relpath_bytes)}US{relpath}US{status}US{sha256 or ''}US{first_seen}``,
      one per file, sorted by ``relpath`` (unique within a collection, so the order is total).
      Deliberately **not** sorted by ``id``: ``id`` is the field this encoding distrusts.

    Why each field is there. ``files.id`` / ``collections.id`` are ``INTEGER PRIMARY KEY`` *without*
    ``AUTOINCREMENT``, so SQLite may hand a deleted row's id to a later insert — an
    id-and-status-only preimage is byte-identical for two populations sharing no file, and the stale
    form would then delete a record the operator never saw. ``relpath`` pins the logical file,
    ``sha256`` the content generation, and ``first_seen`` the *row* generation (it is NOT NULL,
    written at insertion and never rewritten in place, so a row deleted and re-created at the same
    path with the same digest on the same reused id still encodes differently). The collection's own
    ``created_at`` does the same job one level up for a recreated collection reusing its id.

    The open-event population is the third population the verb mutates and is not derivable from the
    file rows: a file can go modified -> missing -> restored between render and submit, returning the
    protected set to exactly its rendered value while its ``modified`` event stays open by design.
    It is bound by **identity**, not by cardinality: hashing only the count leaves a same-count ABA
    open — the single open ``missing`` event on a file is acknowledged (by a restore, or in another
    tab) and a fresh incident opens another one, the count returns to 1, and a stale form would
    validate and silently close an alert nobody has seen. ``id`` + ``kind`` name the incident and
    ``detected_at`` pins its generation, since ``events.id`` is a rowid too and an event *can* be
    deleted (``events.file_id`` is ``ON DELETE CASCADE``, and ``_reconcile_moves`` deletes file
    rows), which frees the id for reuse.
    (``added``/``restored``/``moved`` events are born acknowledged, so an ordinary new file does not
    enter this component — the documented ``new``-set exception survives.)
    """
    events = _FP_GS.join(
        f"{e.id}{_FP_FS}{e.kind}{_FP_FS}"
        f"{e.detected_at.isoformat() if e.detected_at else ''}"
        for e in pop.open_events
    )
    header = (
        f"{pop.scope}{_FP_US}{collection.id}{_FP_US}"
        f"{collection.created_at.isoformat()}{_FP_US}open_events={events}"
    )
    if pop.scope == "baseline-new":
        header += f"{_FP_US}issues={pop.issues}"

    records = [
        f"{f.id}{_FP_US}{len(f.relpath.encode('utf-8'))}{_FP_US}{f.relpath}{_FP_US}{f.status}"
        f"{_FP_US}{f.sha256 or ''}{_FP_US}"
        f"{f.first_seen.isoformat() if f.first_seen else ''}"
        for f in pop.files
    ]
    preimage = header + _FP_RS + _FP_RS.join(records)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


async def _guarded_accept(
    session: AsyncSession,
    collection: Collection,
    user: User,
    scope: str,
    submitted_fp: str,
) -> RedirectResponse | None:
    """Run ``accept_collection`` only if the population still matches ``submitted_fp`` (design D14).

    Returns ``None`` when the accept was performed (the caller then redirects to its own success
    target), or the fail-closed refusal response — a 303 to ``/collection/{id}/review?stale=1``,
    the page that lists exactly the issues that caused the refusal — on any of:

    * an absent or empty ``population_fp`` (fail closed: an unguarded POST is refused, never run);
    * an operation already in flight (``active_run``, now belt-and-braces for the *long* window);
    * the writer lock being unobtainable (BUSY / BUSY_SNAPSHOT / LOCKED — that *is* drift, and an
      uncaught 500 on a destructive POST is the refusal promise broken exactly where the guard
      exists, and an invitation to retry blind);
    * for ``baseline-new``, ``issues`` or ``open_events`` no longer being zero;
    * the recomputed fingerprint differing from the submitted one.

    Every refusal path rolls back before returning, so a refusal mutates nothing.
    """
    stale = RedirectResponse(f"/collection/{collection.id}/review?stale=1", status_code=303)
    submitted = (submitted_fp or "").strip()
    if not submitted:
        await session.rollback()
        return stale
    if await collections_svc.active_run(session, collection.id) is not None:
        await session.rollback()
        return stale

    # 1. Take the write lock FIRST, before the reads the guard depends on.
    try:
        await _take_write_lock(session, collection.id)
    except OperationalError as exc:
        if not _is_lock_contention(exc):
            raise
        await session.rollback()
        return stale

    # 2. Recompute inside this transaction, through the SAME single-statement read and the SAME
    #    encoder the form was minted with, and re-assert the detail form's two zeroes explicitly
    #    (they are hashed too, so this is belt-and-braces on an unambiguous failure mode).
    pop = await _read_population(session, collection, scope)
    if scope == "baseline-new" and (pop.issues > 0 or pop.open_events):
        await session.rollback()
        return stale
    current = _population_fingerprint(collection, pop)

    # 3. Compare.
    if not hmac.compare_digest(current, submitted):
        await session.rollback()
        return stale

    # 4. Only now act; `accept_collection`'s own commit() closes this same transaction.
    await scanner_svc.accept_collection(session, collection, user.id)
    return None


@router.post("/collection/{collection_id}/accept", response_class=HTMLResponse, dependencies=[Depends(verify_csrf)])
async def collection_accept(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    population_fp: str = Form(""),
):
    """The detail page's "Baseline new files" submit, bound to the population it was rendered for."""
    collection = await _get_owned_collection(session, collection_id, user)
    refused = await _guarded_accept(session, collection, user, "baseline-new", population_fp)
    if refused is not None:
        return refused
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


def _review_item(fe: FileEntry | _PopFile, root: str, event: Event | None) -> dict[str, Any]:
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
    stale: str = Query(""),
):
    """Focused review of a collection's missing + modified files, with recovery guidance.

    Publishes two keys the review template renders: ``population_fp`` (design D14's hidden field for
    the Accept form) and ``stale`` (the "this collection changed since the page loaded" banner,
    whitelisted from ``?stale=1`` exactly as ``view``/``filter`` are handled in D11).
    """
    collection = await _get_owned_collection(session, collection_id, user)
    view = await _collection_view(session, collection)

    # ONE read of the whole protected population (uncapped) + the open-event set. Everything below
    # is derived from it: the rendered rows, the recovery copy list, the issue total, the "need
    # action" pill and the fingerprint. Re-querying for any of those would reopen the window this
    # guard exists to close — a scanner commit between two SELECTs would put a file in the hash
    # that is not in the list, and the operator's unchanged POST would then delete it unseen.
    pop = await _read_population(session, collection, "review-accept")
    # `pop.files` is path-ordered; the page shows missing first, then modified, stable by path
    # within each. A Python sort of the fetched rows, not a second query.
    rendered = sorted(pop.files, key=lambda f: (0 if f.status == "missing" else 1, f.relpath))
    rendered = rendered[:REVIEW_ROW_LIMIT]
    events = await _latest_events_by_file(session, collection_id, [f.id for f in rendered])
    items = [_review_item(f, collection.root, events.get(f.id)) for f in rendered]

    copy_relpaths = [f.relpath for f in pop.files[:REVIEW_COPY_LIMIT]]
    review_open = len(pop.open_events)

    total_issues = len(pop.files)
    # The legend sits directly above the list, so it counts the list: take its two numbers from the
    # snapshot rather than from `_collection_view`'s earlier, separate count query.
    view["counts"] = dict(view["counts"])
    view["counts"]["missing"] = sum(1 for f in pop.files if f.status == "missing")
    view["counts"]["modified"] = total_issues - view["counts"]["missing"]
    # Same reason, one level up: "no issues" only means "All clear" if the collection is actually
    # watching files. `_collection_view` counted them in an EARLIER, separate statement, so an
    # accept committed between the two reads (the last missing file adopted, the collection left
    # empty) left `is_empty` false while this snapshot has nothing in it — the page then claimed a
    # green all-clear for a collection that has established nothing (#31). Take it from the
    # snapshot the rest of this page is rendered from.
    view["is_empty"] = pop.total_files == 0
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
            "review_open": review_open,
            # Hashed over the collection's ENTIRE missing+modified set — the same `pop.files` the
            # rows above were sliced from, not a second query: accept acts on all of it, so hashing
            # only the visible rows would leave every issue past the cap outside the guard, and
            # re-reading would let the two disagree.
            "population_fp": _population_fingerprint(collection, pop),
            "stale": stale == "1",
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
    population_fp: str = Form(""),
):
    """Re-baseline from the review page (new/modified → ok, missing removed), stay on review.

    Guarded by the same D14 fingerprint as the detail form. The review page is not exempt: its list
    is a *render*, and "these are exactly the files this button will adopt" stops being true the
    moment a scan records another missing file — the operator would then delete a record they never
    saw, from the one page whose entire purpose is that they saw it.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    refused = await _guarded_accept(session, collection, user, "review-accept", population_fp)
    if refused is not None:
        return refused
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
        except ots_svc.OtsError as exc:
            # Belt-and-braces net, no longer the primary path: `verify` now *returns* an
            # unreachable backend as a `transport_error` result rather than raising. Drop
            # `fe.ots_state` entirely — inheriting it is what rendered a dead node/binary as
            # "Proof pending confirmation".
            result = ots_svc.VerifyResult(
                verified=False, state="none", transport_error=str(exc), message=str(exc)
            )

    if settings.verify_backend == "node":
        verified_via = f"{settings.node_rpc_url or 'Bitcoin node'} (node RPC)"
    else:
        host = settings.explorer_url.replace("https://", "").replace("http://", "").rstrip("/")
        verified_via = f"{host} (explorer lookup)"

    # A digest disagreement establishes only that the live digest and the proof's committed digest
    # DIFFER — not which of the two moved (design D1). The tiebreaker is the baseline Cairn recorded
    # for this file at its last scan: if the live bytes no longer hash to it, the FILE changed; if
    # they still do, blaming the file would be a false alarm on the product's core signal. With no
    # recorded baseline neither can be blamed, so the card names both possibilities.
    #
    # What the recorded baseline is NOT is the digest the stored proof was made from: a scan
    # overwrites `files.sha256` with the newly observed bytes BEFORE a replacement proof exists, so
    # in the modified-awaiting-re-stamp window the live bytes equal the recorded baseline while the
    # (still valid, still current) proof commits to the previous version. Accusing that proof of
    # being corrupt or misfiled there is the mirror-image false alarm. Cairn cannot tell the two
    # apart from what it stores today, so "live == recorded, != proof" splits by the row's own
    # state: `pending` (a re-stamp is queued) or a `modified`/`new` status is the stale-proof
    # window and gets that explanation; anything else stays explicitly undecided. A definitive
    # blame needs each proof's own digest recorded beside it — the proof-versioning work (#15),
    # deliberately out of this sprint (design D1, accepted limitation).
    stored_sha = (fe.sha256 or "").strip().lower()
    live_sha = (digest or "").strip().lower()
    mismatch_blame = None
    if result is not None and result.digest_mismatch and live_sha:
        if not stored_sha:
            mismatch_blame = "unknown"
        elif live_sha != stored_sha:
            mismatch_blame = "file"
        elif fe.ots_state == "pending" or fe.status in ("modified", "new"):
            mismatch_blame = "proof-stale"
        else:
            mismatch_blame = "proof"

    # Verdict by *reason*, in the order of design D2. Branching on the proof's lifecycle state
    # before asking why verification failed is what let a changed file read "pending confirmation".
    if live_unavailable is not None:
        # No live bytes to verify against — distinct danger state, never a green VERIFIED.
        verdict = "danger"
        title = "File unavailable — cannot verify"
    elif mismatch_blame == "file":
        verdict = "danger"
        title = "File no longer matches its proof"
    elif mismatch_blame == "proof-stale":
        # The row says a re-stamp is owed, so the proof committing to other bytes is the expected
        # state of that window, not a fault. Amber like the other "a stamp is owed" verdicts — red
        # here would train the operator to dismiss the red card that means a real mismatch.
        verdict = "warn"
        title = "Proof predates this version of the file"
    elif mismatch_blame == "proof":
        # The live bytes still hash to the recorded baseline and the row claims no re-stamp is
        # owed. That the proof disagrees is all that is established: this card describes it and
        # blames nothing (see the tiebreaker note above).
        verdict = "danger"
        title = "This proof does not match this file"
    elif mismatch_blame == "unknown":
        verdict = "danger"
        title = "Fingerprint and proof disagree"
    elif result is not None and result.verified:
        # Above `proof_mismatch` as belt-and-braces on the source-level rule: one attestation
        # confirmed against its real block is proof, and no caller may turn a bad sibling into a
        # verdict.
        verdict = "ok"
        title = "Proof verified"
    elif result is not None and result.proof_mismatch:
        # Before `transport_error`: a mismatch established before the network failed is knowledge.
        verdict = "danger"
        title = "This proof does not check out"
    elif result is not None and result.transport_error:
        # Neutral, never red — an unreachable backend is not evidence against the file, and crying
        # wolf in red teaches the operator to dismiss the red card that means a real mismatch.
        verdict = "unavailable"
        title = "Couldn't check right now"
    elif result is not None and result.inconclusive:
        verdict = "unavailable"
        title = "Couldn't confirm — pending, changed, or unreachable"
    elif result is not None and result.unreadable_proof:
        # The `.ots` could not be parsed, so nothing was established about anything. Neutral, never
        # red: the generic "could not verify" fallback below reads as a finding about the FILE.
        verdict = "unavailable"
        title = "Proof file could not be read"
    elif result is not None and result.state == "incomplete":
        verdict = "warn"
        title = "Pending confirmation"
    elif result is not None and result.state == "pending":
        # Two states, two branches (design D13): queued-but-not-submitted is not awaiting Bitcoin.
        verdict = "warn"
        title = "Queued to stamp"
    elif result is None and fe.ots_state == "pending":
        # No proof to check because none has been made yet — the file is queued for stamping and
        # `ots_path` is still empty. This reached the red "Could not verify" fallback, which reads
        # as "something is wrong with this file"; nothing is. Same reading as the branch above (and
        # as the `pending` badge everywhere else), just arrived at from the file row rather than
        # from a `VerifyResult`, because there is no proof to build one from.
        verdict = "warn"
        title = "Queued to stamp"
    elif result is None and fe.ots_state == "none":
        # Never stamped at all: neutral information, not a failure. Red here would be crying wolf
        # over a `none` collection or a file the backfill has not reached.
        verdict = "unavailable"
        title = "Not notarized yet"
    else:
        # Genuinely could not be checked: a proof state that claims a proof exists (`incomplete` /
        # `complete`) while nothing verifiable was produced. That IS worth a red card.
        verdict = "danger"
        title = "Could not verify"

    # The Bitcoin block hash is not available from `ots verify` (only the height). Do NOT
    # fabricate it — an integrity tool must never show invented provenance. A real block hash
    # requires an explorer/node block lookup (a later refinement); until then it stays absent.
    block_hash = result.block_hash if result else None

    # "Checked using <backend>" claims a lookup was made at that backend. It is true only where the
    # chain was actually consulted AND answered: a verified attestation, or an attestation fetched
    # and found not to commit to its block. Every other branch never reached the network — a
    # never-stamped or queued file has nothing to look up, an unparseable `.ots` yields no
    # attestation to look up, a digest disagreement short-circuits before the first fetch (the
    # explorer backend compares the committed digest locally and returns), and a transport failure
    # is the lookup NOT happening. Printing the backend there attributes the outcome to a check
    # that never ran, which is the same class of overclaim as the unverified block row above.
    lookup_made = bool(result and (result.verified or result.proof_mismatch))

    # Stale-incomplete disclosure (mirrors `cairn upgrade`'s warning and its threshold,
    # `CAIRN_INCOMPLETE_PROOF_ALARM_DAYS`, so the panel and the CLI can't disagree about when a
    # submitted proof has waited too long). "Usually settles within a few hours" is true of a proof
    # submitted this morning and false — reassuring noise over a real stuck stamp — of one
    # submitted in March.
    stamped_days: int | None = None
    if fe.ots_stamped_at is not None:
        stamped_at = fe.ots_stamped_at
        if stamped_at.tzinfo is None:
            stamped_at = stamped_at.replace(tzinfo=timezone.utc)
        stamped_days = max(0, int((datetime.now(timezone.utc) - stamped_at).total_seconds() // 86400))

    ctx = {
        "file_id": file_id,
        "filename": Path(fe.relpath).name,
        "relpath": fe.relpath,
        "collection": collection.name,
        "collection_id": collection.id,
        "sha256": digest or "(unknown)",
        # The live re-hash, or nothing. A card with no live digest must not present the recorded
        # baseline in the slot labelled "the file's fingerprint" — nothing was hashed this check.
        "live_digest": bool(digest),
        # Shown INSTEAD, explicitly labelled, when the live bytes could not be read: "(unknown)"
        # threw away the one fact Cairn does hold about a file that is gone.
        "baseline_sha256": (fe.sha256 or None),
        "verdict": verdict,
        "title": title,
        # Why no live bytes were hashed (missing / unreadable), and what the file row itself says.
        # The card owes an explicit account of "nothing was compared" here; without these it fell
        # through to the generic fallback, which speculates that the contents may have changed —
        # the one thing this check established nothing about.
        "live_unavailable": live_unavailable,
        "file_status": fe.status,
        "verified": bool(result and result.verified),
        # Whether there is a proof to offer for download at all (the export route 409s without one).
        "has_proof": bool(fe.ots_path),
        # With no `VerifyResult` there is no proof to describe, so the state comes from the file
        # row — that is what the two "no proof yet" verdicts above are reasoning about.
        "ots_state": result.state if result else fe.ots_state,
        # Reason flags travel on every branch, not only the one that won: a transport failure under
        # a verdict that outranks it is still disclosed as a diagnostic line (design D2).
        "digest_mismatch": bool(result and result.digest_mismatch),
        # Which artifact the digest disagreement is attributed to:
        # "file" | "proof-stale" | "proof" | "unknown". Never derived in the template — the
        # template has no access to the recorded baseline or the row's state.
        "mismatch_blame": mismatch_blame,
        "proof_mismatch": bool(result and result.proof_mismatch),
        "unreadable_proof": bool(result and result.unreadable_proof),
        "transport_error": (result.transport_error if result else None),
        "failed_lookups": ots_svc.failed_lookup_count(result),
        "inconclusive": bool(result and result.inconclusive),
        "existed_by": result.existed_by if result else None,
        # Provenance the card may present as CONFIRMED only when `verified` is true. On any other
        # branch this is what the proof *claims* (read offline from the proof itself), unconfirmed
        # against the Bitcoin record — and on a changed file it belongs to the digest the proof was
        # made from, not to the live fingerprint shown beside it (design D1, BLOCKER 3).
        "block_height": result.block_height if result else None,
        "block_hash": block_hash,
        "calendars": result.calendars if result else [],
        "verified_via": verified_via,
        "lookup_made": lookup_made,
        "stamped_at": humanize_date(fe.ots_stamped_at),
        "stamped_days": stamped_days,
        "stamp_stale": (
            stamped_days is not None and stamped_days >= settings.incomplete_proof_alarm_days
        ),
        "message": (
            live_unavailable
            if live_unavailable is not None
            else (result.message if result else "no proof stored for this file yet")
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

# Shown (labelled as an example) only while no panel address is configured. Cairn never guesses its
# own address: an address presented as real but wrong is worse than one openly marked illustrative.
EXAMPLE_HEALTHZ_URL = "https://cairn.example.com/healthz"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    tab: str = Query("notifications"),
    saved: str | None = Query(None),
    test: str | None = Query(None),
    msg: str | None = Query(None),
    val: str | None = Query(None),
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
            # The panel's own public address: what alert deep links are built from, and what the
            # health-monitoring URL below is derived from. Blank when unconfigured — in that case
            # the template shows an address labelled as an example, never as the real one.
            # A rejected submission comes back in `val` so the operator can fix the typo instead of
            # retyping a long URL from scratch; the *stored* value is untouched either way.
            "public_url": (val if saved == "urlerr" and val is not None else eff.public_url or ""),
            "healthz_url": panel_link(eff.public_url, "/healthz") or EXAMPLE_HEALTHZ_URL,
            "healthz_url_is_example": eff.public_url is None,
            # The effective address alone cannot tell env from a saved override, and the difference
            # decides whether editing CAIRN_PUBLIC_URL will still do anything. Name the source.
            "public_url_effective": eff.public_url or "",
            "public_url_is_override": await app_settings_svc.public_url_override_is_set(session),
            "public_url_env": settings.public_url or "",
            "public_url_saved": saved == "url",
            "public_url_cleared": saved == "urlcleared",
            "public_url_error": msg if saved == "urlerr" else "",
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
            # Verification backend is deliberately env-only and read from `settings`, never from
            # the DB overlay: the panel and `cairn verify` must never disagree about how an
            # integrity claim was verified, so the tab describes the environment rather than
            # offering an override (#34).
            "verify_backend": settings.verify_backend,
            "explorer_host": explorer_host,
            "node_rpc_url": settings.node_rpc_url or "",
            "calendars": [
                c.replace("https://", "").replace("http://", "").rstrip("/")
                for c in settings.ots_calendars
            ],
        }
    )
    return templates.TemplateResponse(request, "settings.html", ctx)


def _require_admin(user: User) -> None:
    """App-wide config (SMTP server, panel address) is admin-only (the sole user is admin in single mode)."""
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


@router.post("/settings/panel-url", dependencies=[Depends(verify_csrf)])
async def settings_panel_url_save(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    public_url: str = Form(""),
):
    """Save the panel's externally-reachable base URL (admin-only, app-wide like the SMTP config).

    This is the **fail-loud** half of the setting's validation: a human is present to read the
    error, so a malformed address is refused here with the reason shown inline and the stored value
    left untouched. (At config-load time the same value is fail-soft — a typo must never cost a
    scan or an alert.) An empty value clears the override, which *deletes* the row so
    ``CAIRN_PUBLIC_URL`` becomes visible again.
    """
    from urllib.parse import quote

    _require_admin(user)
    try:
        await app_settings_svc.save_public_url(session, public_url)
    except ValueError as exc:
        # Carry the rejected input back so it can be corrected in place (F4); it is re-escaped by
        # the template, and it never reaches the stored value.
        return RedirectResponse(
            "/settings?tab=notifications&saved=urlerr"
            f"&msg={quote(str(exc)[:200])}&val={quote(public_url[:300])}",
            status_code=303,
        )
    # An empty save is a *clear*, not a set — and the field then repopulates from the environment,
    # which reads as "it came back" unless the message says what it fell back to.
    outcome = "url" if public_url.strip() else "urlcleared"
    return RedirectResponse(f"/settings?tab=notifications&saved={outcome}", status_code=303)


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
        # No specific collection is involved, so point at the collections list. The test exists to
        # prove the configured address is reachable *before* a real incident depends on it; with no
        # panel address configured this stays None and the mail is link-free, exactly as an alert
        # would be.
        url=panel_link(eff.public_url, "/collections"),
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
