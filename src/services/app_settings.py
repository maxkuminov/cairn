"""Global, UI-editable app settings backed by the ``app_settings`` key-value table.

Today this stores the SMTP server config (host/port/TLS/user/password/from/provider) so the email
transport can be configured from the panel instead of env-only. Values are persisted as TEXT and
coerced back to the typed :class:`~src.config.Settings` fields on read.

Precedence: **DB overrides env.** :func:`effective_settings` overlays any stored values onto the
env-derived :class:`Settings`, so an empty table falls back to ``CAIRN_SMTP_*`` (existing deploys
keep working) and a value set in the UI takes effect with no restart (no ``get_settings()`` cache to
bust — the overlay happens at read time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.db import AppSetting

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

_TRUE = {"1", "true", "yes", "on"}


def _coerce(field: str, raw: str) -> Any:
    """Coerce a stored TEXT value back to the type ``Settings`` expects for ``field``."""
    if field == "smtp_port":
        return int(raw)
    if field == "smtp_starttls":
        return raw.strip().lower() in _TRUE
    return raw


async def get_smtp_overrides(session: AsyncSession) -> dict[str, Any]:
    """Return the SMTP fields set in the DB, typed and keyed by ``Settings`` field name.

    Keys with no stored row (or a NULL/uncoercible value) are omitted so the env default wins.
    """
    rows = await session.execute(
        select(AppSetting).where(AppSetting.key.in_(SMTP_FIELDS))
    )
    out: dict[str, Any] = {}
    for row in rows.scalars():
        if row.value is None:
            continue
        try:
            out[row.key] = _coerce(row.key, row.value)
        except (ValueError, TypeError):
            continue
    return out


async def effective_settings(session: AsyncSession, base: "Settings") -> "Settings":
    """``base`` (env/dotenv) with DB-stored SMTP overrides applied on top (DB wins)."""
    overrides = await get_smtp_overrides(session)
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
