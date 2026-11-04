"""Cairn configuration.

All settings come from the environment (prefix ``CAIRN_``) with an optional YAML overlay
(``CAIRN_CONFIG_FILE``). Environment values take precedence over the YAML overlay. Secrets and
host-specific paths are never hardcoded — they arrive via env or a referenced file.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from urllib.parse import urlsplit

import yaml
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .services.panel_url import normalize_public_url

_log = logging.getLogger("cairn.config")

# Default public OpenTimestamps calendars (the same set the ``ots`` CLI submits to).
DEFAULT_OTS_CALENDARS = [
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
]


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Settings source that reads an optional YAML overlay named by ``CAIRN_CONFIG_FILE``.

    Placed below the env/dotenv sources so the environment always wins.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._data: dict[str, Any] = {}
        path = os.environ.get("CAIRN_CONFIG_FILE")
        if path and Path(path).is_file():
            loaded = yaml.safe_load(Path(path).read_text()) or {}
            if isinstance(loaded, dict):
                self._data = {str(k).lower(): v for k, v in loaded.items()}

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:  # noqa: D102
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            value, key, _ = self.get_field_value(None, field_name)
            if value is not None:
                out[key] = value
        return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAIRN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Never echo an input value in a validation error: a too-short CAIRN_SECRET_KEY would
        # otherwise be printed by the very check that rejects it (oidc-sso-login, design D9).
        hide_input_in_errors=True,
    )

    # --- Auth / mode ---
    # `oidc` = one implicit owner behind the OIDC login wall (src/auth/). `multi` is a placeholder.
    auth_mode: Literal["single", "multi", "oidc"] = "single"
    single_user: str = "local"

    # --- Datastore & proof store ---
    database_url: str = "sqlite+aiosqlite:///./data/cairn.db"
    proof_store_path: Path = Path("./proofs")

    # --- Session / security ---
    # SecretStr: its repr/str redact the value. Read with `.get_secret_value()` (session
    # middleware + CSRF signer only).
    secret_key: SecretStr | None = None
    session_cookie_name: str = "cairn_session"
    session_max_age: int = 60 * 60 * 24 * 14  # 14 days

    # --- OpenTimestamps ---
    ots_calendars: list[str] = Field(default_factory=lambda: list(DEFAULT_OTS_CALENDARS))
    verify_backend: Literal["explorer", "node"] = "explorer"
    explorer_url: str = "https://blockstream.info"
    node_rpc_url: str | None = None
    incomplete_proof_alarm_days: int = 7
    # Max files per `ots stamp` invocation. One calendar round-trip yields N independent per-file
    # proofs, so batching cuts ~N× the calendar requests; bounds argv length + the Merkle build.
    ots_stamp_batch_size: int = 256

    # --- Runtime ---
    # Externally-reachable base URL of the panel (e.g. https://cairn.example.com), used to build
    # the deep links in alerts. Optional, not a secret, never inferred — see services/panel_url.py.
    public_url: str | None = None
    auto_migrate: bool = False
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Scheduler ---
    scheduler_enabled: bool = True
    scan_interval_seconds: int = 30  # scheduler tick / due-collection poll
    upgrade_interval_seconds: int = 86400  # daily OTS upgrade pass
    health_freshness_floor_seconds: int = 900  # floor on the per-collection freshness window

    # --- Notifications (channel credentials; routing lives per-collection in alert_json) ---
    # Email transport. SMTP is the implemented transport; resend/ses are recognized but stubbed.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password: str | None = None  # secret — env only
    smtp_from: str | None = None
    email_provider: Literal["local", "resend", "ses"] = "local"

    # --- OIDC (only read in `oidc` mode; ignored entirely otherwise, by server and CLI) ---
    # Loosely typed on purpose: outside `oidc` mode a malformed value must not stop startup, so
    # these are parsed and checked by `_validate_oidc` only when the mode is `oidc`.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None  # secret — env (or a referenced file) only
    oidc_token_auth_method: str = "client_secret_basic"
    oidc_extra_scopes: str | list[str] | None = None
    oidc_required_group: str | None = None
    oidc_groups_claim: str = "groups"
    oidc_session_max_age: int | str = 43200  # 12 h absolute session lifetime

    # --- Optional YAML overlay path (read in _YamlConfigSource) ---
    config_file: Path | None = None

    @field_validator("public_url", mode="before")
    @classmethod
    def _coerce_public_url(cls, value: Any) -> str | None:
        """Normalize ``public_url``, or fall back to unset — **never raise**.

        ``get_settings()`` is ``@lru_cache``d and builds the whole model, so raising here would
        take down startup, scanning, the scheduler and every alert over a cosmetic link setting.
        For a tool whose job is noticing that files changed, that trades a missing hyperlink for
        the exact failure it exists to prevent. Fail-soft at load; the panel-save boundary, where a
        human is present to read the error, is where an invalid value is refused loudly.
        """
        if value is None:
            return None
        try:
            return normalize_public_url(str(value))
        except ValueError as exc:
            _log.warning("CAIRN_PUBLIC_URL ignored (%s): %r", exc, value)
            return None

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.auth_mode == "multi" and not self.secret_key:
            raise ValueError(
                "CAIRN_AUTH_MODE=multi requires CAIRN_SECRET_KEY to be set "
                "(generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\")"
            )
        if self.auth_mode == "oidc":
            self._validate_oidc()
        return self

    def _validate_oidc(self) -> None:
        """Fail loud on incomplete static `oidc` configuration (design D9).

        Every message is `<SETTING>: <reason>` and never interpolates a value. The provider is not
        contacted: an IdP outage must never stop scanning.
        """
        problems: list[str] = []
        if not self.oidc_issuer:
            problems.append("CAIRN_OIDC_ISSUER: required in oidc mode")
        else:
            reason = issuer_problem(self.oidc_issuer)
            if reason:
                problems.append(f"CAIRN_OIDC_ISSUER: {reason}")
        if not self.oidc_client_id:
            problems.append("CAIRN_OIDC_CLIENT_ID: required in oidc mode")
        if self.oidc_client_secret is None or not self.oidc_client_secret.get_secret_value():
            problems.append("CAIRN_OIDC_CLIENT_SECRET: required in oidc mode")
        # The redirect URI is built from the env/YAML value only (never the panel override, never
        # request headers). `public_url` fails soft to None when invalid, so None = unset/invalid.
        if not self.public_url:
            problems.append(
                "CAIRN_PUBLIC_URL: required in oidc mode (unset or invalid in env/YAML); "
                "it must be an https URL"
            )
        elif urlsplit(self.public_url).scheme.lower() != "https":
            problems.append("CAIRN_PUBLIC_URL: must be an https URL in oidc mode")
        key = self.secret_key.get_secret_value() if self.secret_key is not None else ""
        if not key:
            problems.append("CAIRN_SECRET_KEY: required in oidc mode")
        elif len(key) < 32:
            problems.append("CAIRN_SECRET_KEY: must be at least 32 characters in oidc mode")
        if "offline_access" in self.oidc_scopes():
            problems.append("CAIRN_OIDC_EXTRA_SCOPES: offline_access is not allowed")
        if self.oidc_token_auth_method not in ("client_secret_basic", "client_secret_post"):
            problems.append(
                "CAIRN_OIDC_TOKEN_AUTH_METHOD: must be client_secret_basic or client_secret_post"
            )
        lifetime = _positive_int(self.oidc_session_max_age)
        if lifetime is None:
            problems.append("CAIRN_OIDC_SESSION_MAX_AGE: must be a positive integer")
        else:
            self.oidc_session_max_age = lifetime
        if not self.oidc_groups_claim:
            problems.append("CAIRN_OIDC_GROUPS_CLAIM: must not be empty")
        if problems:
            raise ValueError("; ".join(problems))

    def oidc_scopes(self) -> list[str]:
        """`openid` plus the configured extra scopes (space- or comma-separated, or a YAML list)."""
        raw = self.oidc_extra_scopes
        if raw is None:
            extra: list[str] = []
        elif isinstance(raw, str):
            extra = raw.replace(",", " ").split()
        else:
            extra = [str(x).strip() for x in raw if str(x).strip()]
        scopes = ["openid"]
        for scope in extra:
            if scope not in scopes:
                scopes.append(scope)
        return scopes

    @property
    def oidc_session_lifetime(self) -> int:
        """The absolute OIDC session lifetime in seconds (validated in `oidc` mode)."""
        return _positive_int(self.oidc_session_max_age) or 43200

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = priority (first wins). YAML overlay sits below env/dotenv.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls),
            file_secret_settings,
        )


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def issuer_problem(issuer: str) -> str | None:
    """Why ``issuer`` is not an acceptable OIDC issuer, or ``None`` (no value in the reason)."""
    if len(issuer) > 512:
        return "must be at most 512 characters"
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in issuer):
        return "must not contain whitespace or control characters"
    if "?" in issuer or "#" in issuer:
        return "must not carry a query or fragment"
    try:
        parts = urlsplit(issuer)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        return "is not a valid URL"
    if parts.scheme.lower() != "https":
        return "must be an https URL"
    if "@" in parts.netloc:
        return "must not carry userinfo"
    if not host:
        return "must name a host"
    return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
