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

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
    # Reconcile runs orphaned at 'running' by a previous crash/kill (a restarted process cannot
    # still be running them) so no collection shows a perpetual in-progress badge or blocks a new op.
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

    Returns 503 ``error`` when the datastore is unreachable, 503 ``degraded`` when any collection is
    stale, and 200 ``ok`` only when reachable AND no collection is stale.
    """
    from .services.scheduler import compute_health

    settings = get_settings()
    if not await ping():
        return JSONResponse(
            {"status": "error", "mode": settings.auth_mode, "version": __version__},
            status_code=503,
        )

    async with get_sessionmaker()() as session:
        report = await compute_health(session, settings)

    body = {
        "status": report.status,
        "mode": settings.auth_mode,
        "version": __version__,
        "collections": [
            {
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

app.include_router(panel_router)
