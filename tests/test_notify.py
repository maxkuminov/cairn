"""Notifier framework: SMTP composition, channel routing, best-effort dispatch, scanner trigger,
and collection-form alert round-trip. No real network — smtplib and httpx are monkeypatched.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_notify.py``
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select


# --- temp-DB fixture (mirrors tests/test_scanner.py) ----------------------------------------


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    return tmp_path


async def _make_collection(root: Path, *, mode: str = "worm", alert: dict | None = None) -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s, user_id=uid, name=root.name, root=str(root), mode=mode, alert=alert
        )
        return collection.id


# --- SmtpNotifier composition ---------------------------------------------------------------


class _FakeSMTP:
    """Records the last sent message + recipients; captured by monkeypatching smtplib.SMTP."""

    instances: list[_FakeSMTP] = []

    def __init__(self, host=None, port=None, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent = msg


def test_smtp_composes_subject_body_recipients(cairn_env, monkeypatch):
    import smtplib

    from src.config import Settings
    from src.notify.base import Alert
    from src.notify.smtp import SmtpNotifier

    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

    settings = Settings(
        smtp_host="mail.example.com",
        smtp_port=587,
        smtp_starttls=True,
        smtp_user="cairn@example.com",
        smtp_password="secret",
        smtp_from="cairn@example.com",
        email_provider="local",
    )
    notifier = SmtpNotifier(
        recipients=["alice@example.com", "bob@example.com"], settings=settings
    )
    alert = Alert(
        collection_name="Photos",
        summary="2 missing, 1 modified",
        paths=["a/lost.jpg", "b/edited.raw"],
        detected_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
    )

    asyncio.run(notifier.send(alert))

    assert len(_FakeSMTP.instances) == 1
    smtp = _FakeSMTP.instances[0]
    assert smtp.host == "mail.example.com" and smtp.port == 587
    assert smtp.started_tls is True
    assert smtp.logged_in == ("cairn@example.com", "secret")
    msg = smtp.sent
    assert msg is not None
    assert msg["Subject"] == "Cairn: 2 missing, 1 modified in Photos"
    assert msg["From"] == "cairn@example.com"
    assert "alice@example.com" in msg["To"] and "bob@example.com" in msg["To"]
    body = msg.get_content()
    assert "a/lost.jpg" in body and "b/edited.raw" in body
    assert "Photos" in body


def test_smtp_resend_provider_raises(cairn_env):
    from src.config import Settings
    from src.notify.base import Alert, NotifierError
    from src.notify.smtp import SmtpNotifier

    settings = Settings(smtp_host="mail.example.com", email_provider="resend")
    notifier = SmtpNotifier(recipients=["x@example.com"], settings=settings)
    with pytest.raises(NotifierError, match="resend transport not yet wired"):
        asyncio.run(notifier.send(Alert(collection_name="c", summary="1 missing", paths=["x"])))


# --- build_channels routing -----------------------------------------------------------------


def test_build_channels_only_enabled(cairn_env):
    from src.config import Settings
    from src.models.db import Collection
    from src.notify.base import build_channels

    settings = Settings(smtp_host="mail.example.com", smtp_from="cairn@example.com")
    alert_json = json.dumps(
        {
            "email": {"enabled": True, "to": ["alerts@example.com"]},
            "webhook": {"enabled": True, "url": "https://hooks.example.com/x"},
            "ntfy": {"enabled": False, "topic": "cairn", "server": "https://ntfy.sh"},
            "signal": {"enabled": False, "phone": "+10", "apikey": "k"},
            "kuma": {"enabled": False, "push_url": "https://kuma/x"},
        }
    )
    collection = Collection(name="c", root="/tmp", alert_json=alert_json)
    channels = build_channels(collection, settings)
    names = sorted(c.name for c in channels)
    assert names == ["email", "webhook"]


def test_build_channels_empty_yields_none(cairn_env):
    from src.config import Settings
    from src.models.db import Collection
    from src.notify.base import build_channels

    settings = Settings()
    # The scanner/ots/scheduler default — MUST produce no channels (no network).
    assert build_channels(Collection(name="c", root="/tmp", alert_json="{}"), settings) == []
    assert build_channels(Collection(name="c", root="/tmp", alert_json=""), settings) == []


# --- dispatch best-effort isolation ----------------------------------------------------------


def test_dispatch_isolates_channel_failure(cairn_env, monkeypatch):
    from src.config import Settings
    from src.models.db import Collection
    from src.notify import dispatch as dispatch_mod
    from src.notify.base import Alert, NotifierError

    class _Good:
        name = "good"

        def __init__(self):
            self.calls = 0

        async def send(self, alert):
            self.calls += 1

    class _Bad:
        name = "bad"

        async def send(self, alert):
            raise NotifierError("boom")

    good = _Good()
    bad = _Bad()
    # build_channels returns one failing + one succeeding channel regardless of config.
    monkeypatch.setattr(dispatch_mod, "build_channels", lambda collection, settings: [bad, good])

    collection = Collection(name="c", root="/tmp", alert_json="{}")
    result = asyncio.run(
        dispatch_mod.dispatch(Alert(collection_name="c", summary="1 missing"), collection, Settings())
    )
    assert result == {"bad": False, "good": True}
    assert good.calls == 1  # the good channel still ran despite the bad one raising first


# --- scanner trigger -------------------------------------------------------------------------


def _record_dispatch(monkeypatch):
    """Patch the scanner's dispatch hook to record calls instead of sending."""
    from src.notify import dispatch as dispatch_mod

    calls: list = []

    async def fake_dispatch(alert, collection, settings):
        calls.append((alert, collection.name))
        return {}

    monkeypatch.setattr(dispatch_mod, "dispatch", fake_dispatch)
    return calls


@pytest.mark.asyncio
async def test_scan_missing_and_worm_modified_dispatches(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    calls = _record_dispatch(monkeypatch)
    alert = {"email": {"enabled": True, "to": ["a@example.com"]}}

    root = cairn_env / "worm"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    (root / "edit.txt").write_text("v1")
    (root / "drop.txt").write_text("bye")
    cid = await _make_collection(root, mode="worm", alert=alert)
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))  # baseline: 3 added → no alert
    assert calls == []  # added-only does NOT dispatch

    # Modify one (WORM modified → alarm) and delete one (missing → alarm).
    (root / "edit.txt").write_text("v2 is longer and different")
    (root / "drop.txt").unlink()
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 1 and summ.missing == 1

    assert len(calls) == 1
    fired_alert, collection_name = calls[0]
    assert collection_name == "worm"
    assert "1 missing" in fired_alert.summary and "1 modified" in fired_alert.summary
    assert set(fired_alert.paths) == {"edit.txt", "drop.txt"}


@pytest.mark.asyncio
async def test_scan_churn_modify_does_not_dispatch(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    calls = _record_dispatch(monkeypatch)
    alert = {"email": {"enabled": True, "to": ["a@example.com"]}}

    root = cairn_env / "churn"
    root.mkdir()
    (root / "x.txt").write_text("one")
    cid = await _make_collection(root, mode="churn", alert=alert)
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))  # added baseline
    calls.clear()

    # Churn modification → silent re-baseline, no event, no alert.
    (root / "x.txt").write_text("one-changed")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.modified == 0
    assert calls == []


@pytest.mark.asyncio
async def test_scan_added_only_does_not_dispatch(cairn_env, monkeypatch):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    calls = _record_dispatch(monkeypatch)
    alert = {"email": {"enabled": True, "to": ["a@example.com"]}}

    root = cairn_env / "fresh"
    root.mkdir()
    (root / "n1.txt").write_text("a")
    (root / "n2.txt").write_text("b")
    cid = await _make_collection(root, mode="worm", alert=alert)
    sm = get_sessionmaker()

    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.added == 2
    assert calls == []


@pytest.mark.asyncio
async def test_move_does_not_alert_but_deletion_does(cairn_env, monkeypatch):
    """A reconciled move is not a missing change → no alert; a genuine deletion still alerts."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    calls = _record_dispatch(monkeypatch)
    alert = {"email": {"enabled": True, "to": ["a@example.com"]}}

    root = cairn_env / "moves"
    root.mkdir()
    (root / "keep.txt").write_text("keep-bytes")
    (root / "drop.txt").write_text("drop-bytes")
    cid = await _make_collection(root, mode="worm", alert=alert)
    sm = get_sessionmaker()

    async with sm() as s:
        await scan_collection(s, await s.get(Collection, cid))  # baseline (added only) → no alert
    assert calls == []

    # Move keep.txt → renamed.txt (content unchanged) → one `moved`, NO alert.
    (root / "keep.txt").rename(root / "renamed.txt")
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.moved == 1 and summ.missing == 0
    assert calls == []

    # Genuinely delete drop.txt → a `missing` change still alerts.
    (root / "drop.txt").unlink()
    async with sm() as s:
        summ = await scan_collection(s, await s.get(Collection, cid))
        assert summ.missing == 1 and summ.moved == 0
    assert len(calls) == 1
    fired_alert, collection_name = calls[0]
    assert collection_name == "moves" and fired_alert.paths == ["drop.txt"]


# --- collection-form alert round-trip ------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()
    return TestClient(app)


def _run_check(coro_factory):
    from src import database

    database.reset_engine()

    async def _wrapped():
        try:
            return await coro_factory()
        finally:
            await database.get_engine().dispose()

    return asyncio.run(_wrapped())


def _csrf_token(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def test_collection_form_alert_roundtrips(cairn_env):
    base = cairn_env / "store"
    base.mkdir()
    newroot = base / "mycollection"
    newroot.mkdir()
    seedroot = cairn_env / "seed"
    seedroot.mkdir()

    async def seed():
        await _make_collection(seedroot)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post(
            "/collection",
            data={
                "csrf_token": token,
                "name": "Alerting Collection",
                "root": str(newroot),
                "mode": "worm",
                "ots": "none",
                "cadence": "3600",
                "excludes": "",
                "email_enabled": "true",
                "email_to": "alerts@example.com",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    # DB persisted the email alert into alert_json.
    async def fetch_alert():
        from src.database import get_sessionmaker
        from src.models.db import Collection

        async with get_sessionmaker()() as s:
            collection = await s.scalar(select(Collection).where(Collection.name == "Alerting Collection"))
            return collection.id, json.loads(collection.alert_json)

    cid, alert = _run_check(fetch_alert)
    assert alert["email"]["enabled"] is True
    assert alert["email"]["to"] == ["alerts@example.com"]

    # Edit page pre-fills the saved recipient.
    async def seed_noop():
        return None

    with _make_client(cairn_env, seed_noop) as client:
        r = client.get(f"/collection/{cid}/edit")
        assert r.status_code == 200
        assert "alerts@example.com" in r.text


# --- DB-backed SMTP server settings (app_settings) ------------------------------------------


@pytest.mark.asyncio
async def test_effective_settings_db_overrides_env(cairn_env, monkeypatch):
    """Empty DB falls back to env; once saved, DB values win and slot into Settings."""
    from src.config import Settings
    from src.database import get_sessionmaker
    from src.services import app_settings

    monkeypatch.setenv("CAIRN_SMTP_HOST", "env-host.example.com")
    base = Settings()
    assert base.smtp_host == "env-host.example.com"

    sm = get_sessionmaker()
    async with sm() as s:
        # No rows yet → pure env fallback (existing env-only deploys keep working).
        eff = await app_settings.effective_settings(s, base)
        assert eff.smtp_host == "env-host.example.com"

        await app_settings.save_smtp(
            s,
            host="db-host.example.com",
            port=2525,
            starttls=False,
            user="u@example.com",
            from_="cairn@example.com",
            provider="local",
            password="topsecret",
        )

    async with sm() as s:
        eff = await app_settings.effective_settings(s, base)

    assert eff.smtp_host == "db-host.example.com"  # DB overrides env
    assert eff.smtp_port == 2525
    assert eff.smtp_starttls is False
    assert eff.smtp_user == "u@example.com"
    assert eff.smtp_password == "topsecret"


@pytest.mark.asyncio
async def test_save_smtp_blank_password_keeps_existing(cairn_env):
    """A re-save with ``password=None`` (blank field) preserves the stored secret."""
    from src.database import get_sessionmaker
    from src.services import app_settings

    sm = get_sessionmaker()
    async with sm() as s:
        await app_settings.save_smtp(
            s, host="h", port=587, starttls=True, user="u", from_="f",
            provider="local", password="orig-secret",
        )
    async with sm() as s:
        assert await app_settings.smtp_password_is_set(s) is True
        await app_settings.save_smtp(
            s, host="h2", port=587, starttls=True, user="u", from_="f",
            provider="local", password=None,
        )
    async with sm() as s:
        overrides = await app_settings.get_smtp_overrides(s)

    assert overrides["smtp_host"] == "h2"  # other fields update
    assert overrides["smtp_password"] == "orig-secret"  # secret preserved


def test_settings_smtp_save_and_render(cairn_env):
    """POST /settings/smtp persists the server config; the page reflects it without echoing the password."""
    seedroot = cairn_env / "seed_smtp"
    seedroot.mkdir()

    async def seed():
        await _make_collection(seedroot)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post(
            "/settings/smtp",
            data={
                "csrf_token": token,
                "smtp_host": "relay.example.com",
                "smtp_port": "2525",
                "smtp_encryption": "none",
                "smtp_user": "cairn@example.com",
                "smtp_password": "hunter2",
                "smtp_from": "cairn@example.com",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

        page = client.get("/settings?tab=notifications")
        assert page.status_code == 200
        assert "relay.example.com" in page.text
        assert "2525" in page.text
        # The password is never rendered back; the form shows an "unchanged" hint instead.
        assert "hunter2" not in page.text
        assert "unchanged" in page.text
