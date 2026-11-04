"""`oidc` configuration: fail-loud validation and secret-safe errors (oidc-sso-login B.1/B.2).

Covers the configuration delta: the mode selector gains `oidc`; each invalid static `oidc`
configuration refuses to load, naming the setting and never a value; `http` public URLs stay valid
outside `oidc`; `CAIRN_OIDC_*` is ignored outside `oidc` (even malformed); and `cairn serve`, run as
a real subprocess once per invalid configuration with recognisable secrets, names the setting and
prints neither secret.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.oidc_fake import CLIENT_SECRET, SECRET_KEY, oidc_env

REPO = Path(__file__).resolve().parent.parent
SHORT_KEY = "zz-short-key-RECOGNISABLE-31chr"  # exactly 31 characters
assert len(SHORT_KEY) == 31

# (name, env overrides, the setting the error must name). Every entry in the D9 list.
INVALID = [
    ("issuer-missing", {"CAIRN_OIDC_ISSUER": None}, "CAIRN_OIDC_ISSUER"),
    ("issuer-http", {"CAIRN_OIDC_ISSUER": "http://idp.example.com/realms/example"},
     "CAIRN_OIDC_ISSUER"),
    ("issuer-userinfo", {"CAIRN_OIDC_ISSUER": "https://u:p@idp.example.com/realms/example"},
     "CAIRN_OIDC_ISSUER"),
    ("issuer-query", {"CAIRN_OIDC_ISSUER": "https://idp.example.com/realms/example?x=1"},
     "CAIRN_OIDC_ISSUER"),
    ("issuer-fragment", {"CAIRN_OIDC_ISSUER": "https://idp.example.com/realms/example#f"},
     "CAIRN_OIDC_ISSUER"),
    ("client-id-missing", {"CAIRN_OIDC_CLIENT_ID": None}, "CAIRN_OIDC_CLIENT_ID"),
    ("client-secret-missing", {"CAIRN_OIDC_CLIENT_SECRET": None}, "CAIRN_OIDC_CLIENT_SECRET"),
    ("public-url-missing", {"CAIRN_PUBLIC_URL": None}, "CAIRN_PUBLIC_URL"),
    ("public-url-invalid", {"CAIRN_PUBLIC_URL": "ftp://cairn.example.com"}, "CAIRN_PUBLIC_URL"),
    ("public-url-http", {"CAIRN_PUBLIC_URL": "http://cairn.example.com"}, "CAIRN_PUBLIC_URL"),
    ("secret-key-missing", {"CAIRN_SECRET_KEY": None}, "CAIRN_SECRET_KEY"),
    ("secret-key-short", {"CAIRN_SECRET_KEY": SHORT_KEY}, "CAIRN_SECRET_KEY"),
    ("offline-access", {"CAIRN_OIDC_EXTRA_SCOPES": "groups offline_access"},
     "CAIRN_OIDC_EXTRA_SCOPES"),
    ("auth-method", {"CAIRN_OIDC_TOKEN_AUTH_METHOD": "private_key_jwt"},
     "CAIRN_OIDC_TOKEN_AUTH_METHOD"),
    ("lifetime-zero", {"CAIRN_OIDC_SESSION_MAX_AGE": "0"}, "CAIRN_OIDC_SESSION_MAX_AGE"),
    ("lifetime-garbage", {"CAIRN_OIDC_SESSION_MAX_AGE": "12h"}, "CAIRN_OIDC_SESSION_MAX_AGE"),
]


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("CAIRN_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)  # no stray .env
    from src.config import get_settings

    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _set(monkeypatch, env: dict) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _settings():
    from src.config import Settings

    return Settings()


def test_complete_oidc_config_loads(clean_env):
    _set(clean_env, oidc_env(CAIRN_OIDC_EXTRA_SCOPES="groups, email"))
    s = _settings()
    assert s.auth_mode == "oidc"
    assert s.oidc_scopes() == ["openid", "groups", "email"]
    assert s.oidc_session_lifetime == 43200
    assert s.oidc_token_auth_method == "client_secret_basic"
    assert s.oidc_groups_claim == "groups"
    # Secrets are SecretStr: their repr/str never show the value.
    assert CLIENT_SECRET not in repr(s) and SECRET_KEY not in repr(s)
    assert SECRET_KEY not in str(s.secret_key)


def test_invalid_mode_names_allowed_values(clean_env):
    clean_env.setenv("CAIRN_AUTH_MODE", "sso")
    with pytest.raises(ValidationError) as exc:
        _settings()
    text = str(exc.value)
    assert "single" in text and "multi" in text and "oidc" in text
    assert "input_value" not in text  # hide_input_in_errors


@pytest.mark.parametrize("name,overrides,setting", INVALID, ids=[i[0] for i in INVALID])
def test_invalid_oidc_config_names_the_setting_and_no_secret(clean_env, name, overrides, setting):
    _set(clean_env, oidc_env(**overrides))
    with pytest.raises(ValidationError) as exc:
        _settings()
    text = str(exc.value)
    assert setting in text
    for secret in (CLIENT_SECRET, SECRET_KEY, SHORT_KEY):
        assert secret not in text


def test_public_url_only_in_panel_override_refused(clean_env):
    """The panel override never satisfies oidc mode: only env/YAML does (it is not even read)."""
    _set(clean_env, oidc_env(CAIRN_PUBLIC_URL=None))
    with pytest.raises(ValidationError, match="CAIRN_PUBLIC_URL"):
        _settings()


def test_public_url_from_yaml_is_accepted(clean_env, tmp_path):
    cfg = tmp_path / "cairn.yaml"
    cfg.write_text("public_url: https://cairn.example.com\n")
    _set(clean_env, oidc_env(CAIRN_PUBLIC_URL=None, CAIRN_CONFIG_FILE=str(cfg)))
    assert _settings().public_url == "https://cairn.example.com"


def test_http_public_url_still_valid_in_single_mode(clean_env):
    clean_env.setenv("CAIRN_PUBLIC_URL", "http://cairn.example.com")
    assert _settings().public_url == "http://cairn.example.com"


def test_oidc_settings_ignored_outside_oidc_even_when_malformed(clean_env):
    _set(clean_env, {
        "CAIRN_AUTH_MODE": "single",
        "CAIRN_OIDC_ISSUER": "not a url",
        "CAIRN_OIDC_SESSION_MAX_AGE": "garbage",
        "CAIRN_OIDC_TOKEN_AUTH_METHOD": "nonsense",
        "CAIRN_OIDC_EXTRA_SCOPES": "offline_access",
    })
    s = _settings()
    assert s.auth_mode == "single"


def test_multi_secret_message_unchanged(clean_env):
    clean_env.setenv("CAIRN_AUTH_MODE", "multi")
    with pytest.raises(ValidationError, match="CAIRN_AUTH_MODE=multi requires CAIRN_SECRET_KEY"):
        _settings()


def test_oidc_session_middleware_and_csrf_use_the_real_key(clean_env, tmp_path):
    _set(clean_env, oidc_env(
        CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'db' / 'cairn.db'}",
        CAIRN_PROOF_STORE_PATH=str(tmp_path / "proofs"),
        CAIRN_OIDC_SESSION_MAX_AGE="3600",
    ))
    from starlette.middleware.sessions import SessionMiddleware

    from src import csrf
    from src.main import create_app

    app = create_app()
    mws = [m for m in app.user_middleware if m.cls is SessionMiddleware]
    assert len(mws) == 1
    kw = mws[0].kwargs
    assert kw == {"secret_key": SECRET_KEY, "same_site": "lax", "https_only": True,
                  "max_age": 3600}
    assert csrf._secret_key() == SECRET_KEY


# --- cairn serve, one real subprocess per invalid configuration -------------------------------


@pytest.mark.parametrize("name,overrides,setting", INVALID, ids=[i[0] for i in INVALID])
def test_cairn_serve_refuses_and_prints_no_secret(tmp_path, name, overrides, setting):
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAIRN_")}
    env.update(oidc_env(**overrides))
    env.update({
        "PYTHONPATH": str(REPO),
        "CAIRN_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'db' / 'cairn.db'}",
        "CAIRN_PROOF_STORE_PATH": str(tmp_path / "proofs"),
        "CAIRN_SCHEDULER_ENABLED": "0",
        "CAIRN_PORT": "1",  # never reached: startup must refuse before binding
    })
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from src.cli import main; sys.exit(main(['serve']))"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert setting in out, out
    for secret in (CLIENT_SECRET, SECRET_KEY, SHORT_KEY):
        assert secret not in out
