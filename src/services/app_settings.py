"""Global, UI-editable app settings backed by the ``app_settings`` key-value table.

Today this stores the SMTP server config (host/port/TLS/user/password/from/provider) and the
panel's ``public_url`` so both can be configured from the panel instead of env-only. Values are
persisted as TEXT and coerced back to the typed :class:`~src.config.Settings` fields on read.

Precedence: **DB overrides env.** :func:`effective_settings` overlays any stored values onto the
env-derived :class:`Settings`, so an empty table falls back to ``CAIRN_SMTP_*`` (existing deploys
keep working) and a value set in the UI takes effect with no restart (no ``get_settings()`` cache to
bust — the overlay happens at read time).

``model_copy(update=...)`` does **not** re-run the model's validators, so anything with a grammar
is re-validated here on every read. A stored ``public_url`` that fails validation is dropped (the
env value then applies as though no row existed) — an unvalidated value must never reach an
outbound alert, whatever put it in the table: a panel save, a hand edit, a restored backup, or
corruption.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import AppSetting
from .panel_url import normalize_public_url

_log = logging.getLogger("cairn.app_settings")

if TYPE_CHECKING:
    from ..config import Settings

# DB keys == the matching ``Settings`` field names, so overrides slot straight into model_copy().
SMTP_FIELDS: tuple[str, ...] = (
    "smtp_host",
    "smtp_port",
    "smtp_starttls",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "email_provider",
)

# Everything the DB may override, SMTP plus the rest. Kept separate from SMTP_FIELDS so that name
# keeps meaning what it says (the SMTP form reads and writes exactly those keys).
OVERLAY_FIELDS: tuple[str, ...] = SMTP_FIELDS + ("public_url",)

_TRUE = {"1", "true", "yes", "on"}


def _coerce(field: str, raw: str) -> Any:
    """Coerce a stored TEXT value back to the type ``Settings`` expects for ``field``."""
    if field == "smtp_port":
        return int(raw)
    if field == "smtp_starttls":
        return raw.strip().lower() in _TRUE
    return raw


async def _load(session: AsyncSession, fields: tuple[str, ...]) -> dict[str, Any]:
    """Stored values for ``fields``, typed and keyed by ``Settings`` field name.

    Keys with no stored row (or a NULL/uncoercible value) are omitted so the env default wins.
    """
    rows = await session.execute(select(AppSetting).where(AppSetting.key.in_(fields)))
    out: dict[str, Any] = {}
    for row in rows.scalars():
        if row.value is None:
            continue
        try:
            out[row.key] = _coerce(row.key, row.value)
        except (ValueError, TypeError):
            continue
    return out


async def get_smtp_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return just the SMTP fields set in the DB (what the SMTP settings form round-trips)."""
    return await _load(session, SMTP_FIELDS)


async def get_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return every DB-stored override, re-validated where the value has a grammar.

    ``public_url`` is passed back through :func:`normalize_public_url` on **every** read because
    the overlay below applies these via ``model_copy(update=...)``, which skips validators. A value
    that does not validate has its key dropped, so the env value applies as if no row existed.
    """
    out = await _load(session, OVERLAY_FIELDS)
    if "public_url" in out:
        stored = out["public_url"]
        try:
            normalized = normalize_public_url(stored)
        except ValueError as exc:
            _log.warning("stored public_url ignored (%s): %r — using CAIRN_PUBLIC_URL", exc, stored)
            normalized = None
        if normalized is None:
            # Drop, never override-with-None: an empty/invalid row must not shadow the env value.
            del out["public_url"]
        else:
            out["public_url"] = normalized
    return out


async def effective_settings(session: AsyncSession, base: "Settings") -> "Settings":
    """``base`` (env/dotenv) with the DB-stored overrides applied on top (DB wins)."""
    overrides = await get_overrides(session)
    return base.model_copy(update=overrides) if overrides else base


async def _set(session: AsyncSession, key: str, value: str) -> None:
    obj = await session.get(AppSetting, key)
    if obj is None:
        session.add(AppSetting(key=key, value=value))
    else:
        obj.value = value  # onupdate refreshes updated_at on flush


async def smtp_password_is_set(session: AsyncSession) -> bool:
    """True when a non-empty SMTP password is stored in the DB (drives the form's '•••• set' hint)."""
    obj = await session.get(AppSetting, "smtp_password")
    return bool(obj and obj.value)


async def save_smtp(
    session: AsyncSession,
    *,
    host: str,
    port: int,
    starttls: bool,
    user: str,
    from_: str,
    provider: str,
    password: str | None = None,
) -> None:
    """Persist the SMTP server config. ``password=None`` keeps the stored secret unchanged.

    All other fields are written verbatim (an empty string explicitly clears that field, overriding
    any env value), so the saved form becomes the authoritative SMTP config.
    """
    await _set(session, "smtp_host", host.strip())
    await _set(session, "smtp_port", str(int(port)))
    await _set(session, "smtp_starttls", "1" if starttls else "0")
    await _set(session, "smtp_user", user.strip())
    await _set(session, "smtp_from", from_.strip())
    await _set(session, "email_provider", provider)
    if password is not None:
        await _set(session, "smtp_password", password)
    await session.commit()


async def save_public_url(session: AsyncSession, url: str | None) -> None:
    """Persist the panel's public base URL, or clear the override when ``url`` is empty.

    Raises ``ValueError`` for a malformed value so the route can show the reason inline — this is
    the fail-loud boundary that balances the fail-soft config validator.

    Clearing **deletes** the row rather than storing ``""``. That is the opposite of the SMTP
    fields, where an empty string is an intentional authoritative override; here an empty stored
    value would permanently shadow ``CAIRN_PUBLIC_URL`` with no way back to it from the panel.
    """
    normalized = normalize_public_url(url)  # ValueError propagates to the caller
    obj = await session.get(AppSetting, "public_url")
    if normalized is None:
        if obj is not None:
            await session.delete(obj)
    elif obj is None:
        session.add(AppSetting(key="public_url", value=normalized))
    else:
        obj.value = normalized
    await session.commit()
