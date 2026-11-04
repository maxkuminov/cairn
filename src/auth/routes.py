"""Login routes for the `oidc` mode (oidc-sso-login, design D6/D7/D11).

``GET /login``, ``POST /auth/oidc/start``, ``GET /auth/oidc/callback``, ``GET /auth/refused`` and
``POST /auth/logout``. Mounted only in ``oidc`` mode; all five are on the wall's public allowlist.

The callback never renders at its own URL: success is ``303 → next`` with a fresh session, a
protocol failure ``303 → /login?error=failed``, an identity refusal (unlinked, inactive, group)
``303 → /auth/refused`` carrying a one-shot ``refused`` value in the (cleared, anonymous) session.
Logs carry a closed reason code only (``src.auth.oidc.REASONS``); the success line names the local
username. Nothing here logs a query string, code, token, state, nonce, verifier, secret, subject or
claim.
"""

from __future__ import annotations

import logging
import secrets
import time

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..csrf import generate_csrf_token, verify_csrf
from .oidc import TXN_RETRY_AFTER, OIDCClient, OIDCError
from .wall import safe_local_path

logger = logging.getLogger("cairn.auth")

router = APIRouter(tags=["auth"])

NO_STORE_HEADERS = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
REFUSED_TTL = 300


def _templates():
    from ..control_panel.routes import templates

    return templates


def _mode(request: Request) -> str:
    return "dark" if request.cookies.get("cairn_mode") == "dark" else "light"


def _client(request: Request) -> OIDCClient:
    return request.app.state.oidc


def _addr(request: Request) -> str:
    return request.client.host if request.client else "-"


def _redirect(url: str, headers: dict[str, str] | None = None) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=headers)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    error = request.query_params.get("error")
    ctx = {
        "csrf_token": generate_csrf_token(request),
        "mode": _mode(request),
        "next": safe_local_path(request.query_params.get("next")),
        "error": error if error in ("failed", "busy") else None,
    }
    return _templates().TemplateResponse(
        request, "login.html", ctx, headers={"Cache-Control": "no-store"}
    )


@router.post("/auth/oidc/start", dependencies=[Depends(verify_csrf)])
async def oidc_start(request: Request, next: str = Form("/")) -> Response:  # noqa: A002
    client = _client(request)
    next_path = safe_local_path(next) or "/"
    if not client.limits.allow_start(_addr(request)):
        logger.warning("oidc login refused reason=rate")
        return _redirect("/login?error=busy", {"Retry-After": str(TXN_RETRY_AFTER)})
    try:
        provider = await client.provider()
    except OIDCError as exc:
        logger.warning("oidc login refused reason=%s", exc.reason)
        return _redirect("/login?error=failed")
    state, nonce, verifier = client.new_secrets()
    if not client.transactions.admit(state, nonce, verifier, next_path):
        logger.warning("oidc login refused reason=capacity")
        return _redirect("/login?error=busy", {"Retry-After": str(TXN_RETRY_AFTER)})
    # Bind the transaction to this browser: the callback must present the same state.
    request.session["oidc_state"] = state
    logger.info("oidc login started")
    return _redirect(client.authorization_url(provider, state, nonce, verifier), NO_STORE_HEADERS)


@router.get("/auth/oidc/callback")
async def oidc_callback(request: Request) -> Response:
    client = _client(request)
    failed = _redirect("/login?error=failed", NO_STORE_HEADERS)
    params = request.query_params

    def fail(reason: str) -> Response:
        logger.warning("oidc login failed reason=%s", reason)
        return failed

    if not client.limits.allow_callback(_addr(request)):
        return fail("rate")

    state = params.get("state") or ""
    bound = request.session.pop("oidc_state", None)
    if not state or not isinstance(bound, str) or not secrets.compare_digest(
        bound.encode("utf-8"), state.encode("utf-8")
    ):
        return fail("state")
    txn = client.transactions.consume(state)
    if txn is None:
        return fail("state")

    try:
        provider = await client.provider()
        client.check_iss_parameter(provider, params.get("iss"))
        code = params.get("code")
        if params.get("error") is not None or not code:
            raise OIDCError("token_exchange")
        id_token = await client.exchange(provider, code, txn.verifier)
        claims = await client.verify_id_token(provider, id_token, txn.nonce)
    except OIDCError as exc:
        return fail(exc.reason)
    # Provider material is discarded before any session write.
    del id_token, code
    issuer, subject = claims["iss"], claims["sub"]

    from ..database import get_sessionmaker
    from ..services.identity import find_linked_user

    async with get_sessionmaker()() as db:
        user = await find_linked_user(db, issuer, subject)
        reason = None
        if user is None:
            reason = "unlinked"
        elif not user.is_active:
            reason = "inactive"
        elif not client.group_ok(claims):
            reason = "group"
        if reason is not None:
            logger.warning("oidc login refused reason=%s", reason)
            request.session.clear()
            request.session["refused"] = {
                "iss": issuer,
                "sub": subject,
                "exp": int(time.time()) + REFUSED_TTL,
            }
            return _redirect("/auth/refused", NO_STORE_HEADERS)
        uid, epoch, username = user.id, user.session_epoch, user.username
    del claims

    # A fresh session: nothing from the pre-login session (its CSRF nonce included) carries over.
    request.session.clear()
    request.session.update(
        {
            "uid": uid,
            "epoch": epoch,
            "auth": "oidc",
            "login_at": int(time.time()),
            "csrf_nonce": secrets.token_hex(16),
        }
    )
    logger.info("oidc login succeeded user=%s", username)
    return _redirect(safe_local_path(txn.next) or "/", NO_STORE_HEADERS)


@router.get("/auth/refused", response_class=HTMLResponse)
async def auth_refused(request: Request) -> Response:
    value = request.session.pop("refused", None)
    issuer = subject = None
    if (
        isinstance(value, dict)
        and isinstance(value.get("exp"), int)
        and value["exp"] > time.time()
        and isinstance(value.get("iss"), str)
        and isinstance(value.get("sub"), str)
    ):
        issuer, subject = value["iss"], value["sub"]
    ctx = {"mode": _mode(request), "issuer": issuer, "subject": subject}
    return _templates().TemplateResponse(
        request, "auth_refused.html", ctx, status_code=403, headers=dict(NO_STORE_HEADERS)
    )


@router.post("/auth/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request) -> Response:
    username = getattr(request.state, "cairn_username", None)
    request.session.clear()
    if username:
        logger.info("oidc logout user=%s", username)
    return _redirect("/login")
