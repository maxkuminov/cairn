"""The `oidc` login wall: default-deny, re-validated on every request (oidc-sso-login, design D6).

A pure ASGI middleware that sits *inside* `SessionMiddleware` (so `request.session` is the decoded
cookie) and in front of every route. Only an explicit public allowlist passes without a session;
every other path, including any route added later, needs a session minted by the OIDC callback and
still valid against the `users` row:

* the user exists and is active;
* it is still linked (to the configured issuer);
* its `session_epoch` equals the session's `epoch` (`cairn users unlink|revoke-sessions` bump it);
* the login is younger than `CAIRN_OIDC_SESSION_MAX_AGE` (absolute, not sliding).

Any failure clears the session and refuses: a browser GET gets `303 /login?next=<local path>`, an
htmx request `401` + `HX-Redirect: /login`, anything under `/api` a JSON `401`, and any other
request a `303 /login`. The validated user is left on `request.state` (`cairn_user_id`,
`cairn_username`) so the routes and the branded error page never re-derive it from the cookie.

The wall is installed only in `oidc` mode; `single` mode has no wall at all.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import get_settings

logger = logging.getLogger("cairn.auth")

# Exact public paths, plus everything under /static/. Nothing else is reachable without a session.
PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/login",
        "/auth/oidc/start",
        "/auth/oidc/callback",
        "/auth/refused",
        "/auth/logout",
        "/mode/toggle",
    }
)
PUBLIC_PREFIXES = ("/static/",)

# Session keys an authenticated `oidc` session holds (plus the CSRF nonce). Never a subject, a
# token or a claim.
SESSION_KEYS = ("uid", "epoch", "auth", "login_at")


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_local_path(value: object) -> str | None:
    """THE local-path rule, shared by `next`, the callback redirect and `/mode/toggle`.

    Returns ``value`` when it is a local absolute path — a single leading ``/``, not ``//``, no
    backslash anywhere, no whitespace or control character (browsers strip a tab or newline, which
    can turn ``/\\t/host`` into ``//host``) — else ``None``. A scheme cannot occur: the value must
    start with ``/``.
    """
    if not isinstance(value, str) or not value.startswith("/"):
        return None
    if value.startswith("//") or "\\" in value:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def local_target_from_referer(referer: str | None) -> str:
    """The `/mode/toggle` destination: the Referer's path + query when it passes the rule, else `/`.

    An absolute URL contributes only its path and query, whatever its origin, so the response can
    never name another origin; a scheme-less value must itself pass the local-path rule (a
    protocol-relative ``//host/x`` does not).
    """
    raw = referer or ""
    if not raw or "\\" in raw:
        return "/"
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "/"
    if parts.scheme:
        if parts.scheme.lower() not in ("http", "https"):
            return "/"
        candidate = parts.path + (f"?{parts.query}" if parts.query else "")
    else:
        candidate = raw.split("#", 1)[0]
    return safe_local_path(candidate) or "/"


def login_redirect_target(request: Request) -> str:
    """`/login?next=<this request's path+query>` when that is a safe local path, else `/login`."""
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    nxt = safe_local_path(target)
    if not nxt:
        return "/login"
    return "/login?" + urlencode({"next": nxt}, safe="/")


async def load_valid_user(session_data: dict) -> tuple[int, str] | None:
    """Re-validate an `oidc` session against the `users` row. ``(id, username)`` or ``None``."""
    settings = get_settings()
    if session_data.get("auth") != "oidc":
        return None
    uid = session_data.get("uid")
    epoch = session_data.get("epoch")
    login_at = session_data.get("login_at")
    if not (isinstance(uid, int) and isinstance(epoch, int) and isinstance(login_at, int)):
        return None
    if isinstance(uid, bool) or isinstance(epoch, bool) or isinstance(login_at, bool):
        return None
    now = int(time.time())
    if login_at > now + 60:  # a login time from the future is not a session this process minted
        return None
    if now - login_at >= settings.oidc_session_lifetime:
        return None

    from ..database import get_sessionmaker
    from ..models.db import User

    async with get_sessionmaker()() as db:
        user = await db.get(User, uid)
        if user is None or not user.is_active:
            return None
        if user.oidc_subject is None or user.oidc_issuer != settings.oidc_issuer:
            return None
        if user.session_epoch != epoch:
            return None
        return user.id, user.username


def refusal(request: Request) -> Response:
    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    if request.headers.get("hx-request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method in ("GET", "HEAD"):
        return RedirectResponse(login_redirect_target(request), status_code=303)
    return RedirectResponse("/login", status_code=303)


class LoginWall:
    """Default-deny ASGI middleware. Must be added *before* `SessionMiddleware` (so it runs inside)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        request = Request(scope)
        session = scope.get("session")
        if is_public(path):
            # Public paths still learn who is signed in (the error page and `/login` use it), but a
            # failed check there clears nothing and refuses nothing.
            if isinstance(session, dict) and session.get("auth") == "oidc":
                try:
                    found = await load_valid_user(session)
                except Exception:  # noqa: BLE001 - a datastore outage must not 500 a public path
                    found = None
                if found:
                    request.state.cairn_user_id, request.state.cairn_username = found
            await self.app(scope, receive, send)
            return

        found = None
        if isinstance(session, dict):
            found = await load_valid_user(session)
        if found is None:
            if isinstance(session, dict) and session:
                session.clear()
            if scope["type"] == "websocket":  # pragma: no cover - the panel has no websockets
                await send({"type": "websocket.close", "code": 1008})
                return
            await refusal(request)(scope, receive, send)
            return
        request.state.cairn_user_id, request.state.cairn_username = found
        await self.app(scope, receive, send)
