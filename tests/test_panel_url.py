"""Public panel URL: the grammar, the link builder, the fail-soft config validator, and the
DB overlay (validated on read).

These cover the safety properties the alert deep-link feature rests on: the normalized URL is pure
ASCII (it goes verbatim into an HTTP header), a malformed value never stops the application, and an
unvalidated stored value never becomes an effective setting.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_panel_url.py``
"""

from __future__ import annotations

import logging

import pytest


# --- temp-DB fixture (mirrors tests/test_notify.py) -----------------------------------------


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")
    monkeypatch.delenv("CAIRN_PUBLIC_URL", raising=False)

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    return tmp_path


# --- normalize_public_url: accepted forms ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://cairn.example.com", "https://cairn.example.com"),
        ("http://localhost:8000", "http://localhost:8000"),  # a LAN address is the operator's call
        ("http://192.168.1.10:8000", "http://192.168.1.10:8000"),
        ("https://example.com/cairn", "https://example.com/cairn"),  # sub-path reverse proxy
        ("https://cairn.example.com/", "https://cairn.example.com"),  # trailing slash stripped
        ("https://cairn.example.com///", "https://cairn.example.com"),
        ("https://example.com/cairn/", "https://example.com/cairn"),
        ("HTTPS://Cairn.Example.COM", "https://cairn.example.com"),  # scheme+host lowercased
        ("  https://cairn.example.com  ", "https://cairn.example.com"),  # outer padding trimmed
    ],
)
def test_normalize_accepts_and_canonicalizes(raw, expected):
    from src.services.panel_url import normalize_public_url

    assert normalize_public_url(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t"])
def test_normalize_treats_blank_as_unset(raw):
    """Unset is a legitimate state, not an error — no link gets built, nothing raises."""
    from src.services.panel_url import normalize_public_url

    assert normalize_public_url(raw) is None


# --- normalize_public_url: rejected forms ---------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "cairn.example.com",            # bare host — not an absolute URL
        "/cairn",                       # path-only
        "javascript:alert(1)",          # a non-http scheme in a mailed href is an attack surface
        "ftp://cairn.example.com",
        "https://",                     # empty host
        "https://u:p@cairn.example.com",  # userinfo leaks credentials into every alert
        "https://cairn.example.com/?next=evil",  # query
        "https://cairn.example.com/#overview",   # fragment
        'https://cairn.example.com/a"b',         # breaks out of an HTML href attribute
        "https://cairn.example.com/a<b",
        "https://cairn.example.com/a\nb",        # header injection
        "https://cairn.example.com/a\rb",
        "https://cairn.example.com/a\x00b",
        "https://cairn.example.com/a\x07b",
        "https://cairn.example.com/a\x7fb",
        "https://cairn.example.com/a b",
        "https://cairn.example.com:not-a-port",
    ],
)
def test_normalize_rejects(raw):
    from src.services.panel_url import normalize_public_url

    with pytest.raises(ValueError):
        normalize_public_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "https://exa%mple.com",     # a percent-escape is not legal in a host
        "https://%0d%0a.example",   # percent-encoded CR/LF smuggled past the literal-control check
        "https://-",                # a label may not start or end with a hyphen
        "https://.",                # empty labels
        "https://.example.com",     # leading empty label
        "https://exam ple.com",     # (whitespace — caught earlier, asserted here for completeness)
        "https://a..b",
        "https://" + "a" * 64 + ".com",  # label longer than 63 characters
        "https://[::zz]:8000",      # not a valid IPv6 literal
    ],
)
def test_normalize_rejects_malformed_hosts(raw):
    """A malformed host is a dead link, and a dead link is a silently useless alert.

    Nothing dereferences this URL, so these are not security holes — they are typos that used to
    sail through the panel-save boundary whose entire job is to catch a typo while a human is
    watching, then surface months later as a "review your missing files" link that goes nowhere.
    """
    from src.services.panel_url import normalize_public_url

    with pytest.raises(ValueError):
        normalize_public_url(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000"),  # single-label: the spec requires it
        ("http://cairn", "http://cairn"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("http://[2001:db8::1]", "http://[2001:db8::1]"),
        ("https://cairn.example.com", "https://cairn.example.com"),
        ("https://my-panel.example.co.uk/cairn", "https://my-panel.example.co.uk/cairn"),
    ],
)
def test_normalize_still_accepts_every_legitimate_host_shape(raw, expected):
    """Host validation must not narrow what the spec says is valid — LAN addresses included."""
    from src.services.panel_url import normalize_public_url

    assert normalize_public_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["https://cäirn.example.com", "https://cairn.example.com/é", "https://例え.テスト/x"],
)
def test_normalize_result_is_pure_ascii(raw):
    """Non-ASCII is IDNA/percent-encoded or rejected — never emitted raw.

    The URL is written verbatim into ntfy's ``Click`` HTTP header; a non-ASCII header value raises
    at encode time, which would silently cost an ntfy-only operator every alert.
    """
    from src.services.panel_url import normalize_public_url

    try:
        result = normalize_public_url(raw)
    except ValueError:
        return  # rejecting is an accepted outcome for this scenario
    assert result is not None
    assert result.isascii()
    result.encode("ascii")  # the property the ntfy Click header actually needs


# --- panel_link -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        ("https://cairn.example.com", "/collection/1/review", "https://cairn.example.com/collection/1/review"),
        ("https://cairn.example.com/", "/collection/1/review", "https://cairn.example.com/collection/1/review"),
        ("https://cairn.example.com", "collection/1/review", "https://cairn.example.com/collection/1/review"),
        ("https://example.com/cairn", "/collection/1/review", "https://example.com/cairn/collection/1/review"),
        ("https://example.com/cairn/", "collections", "https://example.com/cairn/collections"),
    ],
)
def test_panel_link_joins_with_exactly_one_slash(base, path, expected):
    from src.services.panel_url import panel_link

    link = panel_link(base, path)
    assert link == expected
    assert "//" not in link.split("://", 1)[1]


@pytest.mark.parametrize("base", [None, ""])
def test_panel_link_none_when_unset(base):
    """No base means no link — Cairn never emits a relative or inferred URL."""
    from src.services.panel_url import panel_link

    assert panel_link(base, "/collection/1/review") is None


# --- config: fail-soft at load --------------------------------------------------------------


def test_valid_env_public_url_is_normalized(monkeypatch):
    from src.config import Settings

    monkeypatch.setenv("CAIRN_PUBLIC_URL", "https://cairn.example.com/")
    assert Settings().public_url == "https://cairn.example.com"


def test_malformed_env_public_url_does_not_raise(monkeypatch, caplog):
    """Startup safety: a typo in CAIRN_PUBLIC_URL must never stop the application.

    ``get_settings()`` is ``@lru_cache``d and builds the whole model, so a raising validator here
    would take out startup, scanning, the scheduler and every alert — trading a missing hyperlink
    for the exact failure Cairn exists to prevent. Invalid ⇒ unset + one warning.
    """
    from src.config import Settings

    monkeypatch.setenv("CAIRN_PUBLIC_URL", "cairn.example.com")
    # The migration fixture elsewhere in the suite runs alembic's fileConfig, which disables
    # non-configured loggers; re-enable cairn.config so its warning is capturable here.
    config_log = logging.getLogger("cairn.config")
    config_log.disabled = False
    config_log.propagate = True
    with caplog.at_level(logging.WARNING, logger="cairn.config"):
        settings = Settings()  # must not raise

    assert settings.public_url is None
    assert any("CAIRN_PUBLIC_URL" in rec.getMessage() for rec in caplog.records)


@pytest.mark.parametrize("raw", ["javascript:alert(1)", "https://", "https://a\nb.com", "/cairn"])
def test_every_malformed_env_value_is_coerced_to_none(monkeypatch, raw):
    from src.config import Settings

    monkeypatch.setenv("CAIRN_PUBLIC_URL", raw)
    assert Settings().public_url is None


def test_unset_env_leaves_public_url_none(monkeypatch):
    from src.config import Settings

    monkeypatch.delenv("CAIRN_PUBLIC_URL", raising=False)
    assert Settings().public_url is None


# --- app_settings overlay -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlay_db_wins_falls_back_and_ignores_invalid(cairn_env, monkeypatch):
    """DB wins over env; an absent row falls back; an invalid stored row is ignored, not emitted."""
    from src.config import Settings
    from src.database import get_sessionmaker
    from src.models.db import AppSetting
    from src.services import app_settings

    monkeypatch.setenv("CAIRN_PUBLIC_URL", "https://env.example.com")
    base = Settings()
    assert base.public_url == "https://env.example.com"

    sm = get_sessionmaker()

    # 1. No row → env applies.
    async with sm() as s:
        eff = await app_settings.effective_settings(s, base)
    assert eff.public_url == "https://env.example.com"

    # 2. A stored value wins over env, and is normalized on the way in.
    async with sm() as s:
        await app_settings.save_public_url(s, "https://db.example.com/cairn/")
    async with sm() as s:
        eff = await app_settings.effective_settings(s, base)
    assert eff.public_url == "https://db.example.com/cairn"

    # 3. A hand-edited / corrupt row is dropped on read: model_copy(update=...) skips validators,
    #    so this is the only thing standing between a bad stored value and an outbound alert.
    async with sm() as s:
        row = await s.get(AppSetting, "public_url")
        row.value = "javascript:alert(1)"
        await s.commit()
    async with sm() as s:
        overrides = await app_settings.get_overrides(s)
        eff = await app_settings.effective_settings(s, base)
    assert "public_url" not in overrides
    assert eff.public_url == "https://env.example.com"

    # 4. Clearing deletes the row (never stores "") so the env value becomes visible again.
    async with sm() as s:
        await app_settings.save_public_url(s, "  ")
    async with sm() as s:
        assert await s.get(AppSetting, "public_url") is None
        eff = await app_settings.effective_settings(s, base)
    assert eff.public_url == "https://env.example.com"


@pytest.mark.asyncio
async def test_save_public_url_raises_on_invalid_and_leaves_store_untouched(cairn_env):
    """Fail-loud at the save boundary — a human is present to read the reason."""
    from src.database import get_sessionmaker
    from src.models.db import AppSetting
    from src.services import app_settings

    sm = get_sessionmaker()
    async with sm() as s:
        await app_settings.save_public_url(s, "https://good.example.com")
    async with sm() as s:
        with pytest.raises(ValueError):
            await app_settings.save_public_url(s, "not-a-url")
    async with sm() as s:
        row = await s.get(AppSetting, "public_url")
    assert row is not None and row.value == "https://good.example.com"


@pytest.mark.asyncio
async def test_overlay_leaves_smtp_behaviour_alone(cairn_env, monkeypatch):
    """OVERLAY_FIELDS widening must not change what the SMTP form round-trips."""
    from src.config import Settings
    from src.database import get_sessionmaker
    from src.services import app_settings

    assert app_settings.OVERLAY_FIELDS == app_settings.SMTP_FIELDS + ("public_url",)

    monkeypatch.setenv("CAIRN_SMTP_HOST", "env-host.example.com")
    base = Settings()
    sm = get_sessionmaker()
    async with sm() as s:
        await app_settings.save_smtp(
            s, host="db-host.example.com", port=2525, starttls=False, user="u@example.com",
            from_="cairn@example.com", provider="local", password="topsecret",
        )
    async with sm() as s:
        smtp_only = await app_settings.get_smtp_overrides(s)
        eff = await app_settings.effective_settings(s, base)

    assert "public_url" not in smtp_only  # get_smtp_overrides still means *SMTP* fields
    assert eff.smtp_host == "db-host.example.com"


def test_normalize_accepts_and_canonicalizes_a_trailing_dot_fqdn():
    """``example.com.`` is a legitimate fully-qualified name (the explicit DNS root).

    Rejecting it would silently strip a working review link. It is canonicalized to the dotless
    form so the same panel cannot yield two different-looking links.
    """
    from src.services.panel_url import normalize_public_url

    assert normalize_public_url("https://example.com.") == "https://example.com"
    assert normalize_public_url("http://localhost.:8000") == "http://localhost:8000"
    # A leading dot, a bare dot, and interior empty labels stay rejected.
    for bad in ("https://.example.com", "https://.", "https://a..b"):
        with pytest.raises(ValueError):
            normalize_public_url(bad)
