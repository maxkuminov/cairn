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
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, literal, null, select, text, union_all
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..csrf import generate_csrf_token, verify_csrf
from ..database import get_session
from ..models.db import Collection, Event, FileEntry, Run, User
from ..services import app_settings as app_settings_svc
from ..services import collections as collections_svc
from ..services import proofs as proofs_svc
from ..services import rules as rules_svc
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
    """The number of open issues: files that are missing **or** modified.

    THE definition of "outstanding work" (unify-issue-model D1). The sidebar badge and the
    dashboard's needs-you total both come from here, through ``_base_context``, so they cannot
    disagree; nothing else in the panel counts work waiting for the operator, and in particular
    nothing counts unacknowledged events.
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


def _collection_status(counts: dict[str, int]) -> str:
    # `new` files are informational: a newly-tracked, unmodified file is the happy path, not work
    # for the operator. Only a `modified` file raises "attention"; a `missing` file raises "alert".
    # A collection whose only non-ok files are `new` reads "All clear".
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
    "attention": ("Changed", "var(--warn)", "alert", "warn"),
    "alert": ("Missing", "var(--danger)", "minusCircle", "danger"),
    "empty": ("No files indexed", "var(--text-3)", "folder", "muted"),
}


def _status_label(status: str, counts: dict[str, int]) -> str:
    """What the status pill says: the fact, not a grade (design D11).

    "Attention" and "Alert" left the operator to guess which kind of problem a collection had;
    the pill now states how many files changed and how many are missing.
    """
    if status == "alert":
        label = f"{counts['missing']:,} missing"
        if counts["modified"] > 0:
            label += f" · {counts['modified']:,} changed"
        return label
    if status == "attention":
        return f"{counts['modified']:,} changed"
    return _STATUS_META[status][0]


# The two change policies in the operator's words (design D5). The stored values stay
# `worm` / `churn`; no template prints them. `flags` is the accurate statement of what each
# policy raises: Working is NOT "deletions only" — a file that comes back different after going
# missing is flagged under both.
_POLICY = {
    "worm": {
        "label": "Locked",
        "short": "Nothing here should change",
        "flags": "Every change and every deletion is flagged.",
    },
    "churn": {
        "label": "Working",
        "short": "Edits are normal",
        "flags": (
            "Ordinary edits are not flagged. Deletions are, and so are a file that comes back "
            "different after going missing, possible corruption (contents changed while size and "
            "date did not), and an unusually large change in one scan."
        ),
    },
}


def _policy_view(mode: str, rules: int = 0) -> dict[str, Any]:
    """A policy in the operator's words. ``rules`` is how many path rules the collection carries:
    with any, "every change is flagged" is no longer true of a Locked collection, so the statement
    of what the policy flags is qualified rather than left standing beside the rule count."""
    view: dict[str, Any] = {"value": mode, **_POLICY.get(mode, _POLICY["worm"]), "rules": rules}
    if rules and view["value"] != "churn":
        view["flags"] = (
            "Every deletion is flagged, and every change — except ordinary changes to the files "
            f"its {rules} rule{'' if rules == 1 else 's'} cover{'s' if rules == 1 else ''}."
        )
    return view


def _rules_count(collection: Collection) -> int:
    """How many recognised path rules a collection carries (parsed from its own row, no query)."""
    return len(rules_svc.load(collection))

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
    status = _collection_status(counts)
    meta = _STATUS_META[status]
    run = await collections_svc.active_run(session, collection.id)
    return {
        "id": collection.id,
        "status_kind": meta[3],
        "status_icon": meta[2],
        "status_label": _status_label(status, counts),
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


def _decode_error_sample(raw: str | None) -> list[str]:
    """Decode `runs.error_sample` for display; never raise, never hand back a non-string element.

    The column is written by the scanner as an ASCII-only JSON array of strings, but a render path
    must not become the place a malformed or hand-edited value takes the page down — an unreadable
    diagnostic is worth strictly less than the page that reports the partial scan.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [entry for entry in parsed if isinstance(entry, str)]


async def _collection_view(session: AsyncSession, collection: Collection) -> dict[str, Any]:
    counts = await _collection_counts(session, collection.id)
    ots = await _ots_counts(session, collection.id)
    total_files = sum(counts.values())
    total_size = await session.scalar(
        select(func.coalesce(func.sum(FileEntry.size), 0)).where(
            FileEntry.collection_id == collection.id
        )
    )
    # TWO deliberately separate reads (design D7), because they answer two different questions.
    #
    # `last_run` — the newest COMPLETED scan (`ok`/`partial`). This is what every "Last scan …"
    # claim on the panel is derived from, and the filter must NOT be widened to include
    # `interrupted`/`error`: a sentence beginning "Last scan" can only honestly mean "when a scan
    # last finished", and letting a run that never finished refresh it is exactly the false
    # negative #29 complains about, reintroduced by its own fix. (It is deliberately narrower than
    # the dead-man's-switch freshness in `compute_health`, which additionally honours a live
    # in-flight scan: the switch asks "is this collection still being watched".)
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
    # `latest_terminal` — the newest scan run in ANY terminal state. It never feeds "last scan"; it
    # exists so that something which happened AFTER the last completed scan, and which the operator
    # can otherwise not see at all, is disclosed. Only used when it is newer than `last_run`.
    latest_terminal = await session.scalar(
        select(Run)
        .where(
            Run.collection_id == collection.id,
            Run.kind == "scan",
            Run.result.in_(("ok", "partial", "error", "interrupted")),
        )
        .order_by(Run.finished.desc().nulls_last())
        .limit(1)
    )
    # Disclosed only when it is a LATER, non-completing run than the last completed scan — an
    # `interrupted`/`error` that the "Last scan" line cannot express. `None` otherwise, including
    # when the newest terminal run IS the last completed scan.
    latest_run_result = None
    # Why that later run failed — taken from THAT run, never from the last completed scan's
    # diagnostics: a refused scan (folder unreachable, or empty while files are tracked) records a
    # sentence the operator can act on.
    latest_run_reason = None
    if latest_terminal is not None and latest_terminal.result in ("error", "interrupted"):
        if last_run is None or latest_terminal.id != last_run.id:
            latest_run_result = latest_terminal.result
            if latest_terminal.result == "error":
                latest_run_reason = scanner_svc.refusal_reason(latest_terminal)
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
    return {
        "id": collection.id,
        "name": collection.name,
        "op": _op_view(active) if active else None,
        "root": collection.root,
        "mode": collection.mode,
        "policy": _policy_view(collection.mode, _rules_count(collection)),
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
        "file_count": total_files,
        "file_count_h": humanize_count(total_files),
        "size_bytes": int(total_size or 0),
        "size": humanize_size(total_size),
        "status": status,
        "status_label": _status_label(status, counts),
        "status_color": meta[1],
        "status_icon": meta[2],
        "status_kind": meta[3],
        "issues": counts["modified"] + counts["missing"],
        "last_scan": humanize_delta(last_run.finished) if last_run else "never",
        # The last COMPLETED scan's own result, plus what it skipped. `partial` must never render
        # identically to a clean scan: "I checked" shown where the truth is "I checked most of them"
        # is the false assurance this product cannot afford.
        "last_result": last_run.result if last_run else None,
        "last_errors": (last_run.errors if last_run else 0) or 0,
        # A bounded list of DIAGNOSTIC RENDERINGS (see `runs.error_sample`) — never paths, never
        # offered as a copyable path list, never fed to a filesystem call.
        "last_error_sample": _decode_error_sample(last_run.error_sample if last_run else None),
        "latest_run_result": latest_run_result,
        "latest_run_reason": latest_run_reason,
        # The last completed scan could not list some folders: what it skipped is not "files".
        "last_walk_incomplete": any(
            entry.startswith(f"{scanner_svc.WALK_UNREADABLE}:")
            for entry in _decode_error_sample(last_run.error_sample if last_run else None)
        ),
        "last_scan_full": (
            last_run.finished.strftime("%Y-%m-%d %H:%M UTC") if last_run and last_run.finished
            else "no completed scans yet"
        ),
    }


async def _attach_health_state(
    session: AsyncSession, user: User, views: list[dict[str, Any]]
) -> None:
    """Stamp each collection view with its scan-freshness state, from ONE owner-scoped report.

    The health pill names a count and links to ``/collections``; the per-card stale markers are what
    identify the collections behind that count once the operator arrives. Both therefore have to be
    computed over the same population, by one call per render — which is why this takes the views
    and mutates them rather than letting each card ask for its own answer (design D5).

    Matched by **id**, never by name: no constraint makes a collection name unique across owners, so
    a name match could attach one owner's stale marker to another owner's identically-named card.
    A view with no freshness record is left untouched (the marker simply does not render).
    """
    from ..services.scheduler import compute_health

    report = await compute_health(session, get_settings(), user_id=user.id)
    by_id = {row.id: row.state for row in report.collections}
    for view in views:
        view["health_state"] = by_id.get(view["id"])


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
async def health_pill(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """The polled health indicator: the viewer's OWN health, and the count behind the verdict.

    Owner-scoped (design D5). The pill states a number and then links to ``/collections``, which is
    ``list_collections(user_id=…)`` — so a fleet-global count rendered above it would name
    collections that do not appear on the page it sends the operator to. That is the very
    "computes one thing, shows another" defect this indicator is being fixed for, manufactured by
    its own fix. ``/healthz`` stays fleet-global; it monitors the installation, not a viewer.

    Both the verdict and the count come from the SAME :class:`HealthReport` — never a second query,
    which could disagree with the first.

    **It fails CLOSED.** htmx does not swap a 4xx/5xx response, so a poll that raises leaves the
    previously rendered verdict standing — and the verdict most likely to be standing is "Healthy".
    A green pill that survives the datastore going down is precisely the false assurance this
    indicator was rebuilt to remove, and it would be indefinite: every subsequent poll fails the
    same way. So a failure here is answered with a rendered ``error`` pill (HTTP 200, so the swap
    happens) rather than propagated. The transport and dependency failures this handler never sees —
    a network drop, a timeout, a failing ``get_session``/``current_user`` dependency, a 500 from
    anywhere above — are caught by the pill's own htmx error hooks (``partials/health_pill.html``),
    which neutralize the standing verdict client-side. Server and client cover disjoint halves of
    the same rule: **no poll failure may leave a health claim on screen.**
    """
    from ..services.scheduler import compute_health

    settings = get_settings()
    try:
        report = await compute_health(session, settings, user_id=user.id)
    except Exception:
        logging.getLogger("cairn.panel").warning(
            "health pill: health computation failed — rendering the failed state", exc_info=True
        )
        return templates.TemplateResponse(
            request, "partials/health_pill.html", {"status": "error", "stale_count": 0}
        )
    return templates.TemplateResponse(
        request,
        "partials/health_pill.html",
        {
            "status": report.status,
            "stale_count": sum(1 for c in report.collections if c.state == "stale"),
        },
    )


# --- dashboard ------------------------------------------------------------------------------

# Recent activity is a SUMMARY of the event log, not a feed of it (design D10): one line per day
# and collection. It is read-only by construction — it renders no control — and it includes the
# informational kinds, so a healthy system never shows an empty summary.
ACTIVITY_DAYS = 30
ACTIVITY_LINES = 12
# (event kind, singular, plural, alarming) in the order a line states them: what needs attention
# first. `restored_changed` is named for what it is — never folded into "restored".
_ACTIVITY_KINDS: tuple[tuple[str, str, str, bool], ...] = (
    ("restored_changed", "came back different", "came back different", True),
    ("corrupted", "possibly corrupted", "possibly corrupted", True),
    ("missing", "missing", "missing", True),
    ("modified", "changed", "changed", True),
    ("added", "added", "added", False),
    ("moved", "moved", "moved", False),
    ("restored", "restored", "restored", False),
)


async def _recent_activity(
    session: AsyncSession, collections: list[Collection]
) -> list[dict[str, Any]]:
    """The event log of the last ``ACTIVITY_DAYS`` days, aggregated by local day and collection."""
    names = {c.id: c.name for c in collections}
    if not names:
        return []
    since = datetime.now(timezone.utc) - timedelta(days=ACTIVITY_DAYS)
    day = func.date(Event.detected_at, "localtime")
    result = await session.execute(
        select(day, Event.collection_id, Event.kind, func.count())
        .where(Event.collection_id.in_(list(names)), Event.detected_at >= since)
        .group_by(day, Event.collection_id, Event.kind)
    )
    buckets: dict[tuple[str, int], dict[str, int]] = {}
    for day_s, cid, kind, n in result.all():
        buckets.setdefault((day_s or "", cid), {})[kind] = int(n)
    today = datetime.now().astimezone().date()
    lines = []
    # Newest day first; within a day, collections by name (two stable sorts).
    ordered = sorted(buckets.items(), key=lambda kv: names.get(kv[0][1], ""))
    ordered.sort(key=lambda kv: kv[0][0], reverse=True)
    for (day_s, cid), kinds in ordered:
        try:
            label = _day_label(date.fromisoformat(day_s), today)
        except ValueError:
            label = day_s or "unknown"
        parts = [
            {"text": f"{kinds[k]:,} {sing if kinds[k] == 1 else plur}", "alarm": alarm}
            for k, sing, plur, alarm in _ACTIVITY_KINDS
            if kinds.get(k)
        ]
        if not parts:
            continue
        lines.append(
            {
                "day": label[:1].upper() + label[1:],
                "collection_id": cid,
                "collection": names.get(cid, "—"),
                "parts": parts,
            }
        )
        if len(lines) >= ACTIVITY_LINES:
            break
    return lines


async def _needs_you(
    session: AsyncSession, collections: list[Collection], views: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The collections with changed or missing files, most deserving first (design D10).

    One population read per AFFECTED collection (the cheap status counts already in ``views``
    decide which), ordered exactly like the fleet review page. It carries links only — no form —
    so it mints no fingerprints and nothing here can authorise an action.
    """
    by_id = {c.id: c for c in collections}
    entries = []
    for v in views:
        if v["issues"] <= 0:
            continue
        c = by_id[v["id"]]
        pop = await _read_population(session, c)
        if not pop.files:
            continue  # resolved between the two reads
        index = _PopIndex(pop)
        found = {f.id: _found_at(f, index) for f in pop.files}
        newest = max((d for d in found.values() if d is not None), default=None)
        missing = sum(1 for f in pop.files if f.status == "missing")
        groups = _group_scopes(pop.files, _ROOT_SCOPE)
        groups.sort(key=lambda g: (-len(g[1]), g[0].dir))
        folders = []
        if len(groups) > 1:
            folders = [
                {"label": _group_label(g, _ROOT_SCOPE), "count": len(members)}
                for g, members in groups[:2]
            ]
        elif groups:
            common = groups[0][0].dir
            if common:
                folders = [{"label": common + "/", "count": len(groups[0][1])}]
        entries.append(
            {
                "id": c.id,
                "name": c.name,
                "policy": _policy_view(c.mode, _rules_count(c)),
                "missing": missing,
                "modified": len(pop.files) - missing,
                "total": len(pop.files),
                "found": humanize_delta(newest),
                "folders": folders,
                "more_folders": max(0, len(groups) - len(folders)) if len(groups) > 1 else 0,
                "_key": _severity_key(c, newest),
            }
        )
    entries.sort(key=lambda e: e.pop("_key"))
    return entries


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    collections = await collections_svc.list_collections(session, user_id=user.id)
    views = [await _collection_view(session, c) for c in collections]
    await _attach_health_state(session, user, views)

    total_files = sum(v["file_count"] for v in views)
    total_size = sum(v["size_bytes"] for v in views)

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

    needs = await _needs_you(session, collections, views)
    activity = await _recent_activity(session, collections)

    collection_ids = [c.id for c in collections]
    last_run = None
    if collection_ids:
        last_run = await session.scalar(
            select(Run)
            .where(Run.collection_id.in_(collection_ids), Run.finished.is_not(None))
            .order_by(Run.finished.desc())
            .limit(1)
        )
    last_collection = ""
    last_activity_sub = "no activity yet"
    if last_run is not None:
        c = await session.get(Collection, last_run.collection_id)
        last_collection = c.name if c else ""
        # The tile summarises the newest finished run of ANY kind, so it must say what it found
        # (design D14 of add-fleet-review-and-run-health): a `stamp` pass, last night's `upgrade`,
        # a `partial` scan that skipped files, a failed scan and a run reclaimed by the reaper must
        # not render identically to a clean scan.
        kind = last_run.kind or "scan"
        last_activity_sub = f"{last_collection} {kind}" if last_collection else f"last {kind}"
        # `interrupted` is worded neutrally and NEVER as a fault: it is what restarting the app
        # mid-scan produces, an ordinary outcome, not an integrity event.
        _RESULT_WORDS = {"partial": "partial", "error": "failed", "interrupted": "interrupted"}
        if last_run.result != "ok":
            last_activity_sub += f" · {_RESULT_WORDS.get(last_run.result, last_run.result)}"
        if last_run.moved:
            last_activity_sub += f" · {last_run.moved} moved"

    ctx = await _base_context(request, session, user, "dashboard")
    ctx.update(
        {
            "collections": views,
            "needs": needs,
            # The SAME number the sidebar badge shows, from the same definition
            # (`_alert_badge_count`, already in the base context) — never a second sum.
            "needs_total": ctx["alert_count"],
            "needs_missing": sum(v["counts"]["missing"] for v in views),
            "needs_modified": sum(v["counts"]["modified"] for v in views),
            "any_files": total_files > 0,
            "activity": activity,
            "tiles": {
                "files": humanize_count(total_files),
                "files_sub": f"{len(views)} collection{'' if len(views) == 1 else 's'} · {humanize_size(total_size)}",
                "anchored": humanize_count(total_anchored),
                "anchored_sub": anchored_sub,
                "last_activity": humanize_delta(last_run.finished) if last_run else "—",
                "last_activity_sub": last_activity_sub,
            },
        }
    )
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/collections", response_class=HTMLResponse)
async def collections_list(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
):
    """Dedicated collections list page (the left-nav 'Collections' target).

    This is where the health pill's link lands, so it is where the stale collections it counted have
    to be identifiable — hence the per-card freshness marker, from the same owner-scoped health
    computation the pill itself used.
    """
    collections = await collections_svc.list_collections(session, user_id=user.id)
    views = [await _collection_view(session, c) for c in collections]
    await _attach_health_state(session, user, views)
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
        if await collections_svc.blocking_run(session, c.id) is not None:
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
        "policies": [_policy_view("worm"), _policy_view("churn")],
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
    if await collections_svc.blocking_run(session, collection_id) is not None:
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
# Accepting is irreversible: it sets a changed file healthy, or DELETEs a missing file's record.
# Every route that does it renders a page first and acts at submit time, and the scheduler can
# complete a scan in between — so a button labelled "Accept 40 deletions" can become the removal of
# records the operator never saw. Neither a render-time visibility rule nor a submit-time recount
# closes that: the recount is a separate statement from the accept, and the same scan can claim,
# run and commit in the gap.
#
# The fix is one mechanism used identically by every accept route: each form carries a hidden
# SHA-256 of a canonical description of the population it claims to act on, and the POST recomputes
# it INSIDE the same write transaction as the accept's own reads and writes. SQLite's single-writer
# serialization is what makes the check-and-act atomic.

# US separates fields, RS separates records. The byte-length prefix on `relpath` (and on a folder
# scope's `dir`) is what makes the encoding injective: a name may legally contain any byte but `/`
# and NUL — US and RS included — so without the length two different populations could be made to
# hash equal. Framing by length removes that by construction rather than by escaping.
_FP_US = "\x1f"
_FP_RS = "\x1e"
# GS/FS do the same framing one level down, inside the header's open-event component: GS between
# event records, FS between an event's fields.
_FP_GS = "\x1d"
_FP_FS = "\x1c"

# The one READ a review surface makes: every `missing` and `modified` file of the collection plus
# its open events, in a single statement, so the page's rows, its counts and every fingerprint it
# publishes come from one snapshot.
_FP_READ_STATUSES: tuple[str, ...] = ("missing", "modified")

# A **form scope** is what a fingerprint is minted *for*. It is the first field of the preimage
# header, so a fingerprint minted for one verb can never validate another. Each entry names the
# file statuses its own population covers; `_narrow` does the slicing, purely, on the mint side and
# the recount side alike. The names are the route constants and are deliberately unchanged across
# the rename of the operator-facing verbs ("Accept change" / "Accept deletion"): they appear in the
# access log, and one address must keep meaning one consequence.
_FP_FORMS: dict[str, tuple[str, ...]] = {
    "adopt-changed": ("modified",),
    "stop-tracking": ("missing",),
    # A per-file accept names its one record by `file_id`, not by the status tuple.
    "accept-file": ("missing", "modified"),
}
# The two bulk forms, which additionally carry a folder scope (design D6).
_FP_BULK_FORMS = frozenset({"adopt-changed", "stop-tracking"})

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


# --- folder scope (design D6) ---------------------------------------------------------------


class _Scope(NamedTuple):
    """Which part of a collection a bulk verb (or a review page) covers.

    ``dir`` is a collection-relative directory with no leading or trailing slash (``""`` = the
    collection root). ``deep`` selects the directory's whole subtree; ``False`` selects only the
    files directly in it. The pair travels in a form or a query string, so it is operator-controlled
    bytes: it is never trusted to select anything on its own — it is bound into the fingerprint and
    recomputed against (:func:`_narrow`), and it never reaches the filesystem.
    """

    dir: str
    deep: bool

    @property
    def is_root(self) -> bool:
        return self.dir == "" and self.deep


_ROOT_SCOPE = _Scope("", True)
_SCOPE_MAX_BYTES = 4096


def _parse_scope(dir_raw: str | None, deep_raw: str | None) -> _Scope | None:
    """Parse a submitted scope; ``None`` means "not a scope any page could have rendered".

    Refused rather than repaired: an absolute path, a trailing slash, a NUL, an over-long value or
    an unknown ``deep`` flag is not something this panel emits, so guessing what was meant would
    put an interpretation step between the operator's click and a destructive verb.
    """
    d = dir_raw or ""
    try:
        encoded = d.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _SCOPE_MAX_BYTES or "\x00" in d:
        return None
    if d.startswith("/") or d.endswith("/"):
        return None
    if deep_raw not in ("0", "1"):
        return None
    return _Scope(d, deep_raw == "1")


def _in_scope(relpath: str, scope: _Scope) -> bool:
    """THE folder rule — the only place it is written (design D6).

    Literal, case-sensitive string comparison on the stored relpath: no pattern matching, so a
    folder named ``100%`` or ``a_b`` matches only itself, and ``foo`` never matches ``foobar/x``
    because the separator is part of the prefix.
    """
    if scope.dir:
        prefix = scope.dir + "/"
        if not relpath.startswith(prefix):
            return False
        rest = relpath[len(prefix):]
    else:
        rest = relpath
    return scope.deep or "/" not in rest


def _scope_query(scope: _Scope) -> str:
    """The query string that re-opens a review page at ``scope`` (empty for the whole collection).

    Built with ``urlencode`` on a fixed path by every caller, so a submitted ``dir`` only ever
    becomes a percent-encoded query *value* — never a host, a path segment or a second parameter.
    """
    if scope.is_root:
        return ""
    params = {"dir": scope.dir}
    if not scope.deep:
        params["deep"] = "0"
    return urlencode(params)


class _PopFile(NamedTuple):
    """One file row of an accept-family population.

    Carries BOTH the fields the guard hashes (``id``/``relpath``/``status``/``sha256``/
    ``first_seen``) and the fields the review page renders, because they come from one fetch: see
    :func:`_read_population`.
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
    mtime: float | None
    # The size on record before an unaccepted change (display only — never hashed).
    prior_size: int | None


class _PopEvent(NamedTuple):
    """One open event, by identity and generation — never by tally (see `_population_fingerprint`).

    ``file_id`` is **not** hashed. It is what :func:`_narrow` filters on, so a verb's event term
    covers exactly the alerts that verb will close and no others. ``None`` marks a *detached* event,
    whose file record an earlier accept removed: it belongs to no narrowed population.
    """

    id: int
    kind: str
    detected_at: datetime | None
    file_id: int | None


class _Population(NamedTuple):
    """Everything an accept-family form displays and hashes, read as ONE statement.

    ``files`` is sorted by ``relpath`` (total order — unique within a collection), ``open_events``
    by ``id``. ``total_files`` is the collection's whole file population (every status) from this
    same snapshot: it is what tells "nothing is wrong" apart from "nothing is watched".
    """

    scope: str
    files: list[_PopFile]
    open_events: list[_PopEvent]
    total_files: int
    # Set by `_narrow` for the `accept-file` form only: the row the form was minted for, carried
    # into the preimage header so a fingerprint cannot validate a POST at another row's address.
    file_id: int | None = None
    # Set by `_narrow` for the two bulk forms: the folder the form was minted for (design D6).
    folder: _Scope | None = None


class _PopIndex:
    """Lookups over one snapshot, built once per page so minting N row fingerprints is O(N).

    Purely an access path: it holds the same tuples the snapshot holds and changes nothing about
    what is selected. Without it every row's `_narrow` rescans the whole population, which on a
    collection whose volume went away is hundreds of rows times hundreds of thousands of records.
    """

    def __init__(self, pop: _Population) -> None:
        self.files_by_id: dict[int, _PopFile] = {f.id: f for f in pop.files}
        self.events_by_file: dict[int, list[_PopEvent]] = {}
        for e in pop.open_events:  # id-ordered, so each per-file list stays id-ordered
            if e.file_id is not None:
                self.events_by_file.setdefault(e.file_id, []).append(e)

    def newest_event(self, file_id: int, kind: str | None = None) -> _PopEvent | None:
        """The file's newest open event (of ``kind``, if given) — highest id is the newest."""
        events = self.events_by_file.get(file_id) or ()
        for event in reversed(events):
            if kind is None or event.kind == kind:
                return event
        return None


async def _read_population(
    session: AsyncSession,
    collection: Collection,
    file_id: int | None = None,
) -> _Population:
    """Read a collection's whole review population in a SINGLE SQL statement.

    The mint side and the recount side both go through here, so the two can never encode the same
    state differently — and, on the mint side, the rows the page *renders* are sliced from the very
    list that is hashed. That is not a tidiness point. Python's ``sqlite3`` runs in legacy
    transaction mode: a ``SELECT`` does not open a transaction, so two consecutive ``SELECT``s on
    one connection are two independent snapshots and a scanner can commit between them. A review
    page that read its visible rows in one statement and minted the fingerprint in another could
    therefore publish a fingerprint for a population it never displayed — an unchanged POST would
    then validate and delete a ``missing`` row the operator never saw. One statement is one
    snapshot, by construction.

    The union is the price of that: the file rows, the open-event rows and the file total live in
    different tables/shapes, so they are padded to one 12-column row shape and tagged in the first
    column. SQLAlchemy takes result types from the first leg, so the events' ``detected_at`` is
    decoded by ``first_seen``'s ``DateTime`` processor (same column type, same storage format).
    """
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
        FileEntry.mtime.label("f1"),
        FileEntry.prior_size.label("n3"),
    ).where(
        FileEntry.collection_id == collection.id,
        FileEntry.status.in_(_FP_READ_STATUSES),
    )
    if file_id is not None:
        # The `accept-file` RECOUNT only: reading a whole collection's issue set to hash one row is
        # waste. This narrows which rows are *fetched*, never how they are selected once fetched.
        files_q = files_q.where(FileEntry.id == file_id)
    # `Event.file_id` rides the `n2` slot, which the file leg fills with `FileEntry.size` — an
    # integer column either way. It is what `_narrow` filters the event term on; it never enters
    # the preimage.
    events_q = select(
        literal("e"),
        Event.id,
        Event.kind,
        null(),
        null(),
        Event.detected_at,
        Event.file_id,
        null(),
        null(),
        null(),
        null(),
        null(),
    ).where(Event.collection_id == collection.id, Event.acknowledged_at.is_(None))
    # The collection's entire file population, counted in the same snapshot as the rows above so a
    # concurrent accept cannot empty the collection between "are there issues?" and "is this
    # collection watching anything at all?".
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
            null(),
            null(),
        )
        .select_from(FileEntry)
        .where(FileEntry.collection_id == collection.id)
    )

    files: list[_PopFile] = []
    events: list[_PopEvent] = []
    total_files = 0
    for row in (await session.execute(union_all(files_q, events_q, total_q))).all():
        part = row[0]
        if part == "f":
            files.append(_PopFile(*row[1:12]))
        elif part == "e":
            events.append(_PopEvent(row[1], row[2], row[5], row[6]))
        else:
            total_files = int(row[1] or 0)

    files.sort(key=lambda f: f.relpath)
    events.sort(key=lambda e: e.id)
    return _Population(scope="review", files=files, open_events=events, total_files=total_files)


def _narrow(
    pop: _Population,
    form: str,
    file_id: int | None = None,
    folder: _Scope | None = None,
    index: _PopIndex | None = None,
) -> _Population:
    """Slice one wide read down to the population a SINGLE form acts on. Pure — it reads nothing.

    This is the only narrowing there is. The mint side (which needs the whole review set once,
    because the page renders it, groups it, counts it and mints many fingerprints from it) and the
    recount side both go through here, so the two cannot encode the same state differently.

    * ``accept-file`` — the one record named by ``file_id`` (whatever its status), and its events.
    * ``adopt-changed`` / ``stop-tracking`` — every file in the form's status **inside ``folder``**
      (:func:`_in_scope`; the whole collection when ``folder`` is the root scope), and the open
      events of exactly those files: the alerts the verb will close. Binding the event term to the
      collection's whole set instead would refuse a submission over drift the verb cannot reach (an
      alert opening on a file in another state or another folder), and refusals an operator cannot
      account for are how a guard gets worked around.

    A detached event (``file_id IS NULL``) is in no narrowed term: no verb closes it.
    ``index`` is an optional access path for the per-row case; it never changes the result.
    """
    statuses = _FP_FORMS[form]
    if file_id is not None:
        if index is not None:
            one = index.files_by_id.get(file_id)
            files = [one] if one is not None else []
            events = list(index.events_by_file.get(file_id, ())) if one is not None else []
        else:
            files = [f for f in pop.files if f.id == file_id]
            ids = {f.id for f in files}
            events = [e for e in pop.open_events if e.file_id is not None and e.file_id in ids]
        return _Population(
            scope=form, files=files, open_events=events, total_files=pop.total_files,
            file_id=file_id,
        )

    scope = folder if folder is not None else _ROOT_SCOPE
    files = [f for f in pop.files if f.status in statuses and _in_scope(f.relpath, scope)]
    ids = {f.id for f in files}
    events = [e for e in pop.open_events if e.file_id is not None and e.file_id in ids]
    return _Population(
        scope=form, files=files, open_events=events, total_files=pop.total_files,
        folder=scope if form in _FP_BULK_FORMS else None,
    )


def _population_fingerprint(collection: Collection, pop: _Population) -> str:
    """Hex SHA-256 of D14's canonical encoding of one narrowed population.

    Pure: it hashes the rows it is handed and reads nothing. That is what lets the mint side hash
    exactly the rows it rendered, and the recount side re-derive the same encoding from its own
    single read under the write lock — one code path, two callers.

    Preimage = header + RS + RS.join(records), UTF-8 encoded.

    * header — ``{form}US{collection_id}US{created_at}USopen_events={events}`` and then, per form,
      ``USfile={id}`` (``accept-file``, so the row is named by the header as well as by its record)
      or ``USdir={byte length}US{dir}USdeep={0|1}`` (the bulk forms, so a fingerprint minted for one
      folder cannot validate a submission naming another even where the two hold the same files —
      design D6).
    * ``events`` — GS-joined, id-ordered ``{id}FS{kind}FS{detected_at}`` for every open event of the
      population's files; empty when there are none.
    * record — ``{id}US{len(relpath_bytes)}US{relpath}US{status}US{sha256 or ''}US{first_seen}``,
      one per file, sorted by ``relpath`` (unique within a collection, so the order is total).
      Deliberately **not** sorted by ``id``: ``id`` is the field this encoding distrusts.

    Why each field is there. ``files.id`` / ``collections.id`` are ``INTEGER PRIMARY KEY`` *without*
    ``AUTOINCREMENT``, so SQLite may hand a deleted row's id to a later insert — an
    id-and-status-only preimage is byte-identical for two populations sharing no file. ``relpath``
    pins the logical file, ``sha256`` the content generation, and ``first_seen`` the *row*
    generation (NOT NULL, written at insertion and never rewritten in place). The collection's own
    ``created_at`` does the same job one level up for a recreated collection reusing its id.

    The open-event population is bound by **identity**, not by cardinality: hashing only the count
    leaves a same-count ABA open — the single open ``missing`` event on a file is closed (by a
    restore) and a fresh incident opens another one, the count returns to 1, and a stale form would
    validate and silently close an alert nobody has seen. ``id`` + ``kind`` name the incident and
    ``detected_at`` pins its generation, since ``events.id`` is a rowid too and an event *can* be
    deleted (``events.file_id`` is ``ON DELETE CASCADE``), which frees the id for reuse.
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
    if pop.scope == "accept-file":
        header += f"{_FP_US}file={pop.file_id}"
    elif pop.scope in _FP_BULK_FORMS:
        folder = pop.folder if pop.folder is not None else _ROOT_SCOPE
        header += (
            f"{_FP_US}dir={len(folder.dir.encode('utf-8'))}{_FP_US}{folder.dir}"
            f"{_FP_US}deep={'1' if folder.deep else '0'}"
        )

    records = [
        f"{f.id}{_FP_US}{len(f.relpath.encode('utf-8'))}{_FP_US}{f.relpath}{_FP_US}{f.status}"
        f"{_FP_US}{f.sha256 or ''}{_FP_US}"
        f"{f.first_seen.isoformat() if f.first_seen else ''}"
        for f in pop.files
    ]
    preimage = header + _FP_RS + _FP_RS.join(records)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def _review_return(collection_id: int, origin: str, scope: _Scope | None, stale: bool = False) -> str:
    """Where an accept lands — on success and on refusal alike — chosen from a **fixed pair**.

    ``origin`` is a query value, so it is treated as one bit of information and nothing else:
    exactly the literal ``"fleet"`` selects the fleet page; every other value — absent, empty,
    ``//evil.example``, an absolute URL, arbitrary text — falls back to the collection's own review
    page. No part of the supplied string ever reaches the ``Location`` header.

    ``scope`` is the review scope the form was rendered on, so an operator working through one
    folder is returned to that folder. It contributes only percent-encoded query values on the
    fixed collection path (:func:`_scope_query`); a scope that did not parse contributes nothing.
    It selects a DESTINATION only — the verb performed stays a route constant.
    """
    if origin == "fleet":
        return "/review?stale=1" if stale else "/review"
    query = _scope_query(scope) if scope is not None else ""
    if stale:
        query = f"{query}&stale=1" if query else "stale=1"
    base = f"/collection/{collection_id}/review"
    return f"{base}?{query}" if query else base


async def _guarded_accept(
    session: AsyncSession,
    collection: Collection,
    user: User,
    form: str,
    submitted_fp: str,
    file_id: int | None = None,
    origin: str = "",
    folder: _Scope | None = _ROOT_SCOPE,
    return_scope: _Scope | None = None,
    outcome: dict[str, int] | None = None,
) -> RedirectResponse | None:
    """Run ``form``'s accept only if its population still matches ``submitted_fp`` (design D14).

    ``outcome``, if given, receives the service's counts (``accepted`` / ``removed``) on success —
    what the confirmation shown after the redirect states. It is filled only after the mutation
    committed, so a refusal leaves it empty.

    Returns ``None`` when the accept was performed (the caller then redirects to its own success
    target), or the fail-closed refusal response — a 303 to the review surface the submission came
    from, carrying ``stale=1`` — on any of:

    * an absent or empty ``population_fp`` (fail closed: an unguarded POST is refused, never run);
    * a bulk form whose folder scope did not parse (``folder is None``) — refused, not interpreted;
    * an operation already in flight (``blocking_run``, belt-and-braces for the *long* window);
    * the writer lock being unobtainable (BUSY / BUSY_SNAPSHOT / LOCKED — that *is* drift, and an
      uncaught 500 on a destructive POST is the refusal promise broken exactly where the guard
      exists, and an invitation to retry blind);
    * the recomputed fingerprint differing from the submitted one.

    Every refusal path rolls back before returning, so a refusal mutates nothing.

    ``form`` is a **constant per route**, never operator-supplied: the verb performed must not be
    chosen by input on a destructive endpoint. ``folder`` IS operator-supplied, which is why it is
    bound rather than trusted: the recount slices by it, the header names it, and a fingerprint that
    does not reproduce is refused. The mutation then acts on **the ids of the records the recount
    just hashed** (`accept_files`) — never on a second, differently expressed selection of the same
    folder and state, so there is exactly one encoding of "which records" on this path.
    """
    stale = RedirectResponse(
        _review_return(collection.id, origin, return_scope, stale=True), status_code=303
    )
    submitted = (submitted_fp or "").strip()
    if not submitted or (form in _FP_BULK_FORMS and folder is None):
        await session.rollback()
        return stale
    if await collections_svc.blocking_run(session, collection.id) is not None:
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

    # 1b. Look for an operation again, now that nothing else can commit. The check above ran
    #     before the lock, so a scan could have claimed the collection in between and already be
    #     holding rows it read before this accept — which it would then write over. Under the lock
    #     a claim either committed before us (seen here: refuse) or waits for our commit (and then
    #     reads what we wrote). A plain read: reclaiming a stale claim writes from another
    #     connection, which would wait on the lock this request is holding.
    if await collections_svc.active_run(session, collection.id) is not None:
        await session.rollback()
        return stale

    # 2. Recompute inside this transaction, through the SAME single-statement read, the SAME slice
    #    and the SAME encoder the form was minted with.
    pop = await _read_population(
        session, collection, file_id=file_id if form == "accept-file" else None
    )
    narrowed = _narrow(
        pop,
        form,
        file_id=file_id if form == "accept-file" else None,
        folder=folder if form in _FP_BULK_FORMS else None,
    )
    current = _population_fingerprint(collection, narrowed)

    # 3. Compare.
    if not hmac.compare_digest(current, submitted):
        await session.rollback()
        return stale

    # 4. Only now act; the service's own commit() closes this same transaction.
    if form == "accept-file":
        # The fingerprint just proved this row is present, in the state the form named, in this
        # collection. Load it fresh so the service acts on the row the recount saw.
        fe = await session.get(FileEntry, file_id) if narrowed.files else None
        if fe is None or fe.collection_id != collection.id:
            await session.rollback()
            return stale
        result = await scanner_svc.accept_file(session, collection, fe, user.id)
    else:
        result = await scanner_svc.accept_files(
            session, collection, [f.id for f in narrowed.files], user.id
        )
    if outcome is not None:
        outcome.update(result)
    return None


# --- "it worked" (a one-shot confirmation after an accept) -----------------------------------
# An accept answers with a redirect to a freshly rendered review page. The only sign that it
# worked used to be a smaller number somewhere on that page. The route now leaves a short-lived
# cookie stating what the service reported, and the next review render shows it once and clears it.
# The cookie is client-held, so it is parsed against a strict pattern and can only ever produce
# the sentence "Accepted N change(s)/deletion(s)" — it selects nothing and authorises nothing.
_DONE_COOKIE = "cairn_done"
_DONE_RE = re.compile(r"^(changes|deletions|rule):([0-9]{1,7})$")


def _set_done(response: RedirectResponse, outcome: dict[str, int]) -> None:
    accepted, removed = int(outcome.get("accepted", 0)), int(outcome.get("removed", 0))
    if removed > 0:
        value = f"deletions:{removed}"
    elif accepted > 0:
        value = f"changes:{accepted}"
    else:
        return
    response.set_cookie(_DONE_COOKIE, value, max_age=60, httponly=True, samesite="lax", path="/")


def _set_rule_done(response: RedirectResponse, listed: int) -> None:
    """A rule was added; ``listed`` is how many changed files on the review page it matches (and
    which therefore stay listed). Same one-shot cookie, same strict shape."""
    value = f"rule:{min(max(int(listed), 0), 9_999_999)}"
    response.set_cookie(_DONE_COOKIE, value, max_age=60, httponly=True, samesite="lax", path="/")


def _take_done(request: Request) -> dict[str, Any] | None:
    """The confirmation to show on this render, or ``None``. Never trusts the cookie's shape."""
    match = _DONE_RE.match(request.cookies.get(_DONE_COOKIE, ""))
    if match is None:
        return None
    return {"kind": match.group(1), "count": int(match.group(2))}


# --- issue review + recovery ----------------------------------------------------------------
# The "what happened to my files, and what do I do now" surfaces: a collection's review page, the
# fleet-wide review page and the dashboard's needs-you list. All three are views of ONE model,
# built by `_review_model` from ONE `_read_population` snapshot per collection.

# Bound the work a page does for a collection with a very large issue set (a whole deleted folder,
# a volume that went away): at most this many detailed rows per page and per group, and at most
# this many paths in the recovery clipboard. Counts, groups and fingerprints always come from the
# whole snapshot, never from the truncated render list.
REVIEW_ROW_LIMIT = 500
REVIEW_GROUP_ROW_LIMIT = 200
REVIEW_COPY_LIMIT = 2000
# A group this small is rendered open; a larger one is a collapsed <details>.
REVIEW_GROUP_OPEN_MAX = 8

# The fleet page's two caps, for two different failure modes:
#   * per collection, so one collection with 40,000 missing files cannot crowd every other
#     collection off a page whose whole purpose is showing every collection in trouble;
#   * in total, so the page's cost is bounded however many collections are affected.
# A section past the TOTAL budget is still rendered — header, counts, group headers and its link.
FLEET_COLLECTION_ROW_LIMIT = 100
FLEET_ROW_LIMIT = 500

# The kinds of event that record a change to a file's contents (as opposed to its presence).
_CHANGE_EVENT_KINDS = ("modified", "restored_changed", "corrupted")


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _local_day(dt: datetime | None) -> date | None:
    """The calendar day ``dt`` fell on in the server's timezone (what "found on" means to a person)."""
    dt = _as_utc(dt)
    return dt.astimezone().date() if dt is not None else None


def _day_label(day: date | None, today: date | None = None) -> str:
    """"Today" / "Yesterday" / "26 Oct" (with the year once it is not this year)."""
    if day is None:
        return "an unknown date"
    today = today or datetime.now().astimezone().date()
    if day == today:
        return "today"
    if day == today - timedelta(days=1):
        return "yesterday"
    label = f"{day.day} {day.strftime('%b')}"
    return label if day.year == today.year else f"{label} {day.year}"


def _stamp_utc(dt: datetime | None) -> str:
    dt = _as_utc(dt)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt is not None else "unknown"


def _found_at(f: _PopFile, index: _PopIndex) -> datetime | None:
    """When this issue was found: its newest open event, from the SAME snapshot as the row.

    A file with no open event (data from before events stayed open until resolution) falls back to
    when its record last changed, then to when it was last checked.
    """
    event = index.newest_event(f.id)
    if event is not None and event.detected_at is not None:
        return _as_utc(event.detected_at)
    return _as_utc(f.last_changed or f.last_checked)


def _common_dir(relpaths: list[str]) -> str:
    """The deepest directory containing every path ("" = the collection root)."""
    common: list[str] | None = None
    for p in relpaths:
        segs = p.split("/")[:-1]
        if common is None:
            common = segs
        else:
            n = 0
            limit = min(len(common), len(segs))
            while n < limit and common[n] == segs[n]:
                n += 1
            common = common[:n]
        if not common:
            break
    return "/".join(common or [])


def _group_scopes(files: list[_PopFile], scope: _Scope) -> list[tuple[_Scope, list[_PopFile]]]:
    """Partition ``files`` (already inside ``scope``) into folder groups (design D7). Pure.

    One group per immediate child folder of the files' deepest common folder (its whole subtree),
    plus one for any files sitting directly in that common folder. A single element means "no
    grouping to show": every file is in one folder.
    """
    if not files:
        return []
    if not scope.deep:
        return [(scope, files)]
    common = _common_dir([f.relpath for f in files])
    base = f"{common}/" if common else ""
    groups: dict[_Scope, list[_PopFile]] = {}
    for f in files:
        rest = f.relpath[len(base):]
        if "/" in rest:
            key = _Scope(base + rest.split("/", 1)[0], True)
        else:
            key = _Scope(common, False)
        groups.setdefault(key, []).append(f)
    return list(groups.items())


def _action(collection: Collection, pop: _Population, form: str, folder: _Scope, shown: int) -> dict[str, Any] | None:
    """One bulk control: its count, its fingerprint and how many of its files are not on the page.

    All three come from the same slice of the same snapshot, so the number on the button, the
    population hashed and the population eventually mutated are one statement. ``None`` when the
    population is empty — an action over nothing is never rendered.
    """
    narrowed = _narrow(pop, form, folder=folder)
    count = len(narrowed.files)
    if count == 0:
        return None
    return {
        "count": count,
        "fp": _population_fingerprint(collection, narrowed),
        "hidden": max(0, count - shown),
        "dir": folder.dir,
        "deep": "1" if folder.deep else "0",
    }


def _row(
    f: _PopFile,
    collection: Collection,
    pop: _Population,
    index: _PopIndex,
    group_dir: str,
) -> dict[str, Any]:
    """One review row from the snapshot. Display-only facts are attached later (`_attach_facts`)."""
    rel_dir = f.relpath.rsplit("/", 1)[0] if "/" in f.relpath else ""
    # Inside a group the folder is already on the group's header; show only what lies below it.
    shown_dir = rel_dir
    if group_dir and (rel_dir == group_dir or rel_dir.startswith(group_dir + "/")):
        shown_dir = rel_dir[len(group_dir):].lstrip("/")
    event = index.newest_event(f.id)
    found = _found_at(f, index)
    written = (
        datetime.fromtimestamp(f.mtime, tz=timezone.utc) if f.mtime is not None else None
    )
    # A file that came back different stays that alarm for as long as that event is open — even
    # when a LATER ordinary change has opened a newer event on top of it. Its detail line is the
    # only surviving record of the digest the file had before it came back, and accepting the row
    # closes that event too, so it must never be accepted without having been shown.
    came_back = index.newest_event(f.id, "restored_changed") if f.status != "missing" else None
    # Possible corruption — contents changed while size and date did not — is likewise never drawn
    # as an ordinary change while that alert is open. "Came back different" outranks it: that row
    # carries the digest record.
    corrupted = index.newest_event(f.id, "corrupted") if f.status != "missing" else None
    if f.status == "missing":
        kind = "missing"
    elif came_back is not None:
        kind = "came_back"
    elif corrupted is not None:
        kind = "corrupted"
    else:
        kind = "changed"
    return {
        "id": f.id,
        "relpath": f.relpath,
        "name": f.relpath.rsplit("/", 1)[-1],
        "dir": shown_dir,
        "status": f.status,
        "kind": kind,
        "size": humanize_size(f.size),
        # What the size was before the change, where that differs — one more fact for "was that me?".
        "was_size": (
            humanize_size(f.prior_size)
            if f.status != "missing" and f.prior_size is not None and f.prior_size != f.size
            else None
        ),
        "found": humanize_delta(found),
        "found_full": _stamp_utc(found),
        "written": humanize_delta(written) if written is not None else None,
        "written_full": _stamp_utc(written),
        "notarized": f.ots_state in ("incomplete", "complete"),
        # The event whose recorded detail the row shows (the came-back-different one, if any).
        "event_id": (came_back or event).id if (came_back or event) is not None else None,
        "changes": 0,
        "changes_since": None,
        "detail": None,
        "fp": _population_fingerprint(
            collection, _narrow(pop, "accept-file", file_id=f.id, index=index)
        ),
    }


async def _attach_facts(session: AsyncSession, collection_id: int, rows: list[dict[str, Any]]) -> None:
    """Add the display-only history to rendered rows: how often a file changed, and since when.

    Separate reads on purpose. Nothing here names a verb or enters a fingerprint — a row's status,
    its control and its authorisation all came from the snapshot — so these are "pure display"
    counts in the guard's own terms, and they are fetched only for the rows actually rendered.
    """
    ids = [r["id"] for r in rows if r["status"] != "missing"]
    for i in range(0, len(ids), 400):
        chunk = ids[i : i + 400]
        result = await session.execute(
            select(Event.file_id, func.count(), func.min(Event.detected_at))
            .where(
                Event.collection_id == collection_id,
                Event.file_id.in_(chunk),
                Event.kind.in_(_CHANGE_EVENT_KINDS),
            )
            .group_by(Event.file_id)
        )
        history = {fid: (n, first) for fid, n, first in result.all()}
        for r in rows:
            n, first = history.get(r["id"], (0, None))
            if n > 1:
                r["changes"] = int(n)
                r["changes_since"] = _day_label(_local_day(first))
    # A file that came back different carries both digests in its event's detail — the only
    # surviving record of what it was before — and the row is where that is shown now.
    detail_ids = [r["event_id"] for r in rows if r["kind"] == "came_back" and r["event_id"]]
    for i in range(0, len(detail_ids), 400):
        chunk = detail_ids[i : i + 400]
        result = await session.execute(select(Event.id, Event.detail).where(Event.id.in_(chunk)))
        details = dict(result.all())
        for r in rows:
            if r["event_id"] in details:
                r["detail"] = details[r["event_id"]]


def _found_range(days: list[date]) -> str:
    """"26 Oct" for one day of findings, "2 Nov – 9 Nov" for several."""
    if not days:
        return ""
    lo, hi = min(days), max(days)
    return _day_label(lo) if lo == hi else f"{_day_label(lo)} – {_day_label(hi)}"


def _group_label(scope: _Scope, page_scope: _Scope) -> str:
    """A group's heading: its folder, relative to nothing (the full collection-relative path)."""
    if scope.deep:
        return (scope.dir + "/") if scope.dir else "the whole collection"
    # The files sitting directly in a folder, as opposed to the folder's whole subtree: the label
    # has to say so, because its bulk action and its scope link leave the subfolders alone.
    return f"{scope.dir}/ (this folder only)" if scope.dir else "top level (not in any folder)"


def _review_model(
    collection: Collection,
    pop: _Population,
    scope: _Scope,
    row_budget: int,
) -> dict[str, Any]:
    """Everything a review surface shows for one collection at one scope. Pure — it reads nothing.

    Groups, counts, the day summary, every row and every fingerprint are slices of the one
    snapshot it is handed (design D7), so no two controls on a page can describe different states.
    Rows are budgeted (per group and per page); counts and fingerprints are not.
    """
    index = _PopIndex(pop)
    files = [f for f in pop.files if _in_scope(f.relpath, scope)]
    found_by_id = {f.id: _found_at(f, index) for f in files}
    missing = sum(1 for f in files if f.status == "missing")
    modified = len(files) - missing

    # "Found" summary by local day, newest first.
    days: dict[date | None, int] = {}
    for f in files:
        d = _local_day(found_by_id[f.id])
        days[d] = days.get(d, 0) + 1
    ordered_days = sorted(
        days.items(), key=lambda kv: (kv[0] is not None, kv[0] or date.min), reverse=True
    )
    found_days = [{"label": _day_label(d), "count": n} for d, n in ordered_days[:4]]
    earlier = sum(n for _d, n in ordered_days[4:])

    partition = _group_scopes(files, scope)
    flat = len(partition) <= 1
    # Newest finding first, then by path — a stable, total order.
    def _newest(members: list[_PopFile]) -> float:
        stamps = [found_by_id[f.id].timestamp() for f in members if found_by_id[f.id] is not None]
        return max(stamps) if stamps else 0.0

    partition.sort(key=lambda g: (-_newest(g[1]), g[0].dir, not g[0].deep))

    groups: list[dict[str, Any]] = []
    rows_all: list[dict[str, Any]] = []
    budget = max(row_budget, 0)
    shown_missing = shown_modified = 0
    for gscope, members in partition:
        ordered = sorted(members, key=lambda f: (0 if f.status == "missing" else 1, f.relpath))
        # A group is capped so one folder cannot use the whole page; a single list IS the page.
        take = min(len(ordered), budget) if flat else min(len(ordered), REVIEW_GROUP_ROW_LIMIT, budget)
        budget -= take
        rows = [_row(f, collection, pop, index, gscope.dir) for f in ordered[:take]]
        rows_all.extend(rows)
        g_missing = sum(1 for f in members if f.status == "missing")
        g_shown_missing = sum(1 for r in rows if r["status"] == "missing")
        shown_missing += g_shown_missing
        shown_modified += len(rows) - g_shown_missing
        g_days = [d for d in (_local_day(found_by_id[f.id]) for f in members) if d is not None]
        groups.append(
            {
                "label": _group_label(gscope, scope),
                "dir": gscope.dir,
                "deep": gscope.deep,
                "query": _scope_query(gscope),
                "total": len(members),
                "missing": g_missing,
                "modified": len(members) - g_missing,
                "found": _found_range(g_days),
                "items": rows,
                "shown": len(rows),
                "more": len(members) - len(rows),
                "open": len(members) <= REVIEW_GROUP_OPEN_MAX,
                # Bulk actions only where grouping exists; a flat list uses the page-wide ones.
                "accept_changes": None if flat else _action(
                    collection, pop, "adopt-changed", gscope, len(rows) - g_shown_missing
                ),
                "accept_deletions": None if flat else _action(
                    collection, pop, "stop-tracking", gscope, g_shown_missing
                ),
            }
        )

    return {
        "collection_id": collection.id,
        "collection_name": collection.name,
        "root": collection.root,
        "scope_dir": scope.dir,
        "scope_deep": scope.deep,
        "scope_is_root": scope.is_root,
        "scope_query": _scope_query(scope),
        "scope_label": _group_label(scope, scope),
        "total": len(files),
        "missing": missing,
        "modified": modified,
        "collection_total": len(pop.files),
        "found_days": found_days,
        "found_earlier": earlier,
        "newest_found": max(
            (found_by_id[f.id] for f in files if found_by_id[f.id] is not None), default=None
        ),
        "flat": flat,
        "groups": groups,
        "rows": rows_all,
        "shown": len(rows_all),
        "truncated": len(files) > len(rows_all),
        # Page-wide actions act on the page's own scope.
        "accept_changes": _action(collection, pop, "adopt-changed", scope, shown_modified),
        "accept_deletions": _action(collection, pop, "stop-tracking", scope, shown_missing),
        "copy_relpaths": [f.relpath for f in files[:REVIEW_COPY_LIMIT]],
    }


def _newest_found(pop: _Population) -> datetime | None:
    """When the collection's most recent issue was found (from the snapshot, like every row)."""
    index = _PopIndex(pop)
    return max(
        (d for d in (_found_at(f, index) for f in pop.files) if d is not None), default=None
    )


def _severity_key(collection: Collection, newest: datetime | None) -> tuple[int, float, str]:
    """Order affected collections by what most deserves attention, not by size (design D9).

    Locked before Working — an issue where nothing should change is the higher signal — then the
    collection whose newest issue was found most recently, then by name.
    """
    return (
        0 if collection.mode == "worm" else 1,
        -(newest.timestamp() if newest is not None else 0.0),
        collection.name,
    )


@router.get("/collection/{collection_id}/review", response_class=HTMLResponse)
async def collection_review(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    stale: str = Query(""),
    dir: str = Query(""),
    deep: str = Query("1"),
):
    """A collection's changed and missing files, grouped by folder, with the actions that resolve them.

    ``dir`` / ``deep`` open the page at a folder scope (design D7): the same view, restricted to
    that folder and grouped one level deeper. A scope that does not parse is ignored (the page
    renders the whole collection) — it selects only what is *displayed*; every action on the page
    carries its own bound scope.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    view = await _collection_view(session, collection)
    scope = _parse_scope(dir, deep) or _ROOT_SCOPE

    # ONE read of the whole review population. Everything below is derived from it: the groups, the
    # rows, the copy list, the totals and every fingerprint. Re-querying for any of those would
    # reopen the window this guard exists to close.
    pop = await _read_population(session, collection)
    model = _review_model(collection, pop, scope, REVIEW_ROW_LIMIT)
    await _attach_facts(session, collection.id, model["rows"])
    _attach_rule_state(collection, model["rows"])

    # The page's own claims about the collection come from the snapshot too, not from
    # `_collection_view`'s earlier, separate count queries: "nothing to review" only means
    # "all clear" if the collection is actually watching files (#31).
    view["counts"] = dict(view["counts"])
    view["counts"]["missing"] = sum(1 for f in pop.files if f.status == "missing")
    view["counts"]["modified"] = len(pop.files) - view["counts"]["missing"]
    view["is_empty"] = pop.total_files == 0

    copy_relpaths = model["copy_relpaths"]
    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": view,
            "r": model,
            "row_from": "",
            "copy_relpaths": "\n".join(copy_relpaths),
            "copy_count": len(copy_relpaths),
            "copy_truncated": model["total"] > len(copy_relpaths),
            "stale": stale == "1",
            # Never beside the refusal notice: the two would be about different clicks.
            "done": None if stale == "1" else _take_done(request),
        }
    )
    response = templates.TemplateResponse(request, "collection_review.html", ctx)
    if ctx["done"] is not None:
        response.delete_cookie(_DONE_COOKIE, path="/")
    return response


@router.get("/review", response_class=HTMLResponse)
async def fleet_review(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    stale: str = Query(""),
):
    """Every open issue across the current user's collections, one section per collection.

    **One population read per collection, and everything a section shows or authorizes is a slice
    of it** — the counts, the groups, the rows and every fingerprint. Each control acts inside one
    collection (one file, or one folder of one collection); there is no collection-spanning verb,
    because every accept is authorised over a single collection's population and is irreversible.
    """
    collections = await collections_svc.list_collections(session, user_id=user.id)

    # Read once per collection, then decide what to render. Ordering needs every section's newest
    # finding, so the reads cannot be stopped early at the row budget — and a section past the
    # budget still has to state its counts.
    snapshots = []
    for c in collections:
        pop = await _read_population(session, c)
        if not pop.files:
            continue  # not affected — an unaffected collection is not listed at all
        snapshots.append((c, pop))

    sections: list[dict[str, Any]] = []
    budget = FLEET_ROW_LIMIT
    snapshots.sort(key=lambda s: _severity_key(s[0], _newest_found(s[1])))
    for c, pop in snapshots:
        model = _review_model(c, pop, _ROOT_SCOPE, min(FLEET_COLLECTION_ROW_LIMIT, max(budget, 0)))
        budget -= model["shown"]
        await _attach_facts(session, c.id, model["rows"])
        _attach_rule_state(c, model["rows"])
        model["policy"] = _policy_view(c.mode, _rules_count(c))
        sections.append(model)

    ctx = await _base_context(request, session, user, "review")
    ctx.update(
        {
            "sections": sections,
            "has_collections": bool(collections),
            "total_missing": sum(s["missing"] for s in sections),
            "total_modified": sum(s["modified"] for s in sections),
            "total_issues": sum(s["total"] for s in sections),
            "row_from": "fleet",
            "stale": stale == "1",
            # Never beside the refusal notice: the two would be about different clicks.
            "done": None if stale == "1" else _take_done(request),
        }
    )
    response = templates.TemplateResponse(request, "review_fleet.html", ctx)
    if ctx["done"] is not None:
        response.delete_cookie(_DONE_COOKIE, path="/")
    return response


# The two bulk verbs, one route each. The addresses are unchanged from before the verbs were
# renamed for the operator ("Accept N changes" / "Accept N deletions"): one address keeps meaning
# one consequence. Each takes the folder scope its form was rendered for.


async def _bulk_accept(
    form: str,
    collection_id: int,
    session: AsyncSession,
    user: User,
    population_fp: str,
    dir: str,
    deep: str,
    return_dir: str,
    return_deep: str,
    origin: str,
) -> RedirectResponse:
    collection = await _get_owned_collection(session, collection_id, user)
    return_scope = _parse_scope(return_dir, return_deep)
    outcome: dict[str, int] = {}
    refused = await _guarded_accept(
        session,
        collection,
        user,
        form,
        population_fp,
        origin=origin,
        folder=_parse_scope(dir, deep),
        return_scope=return_scope,
        outcome=outcome,
    )
    if refused is not None:
        return refused
    response = RedirectResponse(_review_return(collection_id, origin, return_scope), status_code=303)
    _set_done(response, outcome)
    return response


@router.post(
    "/collection/{collection_id}/review/adopt-changed",
    dependencies=[Depends(verify_csrf)],
)
async def collection_review_adopt_changed(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    population_fp: str = Form(""),
    dir: str = Form(""),
    deep: str = Form("1"),
    return_dir: str = Form(""),
    return_deep: str = Form("1"),
    origin: str = Query("", alias="from"),
):
    """Accept the changed files inside one folder scope as they are now ("Accept N changes").

    Acts on the `modified` files in that scope and nothing else: missing records are untouched.
    Guarded by the D14 fingerprint minted over *this* verb's population in *this* folder — so a
    scan recording a change in another folder does not refuse it, while a change to its own set
    does.
    """
    return await _bulk_accept(
        "adopt-changed", collection_id, session, user, population_fp,
        dir, deep, return_dir, return_deep, origin,
    )


@router.post(
    "/collection/{collection_id}/review/stop-tracking",
    dependencies=[Depends(verify_csrf)],
)
async def collection_review_stop_tracking(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    population_fp: str = Form(""),
    dir: str = Form(""),
    deep: str = Form("1"),
    return_dir: str = Form(""),
    return_deep: str = Form("1"),
    origin: str = Query("", alias="from"),
):
    """Remove the records of the missing files inside one folder scope ("Accept N deletions").

    The file rows are deleted (their events detached first, so the history survives the cascade and
    keeps the path it is about). Nothing on disk is touched and no `.ots` proof is deleted — what
    is lost is the link from Cairn's records to the proof.
    """
    return await _bulk_accept(
        "stop-tracking", collection_id, session, user, population_fp,
        dir, deep, return_dir, return_deep, origin,
    )


@router.post(
    "/collection/{collection_id}/file/{file_id}/accept",
    dependencies=[Depends(verify_csrf)],
)
async def collection_file_accept(
    collection_id: int,
    file_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    population_fp: str = Form(""),
    return_dir: str = Form(""),
    return_deep: str = Form("1"),
    # Which review surface this row was rendered on. Whitelisted to a fixed pair by
    # `_review_return`; it never becomes part of a redirect target and never selects a verb.
    origin: str = Query("", alias="from"),
):
    """Accept ONE row from a review page, then land back on a freshly rendered review.

    A plain POST → 303, not an htmx row swap: the row's disappearance changes the summary, the
    group's counts, the copy list, every bulk label and the sidebar badge — and a *refusal* inside
    a row swap has nowhere to render the staleness notice.

    A row belonging to another collection is a 404 — the same non-disclosure `_get_owned_collection`
    makes one level up. A row that no longer exists is NOT a 404: another tab may have just accepted
    it, and that is drift, so it takes the ordinary refusal path with every other kind of drift.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    owner_id = await session.scalar(
        select(FileEntry.collection_id).where(FileEntry.id == file_id)
    )
    if owner_id is not None and owner_id != collection.id:
        raise HTTPException(status_code=404, detail="file not found")
    return_scope = _parse_scope(return_dir, return_deep)
    outcome: dict[str, int] = {}
    refused = await _guarded_accept(
        session, collection, user, "accept-file", population_fp,
        file_id=file_id, origin=origin, return_scope=return_scope, outcome=outcome,
    )
    if refused is not None:
        return refused
    # Success and refusal land on the SAME surface and scope.
    response = RedirectResponse(_review_return(collection_id, origin, return_scope), status_code=303)
    _set_done(response, outcome)
    return response


# --- path rules ---------------------------------------------------------------------------------
# A rule says that ordinary changes to the files a pattern matches are expected (add-path-rules).
# These routes only ever store or remove a pattern: they accept nothing, acknowledge nothing and
# remove no record, which is why they carry no population fingerprint. What keeps a rule honest is
# that its reach is on the page before it exists, and that the grammar refuses a rule for
# everything. The grammar itself lives in `services/rules.py`; nothing here re-states it.

RULE_START_PATH_MAX = 4096
RULE_TYPED_MAX = 2048

# Why a pattern was refused, in the operator's words. Keyed by `RuleError.reason`; an unknown
# reason falls back to the service's own sentence.
_RULE_REFUSALS = {
    "empty": "Type a pattern first.",
    "absolute": "A pattern is read from the collection's own folder, so it cannot start with a slash.",
    "segment": "A pattern cannot contain an empty folder name, “.” or “..”.",
    "control": "A pattern cannot contain a line break.",
    "too_long": f"That pattern is too long — {rules_svc.MAX_PATTERN_BYTES} bytes at most.",
    "double_star": (
        "“**” stands for whole folders, so it has to be a folder on its own, as in a/**/b. "
        "It cannot be part of a name."
    ),
    "match_all": (
        "A rule cannot cover every file. If changes anywhere in this collection are normal, that "
        "is what its policy says: set the collection's policy to Working."
    ),
    "duplicate": "This collection already has that rule.",
    # A stored entry with this pattern that the scanner ignores (an effect this version does not
    # know): not "already a rule" — nothing is being silenced — but it does occupy the pattern.
    "duplicate_ignored": (
        "This collection has a stored entry with that pattern that is not recognised and is "
        "ignored. Remove it on the rules page, then add the rule."
    ),
    "too_many": (
        f"This collection already has {rules_svc.MAX_RULES} rules, the most it can hold. "
        "Remove one first."
    ),
}


RULE_SAMPLE = 20


def _covering_rule(collection: Collection, relpath: str) -> str | None:
    """The first of the collection's rules that matches ``relpath``, or ``None``."""
    for pattern in rules_svc.load(collection).patterns:
        try:
            if rules_svc.compile_pattern(pattern).fullmatch(relpath):
                return pattern
        except (rules_svc.RuleError, re.error):
            continue
    return None


def _attach_rule_state(collection: Collection, rows: list[dict[str, Any]]) -> None:
    """What each ordinary-change row says about rules: ``rule_link`` (offer "Don't flag future
    changes…") or ``covered_by`` (the rule that already covers it — so the row says the change
    only needs accepting, instead of inviting a second, redundant rule). Neither under the
    Working policy, where ordinary edits are quiet anyway and a rule does nothing."""
    compiled: list[tuple[str, re.Pattern[str]]] = []
    if collection.mode != "churn":
        for pattern in rules_svc.load(collection).patterns:
            try:
                compiled.append((pattern, rules_svc.compile_pattern(pattern)))
            except (rules_svc.RuleError, re.error):
                continue
    for row in rows:
        row["rule_link"], row["covered_by"] = False, None
        if row["kind"] != "changed" or collection.mode == "churn":
            continue
        row["covered_by"] = next(
            (pattern for pattern, rx in compiled if rx.fullmatch(row["relpath"])), None
        )
        row["rule_link"] = row["covered_by"] is None


def _mass_rule(settings: Any) -> dict[str, int] | None:
    """The mass-change ceiling's two thresholds, for the sentence that explains it; ``None`` when
    it is switched off (a share of 0), so the pages do not describe a safety net that is not on."""
    if settings.mass_change_percent <= 0:
        return None
    return {"files": settings.mass_change_min_files, "percent": settings.mass_change_percent}


def _rule_refusal(reason: str, fallback: str = "") -> dict[str, str]:
    return {"reason": reason, "text": _RULE_REFUSALS.get(reason) or fallback or "That pattern cannot be used."}


def _legal_relpath(path: str) -> bool:
    """Whether a query value can be a file's collection-relative path. The value is untrusted text:
    anything else is dropped, never shown and never split into proposals."""
    if not path or len(path.encode("utf-8", "replace")) > RULE_START_PATH_MAX:
        return False
    # A line break is legal in a file name (macOS writes `Icon\r` into folders), so it is legal
    # here: such a name cannot be a pattern, but its folder can, and the page still starts from it.
    if path.startswith("/") or "\x00" in path:
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def _rule_proposals(path: str) -> list[dict[str, Any]]:
    """The patterns offered for a flagged file, narrowest first: the file alone, its folder, its
    name in any folder. A proposal is kept only if the grammar stores it **unchanged** and the
    compiled pattern **matches the file it was derived from** — the page never offers a pattern it
    has not just checked against the path it is describing. A file directly in the collection root
    has one proposal: a bare name is, by the grammar, that name in any folder."""
    if not _legal_relpath(path):
        return []
    folder, _, name = path.rpartition("/")
    candidates = [("file", path), ("folder", folder + "/")] if folder else []
    candidates.append(("name", name))
    proposals: list[dict[str, Any]] = []
    for kind, pattern in candidates:
        try:
            stored = rules_svc.validate_pattern(pattern)
            if stored != pattern or not rules_svc.compile_pattern(stored).fullmatch(path):
                continue
        except (rules_svc.RuleError, re.error):
            continue
        if any(p["pattern"] == stored for p in proposals):
            continue
        proposals.append(
            {
                "kind": kind,
                "pattern": stored,
                "name": name,
                "folder": folder,
                # `*` and `?` in a real file name are wildcards to the grammar (there is no
                # escape), so such a proposal can reach further than its label; the page says so.
                "wild": "*" in stored or "?" in stored,
            }
        )
    return proposals


def _rule_check(collection: Collection, raw: str) -> tuple[str | None, dict[str, str] | None]:
    """``(pattern to store, None)`` or ``(None, refusal)`` for a typed pattern — every refusal
    `add_rule` would make, so the page can state it before anything is submitted."""
    try:
        pattern = rules_svc.validate_pattern(raw)
    except rules_svc.RuleError as exc:
        return None, _rule_refusal(exc.reason, str(exc))
    existing = rules_svc.list_rules(collection)
    for entry in existing:
        if entry["pattern"] == pattern:
            return None, _rule_refusal("duplicate" if entry["recognised"] else "duplicate_ignored")
    if len(existing) >= rules_svc.MAX_RULES:
        return None, _rule_refusal("too_many")
    return pattern, None


def _rule_collection(collection: Collection) -> dict[str, Any]:
    """What the two rule pages say about the collection they belong to."""
    return {
        "id": collection.id,
        "name": collection.name,
        "policy": _policy_view(collection.mode, _rules_count(collection)),
        "ots": collection.ots_mode,
        # Under Working, ordinary edits are quiet everywhere; a rule adds nothing there. The pages
        # say so instead of letting a rule look like it did something.
        "rules_have_effect": collection.mode != "churn",
    }


@router.get("/collection/{collection_id}/rules/new", response_class=HTMLResponse)
async def rule_new(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    path: str = Query(""),
    pattern: str | None = Query(None),
):
    """Say that changes to some files are expected — with each pattern's reach shown first.

    ``path`` is the flagged file the operator came from (untrusted: used only to derive proposals
    and shown escaped). ``pattern`` is a pattern of their own to check: it is validated and its
    reach — or the reason it is refused — is rendered, and nothing is stored by a GET.
    """
    collection = await _get_owned_collection(session, collection_id, user)
    start = path if _legal_relpath(path) else ""
    proposals = _rule_proposals(start)
    stored_entries = rules_svc.list_rules(collection)
    existing = {entry["pattern"] for entry in stored_entries if entry["recognised"]}
    ignored = {entry["pattern"] for entry in stored_entries if not entry["recognised"]}

    custom: dict[str, Any] | None = None
    if pattern is not None:
        # Present but blank is a submission too: it is refused, with the reason, like any other.
        typed = pattern[:RULE_TYPED_MAX]
        stored, refusal = _rule_check(collection, typed)
        # `typed` is what the operator typed, kept verbatim for the field and for a refusal;
        # `pattern` is what would be stored. The card of an accepted pattern shows `pattern` —
        # never `typed` — so the text beside "Add this rule" is exactly what that button stores.
        custom = {"typed": typed, "pattern": stored, "refusal": refusal}
        # The operator's own pattern is shown once, as theirs.
        proposals = [p for p in proposals if p["pattern"] != stored]

    wanted = [p["pattern"] for p in proposals]
    as_folder: str | None = None
    if custom and custom["pattern"]:
        wanted.append(custom["pattern"])
        # The commonest way to match nothing is a folder typed without its trailing slash (which
        # reads as a file NAME). Checked in the same pass, and offered only if it matches.
        if not custom["pattern"].endswith("/"):
            try:
                as_folder = rules_svc.validate_pattern(custom["pattern"] + "/")
                wanted.append(as_folder)
            except rules_svc.RuleError:
                as_folder = None
    reach = await rules_svc.reach_many(session, collection, wanted, sample=RULE_SAMPLE)
    for p in proposals:
        p["reach"] = reach[p["pattern"]]
        p["exists"] = p["pattern"] in existing
        p["ignored"] = p["pattern"] in ignored
    if custom and custom["pattern"]:
        custom["reach"] = reach[custom["pattern"]]
        if custom["reach"]["tracked"] == 0 and as_folder and reach[as_folder]["tracked"]:
            custom["as_folder"] = {"pattern": as_folder, "tracked": reach[as_folder]["tracked"]}

    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": _rule_collection(collection),
            "start": start,
            # The rule that already covers the starting file, if any: said up front, so the page
            # is not an invitation to stack a second rule on a file that only needs accepting.
            "covered_by": _covering_rule(collection, start) if start else None,
            "proposals": proposals,
            "custom": custom,
            "window_days": rules_svc.ALARM_WINDOW_DAYS,
            "mass": _mass_rule(get_settings()),
        }
    )
    return templates.TemplateResponse(request, "rule_new.html", ctx)


@router.post("/collection/{collection_id}/rules", dependencies=[Depends(verify_csrf)])
async def rule_create(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    pattern: str = Form(""),
    path: str = Form(""),
):
    """Store a rule. It changes no file record and no event: what is listed stays listed.

    A refused pattern goes back to the page it came from, which states the reason and keeps what
    was typed (the GET derives the same refusal — one statement of the grammar, not two).
    """
    collection = await _get_owned_collection(session, collection_id, user)
    try:
        stored = await rules_svc.add_rule(session, collection, pattern[:RULE_TYPED_MAX])
    except rules_svc.RuleError:
        back = {"pattern": pattern[:RULE_TYPED_MAX]}
        if _legal_relpath(path):
            back["path"] = path
        return RedirectResponse(
            f"/collection/{collection_id}/rules/new?{urlencode(back)}", status_code=303
        )
    listed = (await rules_svc.reach(session, collection, stored))["open_changed"]
    # Back to where the operator started: the review page when they came from a flagged file,
    # the rules page when they typed a pattern there. Both show the same one-shot confirmation.
    target = "review" if _legal_relpath(path) else "rules"
    response = RedirectResponse(f"/collection/{collection_id}/{target}", status_code=303)
    _set_rule_done(response, listed)
    return response


@router.get("/collection/{collection_id}/rules", response_class=HTMLResponse)
async def collection_rules(
    collection_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    gone: str = Query(""),
):
    """A collection's rules, each with what it matches now, and the control that removes it."""
    collection = await _get_owned_collection(session, collection_id, user)
    entries = rules_svc.list_rules(collection)
    reach = await rules_svc.reach_many(
        session, collection, [e["pattern"] for e in entries if e["recognised"]], sample=RULE_SAMPLE
    )
    rows = []
    for entry in entries:
        created = None
        try:
            created = humanize_date(datetime.fromisoformat(entry["created_at"] or ""))
        except (TypeError, ValueError):
            pass
        rows.append(
            {
                "pattern": entry["pattern"],
                "recognised": entry["recognised"],
                "created": created,
                "tracked": reach[entry["pattern"]]["tracked"] if entry["recognised"] else None,
                "sample": reach[entry["pattern"]]["sample"] if entry["recognised"] else [],
            }
        )
    total = next(iter(reach.values()))["total"] if reach else None
    ctx = await _base_context(request, session, user, "collection")
    ctx.update(
        {
            "c": _rule_collection(collection),
            "rules": rows,
            "total": total,
            "max_rules": rules_svc.MAX_RULES,
            # `?gone=1` a rule was removed; `?gone=0` the rule named was no longer there.
            "gone": gone if gone in ("0", "1") else "",
            "mass": _mass_rule(get_settings()),
        }
    )
    # The one-shot "Rule added" confirmation, when the rule was typed on this page.
    done = _take_done(request)
    ctx["done"] = done if done and done["kind"] == "rule" else None
    response = templates.TemplateResponse(request, "collection_rules.html", ctx)
    if ctx["done"] is not None:
        response.delete_cookie(_DONE_COOKIE, path="/")
    return response


@router.post("/collection/{collection_id}/rules/delete", dependencies=[Depends(verify_csrf)])
async def rule_delete(
    collection_id: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_user),
    pattern: str = Form(""),
):
    """Remove the rule with exactly this pattern — named, never positional, so a list that changed
    since the page was rendered cannot lose a different rule. Changes no file record."""
    collection = await _get_owned_collection(session, collection_id, user)
    removed = await rules_svc.remove_rule(session, collection, pattern)
    return RedirectResponse(
        f"/collection/{collection_id}/rules?gone={'1' if removed else '0'}", status_code=303
    )


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
    if await collections_svc.blocking_run(session, collection_id) is not None:
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
        # The form no longer offers this as a control (design D4): a new file is not work, so a
        # collection created here graduates new files on its own.
        auto_baseline_new=True,
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
        exclude_globs=_parse_excludes(excludes),
        alert=_build_alert(email_enabled, email_to),
    )
    return RedirectResponse(f"/collection/{collection_id}", status_code=303)


# --- verify ---------------------------------------------------------------------------------

# The verify search's row cap. A search whose whole purpose is finding one file must not silently
# drop matches, so the cap is paired with the TRUE match count in the render (see
# `_search_tracked_files`): the operator is told the list is short and how short.
VERIFY_SEARCH_ROW_LIMIT = 50


async def _recent_proofs_query(session: AsyncSession, user: User, limit: int):
    """The verify page's DEFAULT listing: the user's most recently submitted proofs.

    Deliberately narrower than search (design D2): its population is files with a proof that has
    been submitted to a calendar — `incomplete` (awaiting Bitcoin) or `complete`. That filter is
    this list's stated purpose, so it stays here; it must never travel into search, where it hides
    exactly the files whose verify card carries actionable guidance. The rendered heading and copy
    say *proofs*, never "anchored" — half this population is not anchored yet.
    """
    stmt = (
        select(FileEntry, Collection)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(
            Collection.user_id == user.id,
            FileEntry.ots_state.in_(("incomplete", "complete")),
        )
        .order_by(FileEntry.ots_stamped_at.desc().nulls_last())
        .limit(limit)
    )
    return list(await session.execute(stmt))


async def _search_tracked_files(
    session: AsyncSession, user: User, q: str, limit: int = VERIFY_SEARCH_ROW_LIMIT
) -> tuple[list[Any], int]:
    """Search EVERY tracked file the user owns; return (capped rows, true match total).

    Two properties are load-bearing (design D2) and neither is decoration:

      * **No proof-state filter.** A never-stamped or queued file is exactly what an operator
        searching for a file by name is looking for, and its verify card is where the advice for
        that state lives.
      * **A unique deterministic order, `relpath` then `collection_id`.** Ordering by recency of
        stamping and then cutting at the cap makes a file that shares a searchable name with
        enough stamped files unreachable by ANY query — the "silently hides files" defect one
        level down. Path order plus the disclosed total lets a narrower query always converge.

    The total is a separate COUNT over the same predicate, so the render can say "showing N of M"
    rather than presenting a truncated page as the whole answer. Both statements are bounded; the
    full match set is never materialized.
    """
    predicate = (
        Collection.user_id == user.id,
        FileEntry.relpath.like(f"%{collections_svc._escape_like(q)}%", escape="\\"),
    )
    total = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(*predicate)
    )
    stmt = (
        select(FileEntry, Collection)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(*predicate)
        .order_by(FileEntry.relpath.asc(), FileEntry.collection_id.asc())
        .limit(limit)
    )
    return list(await session.execute(stmt)), int(total or 0)


def _verify_row_view(fe: FileEntry, collection: Collection) -> dict[str, Any]:
    """One row of the verify listing, rendered for BOTH populations.

    It carries the row's own `state` unfiltered: that badge is what tells a never-stamped file
    apart from an anchored one in a search that now returns both (design D2).
    """
    return {
        "id": fe.id,
        "filename": Path(fe.relpath).name,
        "relpath": fe.relpath,
        "collection": collection.name,
        "state": fe.ots_state,
    }


async def _results_context(session: AsyncSession, user: User, q: str) -> dict[str, Any]:
    """The one place the verify listing region is filled, for both render paths.

    A blank (or whitespace-only) query is the page's DEFAULT state and renders the recent-proofs
    listing — never the widened search (design D2). Clearing the input therefore restores exactly
    what the page opened with instead of leaking every unstamped file under the "Recent proofs"
    heading, and the full-page and htmx-refresh paths cannot disagree about which population is on
    screen, because they compute it here.

    `match_count` is the TRUE number of matches, not the number of rows returned; the partial
    compares it against the rows it was given to decide whether to disclose the truncation.
    """
    if q:
        rows, total = await _search_tracked_files(session, user, q)
        return {
            "results": [_verify_row_view(fe, c) for fe, c in rows],
            "q": q,
            "match_count": total,
        }
    recent = await _recent_proofs_query(session, user, 5)
    return {
        "results": [_verify_row_view(fe, c) for fe, c in recent],
        "q": "",
        "match_count": len(recent),
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
    q: str = Query(""),
):
    ctx = await _base_context(request, session, user, "verify")
    # The searchable population, and therefore what the input may claim to search: every tracked
    # file this user owns. Counting anchored files here (what this used to do) advertised a search
    # over proofs while the box searches paths — an operator whose files are all unstamped read
    # "Search 0 files…" over a working search.
    total_tracked = await session.scalar(
        select(func.count())
        .select_from(FileEntry)
        .join(Collection, FileEntry.collection_id == Collection.id)
        .where(Collection.user_id == user.id)
    )
    ctx["total_tracked"] = int(total_tracked or 0)
    # `q` arrives from the top-bar search form (a plain GET navigation, design D1) as well as from
    # a bookmarked/refreshed search URL; the htmx live search takes over for refinement from there.
    ctx.update(await _results_context(session, user, q.strip()))
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
    ctx = await _results_context(session, user, q.strip())
    ctx["csrf_token"] = generate_csrf_token(request)
    return templates.TemplateResponse(request, "partials/verify_results.html", ctx)


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
    # `files.ots_digest` — the digest the proof CAIRN PLACED at `ots_path` commits to — is what
    # settles this where sprint 1 had to leave it undecidable (design D7). The provenance branches
    # are ORDERED, and the order is the correctness content: "the .ots at this path is not the proof
    # Cairn recorded placing" is asked BEFORE any staleness reading. A staleness-first ladder
    # produces the A/B/C false reassurance — recorded provenance `A`, live == baseline `B`, an
    # on-disk proof committing to a third digest `C`: `ots_digest != live` holds, so it would report
    # "simply an older proof of your file" about an `.ots` that is neither the recorded proof nor
    # this file's proof. The staleness reading is reachable only once the on-disk proof has been shown
    # to commit to exactly the recorded provenance (`proof_digest == ots_digest`) — and even then it
    # establishes only WHICH DIGEST that `.ots` commits to, never that it is the artifact Cairn
    # wrote nor that its attestations are good (design D7).
    #
    # With NO recorded provenance (a legacy row) nothing changes: a scan overwrites `files.sha256`
    # with the newly observed bytes BEFORE a replacement proof exists, so in the
    # modified-awaiting-re-stamp window the live bytes equal the recorded baseline while the still
    # valid, still current proof commits to the previous version — and sprint 1's split by the row's
    # own state (`pending`, or a `modified`/`new` status) plus its explicit undecidability survive
    # verbatim. Row 6 of D7 — provenance recorded but nothing parsed — falls through to that same
    # undecidable wording rather than to staleness, because with no parsed digest neither reading is
    # established.
    stored_sha = (fe.sha256 or "").strip().lower()
    live_sha = (digest or "").strip().lower()
    recorded_proof_sha = (fe.ots_digest or "").strip().lower()
    parsed_proof_sha = ((result.proof_digest if result else None) or "").strip().lower()
    # Whether a re-stamp is actually owed. Two different questions, deliberately not one variable:
    #
    #   * `restamp_heuristic` — sprint 1's reading of a row with NO recorded provenance. There,
    #     `modified`/`new` is the only signal that the row is in the re-stamp window at all, and it
    #     is what selects the legacy staleness wording. Unchanged.
    #   * `restamp_owed` — what the CARD is allowed to claim. With provenance, staleness is
    #     established from the digests alone, and a `modified` status no longer implies a queued
    #     re-stamp: a `perfile` collection switched to `ots_mode="none"` after a modification sits
    #     at `status="modified"`, `ots_state="complete"` forever and nothing will ever stamp it.
    #     Promising "a re-stamp is still pending" there is a promise the deployment cannot keep, so
    #     with provenance the clause comes strictly from `ots_state == "pending"`.
    restamp_heuristic = fe.ots_state == "pending" or fe.status in ("modified", "new")
    mismatch_blame = None
    # Whether the verdict rests on RECORDED provenance (an established finding) or on sprint 1's
    # heuristic (explicitly undecidable). The two get different copy.
    proof_provenance = False
    if result is not None and result.digest_mismatch and live_sha:
        if not stored_sha:
            mismatch_blame = "unknown"
        elif live_sha != stored_sha:
            mismatch_blame = "file"
        elif recorded_proof_sha and parsed_proof_sha and parsed_proof_sha != recorded_proof_sha:
            # D7 row 3 — the first provenance branch, and it must stay first.
            mismatch_blame, proof_provenance = "proof", True
        elif recorded_proof_sha and recorded_proof_sha == live_sha:
            # D7 row 4 — Cairn recorded placing a proof for exactly these bytes; the stored proof
            # disagrees with them. Reachable without a parsed digest.
            mismatch_blame, proof_provenance = "proof", True
        elif recorded_proof_sha and parsed_proof_sha == recorded_proof_sha:
            # D7 row 5 — the on-disk proof commits to the digest Cairn recorded for the proof it
            # placed here, i.e. to the file's PREVIOUSLY recorded fingerprint. That is a fact about
            # which digest the artifact commits to, and nothing more: digest equality is not
            # artifact identity (any `.ots` over the same bytes matches), and no attestation was
            # validated on this path — the ladder exits on the digest disagreement first. The copy
            # on both surfaces states exactly that and no more (design D7).
            mismatch_blame, proof_provenance = "proof-stale", True
        elif not recorded_proof_sha and restamp_heuristic:
            # D7 row 7 — legacy heuristic, unchanged.
            mismatch_blame = "proof-stale"
        else:
            # D7 rows 6 and 7's fallback: provenance recorded but nothing parsed, or no provenance
            # at all and no re-stamp owed. Undecidable either way — never staleness.
            mismatch_blame = "proof"

    # Only now that provenance is settled can the pending clause be derived: established staleness
    # says nothing about whether a re-stamp is queued (design D7).
    restamp_owed = (fe.ots_state == "pending") if proof_provenance else restamp_heuristic

    # Whether re-running this exact check could produce a different answer. Set ONLY by the two
    # branches where it can (design D8), inside the ladder rather than from the reason flags,
    # because the flags travel on every branch: a transport failure under a *verified* verdict is
    # disclosed as a diagnostic note, but the verdict itself is settled and a Retry beside it would
    # present a finished answer as provisional.
    can_retry = False

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
        # The stored proof commits to the file's earlier fingerprint rather than to its current one
        # — established from the recorded provenance, or (legacy rows) inferred from the row saying
        # a re-stamp is owed. Either way it is the expected state of that window, not a fault. Amber
        # like the other "a stamp is owed" verdicts — red here would train the operator to dismiss
        # the red card that means a real mismatch.
        #
        # The two headlines differ because the two findings differ. With provenance the card may
        # name only what was compared — which digest the `.ots` commits to — since neither the
        # artifact's authorship nor its attestations were checked here; "predates" would smuggle in
        # an age claim that a substituted proof over the same earlier bytes satisfies just as well.
        # The legacy heuristic keeps sprint 1's wording verbatim.
        verdict = "warn"
        title = (
            "Proof commits to the previously recorded fingerprint"
            if proof_provenance
            else "Proof predates this version of the file"
        )
    elif mismatch_blame == "proof" and proof_provenance:
        # Established: the `.ots` at this path is not the proof Cairn recorded placing for these
        # bytes. A positive finding about the PROOF — and still no claim about the file, whose
        # bytes match their baseline.
        verdict = "danger"
        title = "This is not the proof Cairn placed"
    elif mismatch_blame == "proof":
        # The live bytes still hash to the recorded baseline and the row claims no re-stamp is
        # owed. Without recorded provenance that is all that is established: this card describes it
        # and blames nothing (see the tiebreaker note above).
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
        can_retry = True  # the backend was never reached; reaching it changes the answer
    elif result is not None and result.inconclusive:
        verdict = "unavailable"
        title = "Couldn't confirm — pending, changed, or unreachable"
        can_retry = True  # the backend cannot separate unreachability from its other outcomes
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
        # The collection's notarization mode, so the never-stamped card's advice is an action the
        # operator can actually take: a tripwire (`none`) collection has no "Stamp all" control and
        # its stamp route refuses it, so telling its owner to use one sends them looking for a
        # button that does not exist — the same silent dead end #41 is about, one surface along.
        "collection_ots": collection.ots_mode,
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
        # Whether the `proof` / `proof-stale` verdict rests on RECORDED provenance (`files.ots_digest`)
        # or on sprint 1's heuristic. The two get different copy: with provenance the card states a
        # positive finding about the proof; without it, sprint 1's "Cairn cannot tell which" stands
        # verbatim. Derived here, never in the template — the template cannot see `ots_digest`.
        "proof_provenance": proof_provenance,
        # Only true when a re-stamp is actually owed, so `proof-stale`'s "a re-stamp is pending"
        # clause is never asserted about a row where none is queued (design D7).
        "restamp_owed": restamp_owed,
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
        # NO generic `message` key, deliberately (design D8). `result.message` is the backend's
        # general result string and travels on EVERY branch, so rendering it generically would print
        # backend text under a verdict — a `mismatch_blame` attribution above, say — whose entire
        # design is that the card states exactly what the check established and no more. That is the
        # one place in this codebase where an extra sentence is a correctness bug. The reasons the
        # card IS allowed to name are typed and carried individually (`transport_error`,
        # `live_unavailable`, `unreadable_proof`, …); a new one gets its own key, never this.
        #
        # Set by the verdict ladder above, never re-derived here: only the transport-failure and
        # inconclusive verdicts can change on a retry. NOT never-notarized, queued-to-stamp, an
        # unreadable proof, or any digest disagreement — those re-run to the same answer, and a
        # Retry button beside a settled finding presents it as provisional.
        "can_retry": can_retry,
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
