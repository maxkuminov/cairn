"""CSRF protection (adapted from obsidian_mcp's ``csrf.py``).

An itsdangerous-signed token bound to a per-session nonce; validated with a timing-safe compare.
The token is rendered into templates and echoed back via the ``X-CSRF-Token`` header (htmx) or a
``csrf_token`` form field. Cairn resolves the signing key from settings, falling back to a stable
dev key in single-user mode where ``CAIRN_SECRET_KEY`` is optional.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings

_MAX_AGE = 3600
# Stable fallback so single-user installs (where CAIRN_SECRET_KEY is optional) still sign tokens
# consistently within a process. Multi-user mode requires a real CAIRN_SECRET_KEY (config enforces).
_DEV_FALLBACK_KEY = "cairn-single-user-dev-key"


def _secret_key() -> str:
    return get_settings().secret_key or _DEV_FALLBACK_KEY


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt="csrf-token")


def generate_csrf_token(request: Request) -> str:
    try:
        session = request.session
    except (AssertionError, AttributeError):
        return ""
    if "csrf_nonce" not in session:
        session["csrf_nonce"] = secrets.token_hex(16)
    return _serializer().dumps(session["csrf_nonce"])


def validate_csrf_token(request: Request, token: str | None) -> bool:
    try:
        session = request.session
    except (AssertionError, AttributeError):
        return False
    nonce = session.get("csrf_nonce")
    if nonce is None:
        return False
    if not token:
        return False
    try:
        payload = _serializer().loads(token, max_age=_MAX_AGE)
        return secrets.compare_digest(payload, nonce)
    except (BadSignature, SignatureExpired):
        return False


async def verify_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.headers.get("x-csrf-token")
    if token is None:
        try:
            form = await request.form()
        except Exception:
            form = {}
        token = form.get("csrf_token")
    if not validate_csrf_token(request, token):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
