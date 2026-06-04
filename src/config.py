"""Cairn configuration.

All settings come from the environment (prefix ``CAIRN_``) with an optional YAML overlay
(``CAIRN_CONFIG_FILE``). Environment values take precedence over the YAML overlay. Secrets and
host-specific paths are never hardcoded — they arrive via env or a referenced file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

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
    )

    # --- Auth / mode ---
    auth_mode: Literal["single", "multi"] = "single"
    single_user: str = "local"

    # --- Datastore & proof store ---
    database_url: str = "sqlite+aiosqlite:///./data/cairn.db"
    proof_store_path: Path = Path("./proofs")

    # --- Session / security ---
    secret_key: str | None = None
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

    # --- Optional YAML overlay path (read in _YamlConfigSource) ---
    config_file: Path | None = None

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.auth_mode == "multi" and not self.secret_key:
            raise ValueError(
                "CAIRN_AUTH_MODE=multi requires CAIRN_SECRET_KEY to be set "
                "(generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\")"
            )
        return self

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
