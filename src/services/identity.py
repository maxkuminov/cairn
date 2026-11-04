"""Pinned OIDC identity links on ``users`` (oidc-sso-login, design D5).

The ``(oidc_issuer, oidc_subject)`` pair is the only thing ever matched at login; email, username
and every other claim are never used for admission. Linking happens only here (driven by the
``cairn users`` CLI): there is no just-in-time provisioning.

Every mutating call validates its inputs *before* writing, so the CLI fails with a one-line
``IdentityError`` rather than a constraint error (the schema CHECKs and the unique index are the
backstop for any other writer), bumps ``session_epoch`` so every existing session of that user is
refused on its next request, and commits exactly once.
"""

from __future__ import annotations

import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..models.db import User, utcnow

ISSUER_MAX_LEN = 512
SUBJECT_MAX_LEN = 255


class IdentityError(Exception):
    """A refused identity operation. ``str(exc)`` is a single line, safe to print."""

    def __init__(self, msg: str) -> None:
        super().__init__(" ".join(str(msg).split()) or "identity operation refused")


def _has_control(value: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def validate_subject(subject: str | None) -> str:
    if subject is None or subject == "":
        raise IdentityError("subject must not be empty")
    if len(subject) > SUBJECT_MAX_LEN:
        raise IdentityError(f"subject is longer than {SUBJECT_MAX_LEN} characters")
    if _has_control(subject):
        raise IdentityError("subject must not contain control characters")
    return subject


def validate_issuer(issuer: str | None) -> str:
    """``https``, a host, no userinfo, query or fragment, at most 512 characters."""
    if issuer is None or issuer == "":
        raise IdentityError("issuer must not be empty")
    if len(issuer) > ISSUER_MAX_LEN:
        raise IdentityError(f"issuer is longer than {ISSUER_MAX_LEN} characters")
    if _has_control(issuer) or any(ch.isspace() for ch in issuer):
        raise IdentityError("issuer must not contain whitespace or control characters")
    if "?" in issuer or "#" in issuer:
        raise IdentityError("issuer must not carry a query or fragment")
    try:
        parts = urlsplit(issuer)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError as exc:
        raise IdentityError("issuer is not a valid URL") from exc
    if parts.scheme.lower() != "https":
        raise IdentityError("issuer must be an https URL")
    if "@" in parts.netloc:
        raise IdentityError("issuer must not carry userinfo")
    if not hostname:
        raise IdentityError("issuer must name a host")
    return issuer


def _default_issuer() -> str:
    """The configured issuer — only in ``oidc`` mode; outside it the OIDC settings are ignored."""
    settings = get_settings()
    if getattr(settings, "auth_mode", None) != "oidc":
        raise IdentityError("--issuer is required outside oidc mode")
    configured = getattr(settings, "oidc_issuer", None)
    if not configured:
        raise IdentityError("--issuer is required: CAIRN_OIDC_ISSUER is not set")
    return str(configured)


async def _resolve_user(session, username: str | None) -> User:
    if username is not None:
        user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            raise IdentityError(f"no user named {username!r}")
        return user
    count = await session.scalar(select(func.count()).select_from(User))
    if not count:
        raise IdentityError("no user in the database")
    if count > 1:
        raise IdentityError("more than one user exists: pass --user NAME")
    return await session.scalar(select(User).order_by(User.id).limit(1))


async def _commit(session, user: User) -> User:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise IdentityError("refused by the database (identity already linked?)") from exc
    return user


async def find_linked_user(session, issuer: str, subject: str) -> User | None:
    """The user linked to exactly this ``(issuer, subject)`` pair, or ``None``."""
    if not issuer or not subject:
        return None
    return await session.scalar(
        select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject)
    )


async def link(
    session,
    *,
    subject: str,
    issuer: str | None,
    username: str | None = None,
    replace: bool = False,
) -> User:
    """Pin ``(issuer, subject)`` to a user and bump its ``session_epoch``.

    ``issuer=None`` defaults to ``CAIRN_OIDC_ISSUER`` in ``oidc`` mode only. The issuer is
    validated whether given or defaulted. ``username=None`` means the only user (refused when more
    than one exists). Relinking an already-linked user needs ``replace=True``; a pair already
    linked to *another* user is always refused.
    """
    subject = validate_subject(subject)
    issuer = validate_issuer(_default_issuer() if issuer is None else issuer)
    user = await _resolve_user(session, username)

    owner = await find_linked_user(session, issuer, subject)
    if owner is not None and owner.id != user.id:
        raise IdentityError("this issuer and subject are already linked to another user")
    if user.oidc_subject is not None and not replace:
        raise IdentityError(f"user {user.username!r} is already linked: pass --replace to relink")

    user.oidc_issuer = issuer
    user.oidc_subject = subject
    user.oidc_linked_at = utcnow()
    user.session_epoch = (user.session_epoch or 0) + 1
    return await _commit(session, user)


async def unlink(session, *, username: str | None = None) -> User:
    """Clear the user's identity triple and bump its ``session_epoch``."""
    user = await _resolve_user(session, username)
    if user.oidc_subject is None and user.oidc_issuer is None:
        raise IdentityError(f"user {user.username!r} is not linked")
    user.oidc_issuer = None
    user.oidc_subject = None
    user.oidc_linked_at = None
    user.session_epoch = (user.session_epoch or 0) + 1
    return await _commit(session, user)


async def revoke_sessions(session, *, username: str | None = None) -> User:
    """Bump ``session_epoch`` only: every existing session of the user is refused next request."""
    user = await _resolve_user(session, username)
    user.session_epoch = (user.session_epoch or 0) + 1
    return await _commit(session, user)
