"""Cairn FastAPI application + lifespan.

The lifespan opens the datastore, migrates (when enabled), bootstraps the single-user row, and
starts the background scan scheduler (unless disabled). ``/healthz`` reports datastore liveness
plus per-collection scan freshness so an external monitor can poll it as a dead-man's switch. The
full control panel lands in a later change; here the panel is a placeholder.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .config import get_settings
from .database import (
    ensure_dirs,
    ensure_implicit_user,
    get_engine,
    get_sessionmaker,
    ping,
    run_migrations,
)

logger = logging.getLogger("cairn")

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "control_panel" / "static"


SCHEDULER_STOP_TIMEOUT = 10.0  # seconds to wait for the loop to wind down on shutdown


async def start_scheduler(app: FastAPI) -> None:
    """Start the background scan loop (unless disabled), storing it on ``app.state``."""
    settings = get_settings()
    if not settings.scheduler_enabled:
        logger.info("scheduler disabled (CAIRN_SCHEDULER_ENABLED=0) — not starting loop")
        return
    from .services.scheduler import scheduler_loop

    stop_event = asyncio.Event()
    app.state.scheduler_stop = stop_event
    app.state.scheduler_task = asyncio.create_task(scheduler_loop(app, stop_event))
    logger.info("scheduler started (scan tick=%ss)", settings.scan_interval_seconds)


async def stop_scheduler(app: FastAPI) -> None:
    """Signal the loop to stop and await it, cancelling if it overruns the grace period."""
    task = getattr(app.state, "scheduler_task", None)
    stop_event = getattr(app.state, "scheduler_stop", None)
    if task is None:
        return
    if stop_event is not None:
        stop_event.set()
    try:
        await asyncio.wait_for(task, timeout=SCHEDULER_STOP_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("scheduler did not stop in time; cancelling")
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        app.state.scheduler_task = None
        app.state.scheduler_stop = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    ensure_dirs()
    if settings.auto_migrate:
        # Run migrations off the event loop (alembic is synchronous).
        await asyncio.to_thread(run_migrations)
    try:
        async with get_sessionmaker()() as session:
            await ensure_implicit_user(session)
    except Exception:  # pragma: no cover - surfaced clearly to the operator
        logger.exception(
            "Startup bootstrap failed — has the database been migrated? "
            "Run `cairn init` or set CAIRN_AUTO_MIGRATE=1."
        )
        raise
    # Reconcile runs left at 'running' by a previous crash/kill — those that have stopped reporting
    # progress; a live CLI stamp/upgrade keeps its claim (design D10) — so no collection shows a
    # perpetual in-progress badge or blocks a new op. The scheduler repeats this every tick and a
    # blocked claim reconciles in-band (collections.reclaim_stale_claim), so this is the first of
    # three reclamation paths, not the only one.
    try:
        from .services.scheduler import reap_orphaned_runs

        async with get_sessionmaker()() as session:
            reaped = await reap_orphaned_runs(session)
        if reaped:
            logger.info("reaped %d orphaned running run(s) on startup", reaped)
    except Exception:  # pragma: no cover - best-effort; must not block startup
        logger.exception("orphaned-run reaper failed")
    await start_scheduler(app)
    logger.info("Cairn startup complete (mode=%s, version=%s)", settings.auth_mode, __version__)
    try:
        yield
    finally:
        await stop_scheduler(app)
        await get_engine().dispose()


app = FastAPI(title="Cairn", version=__version__, lifespan=lifespan)
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Sessions back the CSRF nonce + (later) login. In single-user mode CAIRN_SECRET_KEY is optional,
# so fall back to a stable dev key; multi-user mode requires a real key (enforced in config).
_session_secret = get_settings().secret_key or "cairn-single-user-dev-key"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness + per-collection scan freshness, for an external dead-man's-switch monitor.

    Returns 503 ``error`` when the datastore is unreachable **or the freshness read fails for any
    other reason**, 503 ``degraded`` when any collection is stale, and 200 ``ok`` only when reachable
    AND no collection is stale. Every failure mode answers in the same parseable shape: a monitor
    that receives an unstructured 500 learns nothing about the installation it is watching.

    **Fleet-global, deliberately.** ``compute_health`` is called with no ``user_id``: this endpoint
    monitors the *installation*, not a user, and a machine-facing dead-man's switch that silently
    skipped one owner's collections would have a hole in it exactly where nobody is looking. The
    panel scopes its own health to the viewer (design D5); the two surfaces answer different
    questions and only this one is unauthenticated.

    Each per-collection object carries the collection's ``id`` alongside its name, so a caller can
    match a freshness record to the collection it describes without matching on a name, which no
    constraint makes unique. That key is additive; nothing was renamed, retyped or removed.

    Two *values* can change relative to earlier releases, both toward reporting rather than
    reassurance (design D13): a collection whose only recent scan run is a ``running`` row with an
    **abandoned** claim now reports ``stale`` rather than ``fresh``, and ``last_scan_age_seconds``
    describes the newest **completed** scan (``null`` when there is none) rather than an unfinished
    run's elapsed time.
    """
    from .services.scheduler import compute_health

    settings = get_settings()
    unreachable = JSONResponse(
        {"status": "error", "mode": settings.auth_mode, "version": __version__},
        status_code=503,
    )

    # ONE datastore-error boundary around the whole read, not just around `ping()`. The probe and
    # the freshness queries are two separate trips to the datastore: a database that answers the
    # first and fails the second (or a session that cannot be opened at all, or a freshness SELECT
    # that raises) is exactly the outage this endpoint exists to report. Leaving that gap open
    # returned a bare, unstructured HTTP 500 — a body the polling monitor cannot parse, carrying no
    # `status`, no `mode` and no `version`, and indistinguishable from a reverse proxy's own error.
    # `Exception`, deliberately, not `SQLAlchemyError`: the contract of a dead-man's switch is that
    # every way it can fail to answer produces the SAME structured "error" verdict. A driver-level
    # OSError, a cancelled connection or a bug in the freshness read must not be the one path that
    # degrades into an unreadable 500.
    try:
        if not await ping():
            return unreachable
        async with get_sessionmaker()() as session:
            report = await compute_health(session, settings)
    except Exception:
        logger.warning("/healthz: health computation failed — reporting error", exc_info=True)
        return unreachable

    body = {
        "status": report.status,
        "mode": settings.auth_mode,
        "version": __version__,
        "collections": [
            {
                "id": c.id,
                "name": c.name,
                "state": c.state,
                "last_scan_age_seconds": c.last_scan_age_seconds,
            }
            for c in report.collections
        ],
    }
    return JSONResponse(body, status_code=200 if report.status == "ok" else 503)


# Mount the control panel (dashboard at /, collection detail, add/edit, verify, settings).
from .control_panel.routes import router as panel_router  # noqa: E402
from .control_panel.routes import templates as panel_templates  # noqa: E402

app.include_router(panel_router)


# --- branded HTML errors for panel navigations ----------------------------------------------

# Paths whose callers are machines, not browsers: they keep the JSON error body whatever the
# request claims to accept. `/healthz` in particular is polled by an external dead-man's-switch
# monitor that parses the body.
_JSON_ONLY_PREFIXES = ("/api", "/healthz")

_ERROR_HEADINGS = {
    403: "Not allowed",
    404: "Page not found",
    409: "That could not be done",
}

# A stale alert deep link is the journey this handler exists for: an old email tapped on a phone,
# pointing at a collection that has since been deleted (or at a different Cairn instance).
_NOT_FOUND_EXPLANATION = (
    "This collection no longer exists, or the link is from a different Cairn instance. "
    "Nothing is wrong with your files — only the address is stale."
)


def _explanation(status_code: int, path: str) -> str:
    if status_code != 404:
        return "Cairn could not complete that request."
    # Only a collection URL warrants the collection wording; a mistyped panel path is its own case.
    if path.startswith("/collection"):
        return _NOT_FOUND_EXPLANATION
    return "That address does not exist in this Cairn panel."


def _wants_html(request: Request) -> bool:
    """True for a browser navigation. htmx/fetch/CLI callers send */* or JSON and keep JSON."""
    return "text/html" in request.headers.get("accept", "")


@app.exception_handler(StarletteHTTPException)
async def panel_error_handler(request: Request, exc: StarletteHTTPException):
    """Render panel errors as a branded page; leave the API/JSON contract untouched.

    Without this a stale `/collection/{id}/review` link — exactly what alerts now hand out —
    dead-ends in a raw `{"detail": ...}` blob with no branding and no way back.
    """
    path = request.url.path
    is_json_client = path.startswith(_JSON_ONLY_PREFIXES) or not _wants_html(request)
    if is_json_client:
        return await http_exception_handler(request, exc)

    detail = exc.detail if isinstance(exc.detail, str) else ""
    ctx = {
        "status_code": exc.status_code,
        "heading": _ERROR_HEADINGS.get(exc.status_code, "Something went wrong"),
        "explanation": _explanation(exc.status_code, path),
        # Shell defaults: the error page must render without touching the datastore, since the
        # datastore may be exactly what failed.
        "page": "",
        "mode": "dark" if request.cookies.get("cairn_mode") == "dark" else "light",
        "auth_mode": get_settings().auth_mode,
        "username": "",
        "is_admin": False,
        "user_email": "",
        "sidebar_collections": [],
        "alert_count": 0,
        "csrf_token": "",
        "detail": detail,
    }
    return panel_templates.TemplateResponse(
        request, "error.html", ctx, status_code=exc.status_code, headers=exc.headers
    )
