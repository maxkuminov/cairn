"""Alert deep links: the scanner's dispatch site and the panel's "Panel address" setting.

The channel-rendering contracts live in tests/test_notify.py; what is asserted here is the pair of
guarantees that sit *around* them:

1. A scan's alert carries ``{public_url}/collection/{id}/review`` when a panel address is
   configured — and, far more importantly, **still dispatches with no link at all** when anything
   in the link path blows up. The link is a convenience; being told that files went missing is the
   product. A cosmetic feature must never be able to suppress an alert.
2. ``POST /settings/panel-url`` is the fail-loud boundary: an invalid address is refused with the
   reason, leaving the stored value untouched; an empty save deletes the row so ``CAIRN_PUBLIC_URL``
   becomes visible again; only admins may save.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_alert_links.py``
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from sqlalchemy import select

PUBLIC_URL = "https://cairn.example.com"


# --- temp-DB fixture (mirrors tests/test_notify.py) -----------------------------------------


@pytest.fixture
def cairn_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "single")
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("CAIRN_PUBLIC_URL", PUBLIC_URL)

    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    yield tmp_path
    get_settings.cache_clear()


async def _make_alerting_collection(root: Path) -> int:
    """A WORM collection with email alerting on, so a missing file dispatches."""
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s,
            user_id=uid,
            name=root.name,
            root=str(root),
            mode="worm",
            alert={"email": {"enabled": True, "to": ["a@example.com"]}},
        )
        return collection.id


def _record_dispatch(monkeypatch) -> list:
    """Replace the real fan-out with a recorder, so nothing is sent and every Alert is captured."""
    from src.notify import dispatch as dispatch_mod

    calls: list = []

    async def fake_dispatch(alert, collection, settings):
        calls.append(alert)
        return {}

    monkeypatch.setattr(dispatch_mod, "dispatch", fake_dispatch)
    return calls


async def _scan(cid: int):
    """One scan in its own session, as the scheduler and CLI both do."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    async with get_sessionmaker()() as s:
        return await scan_collection(s, await s.get(Collection, cid))


# --- 8.11 the link reaches the alert --------------------------------------------------------


async def test_scan_alert_carries_review_link(cairn_env, monkeypatch):
    calls = _record_dispatch(monkeypatch)

    root = cairn_env / "worm"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    (root / "drop.txt").write_text("bye")
    cid = await _make_alerting_collection(root)

    await _scan(cid)  # baseline: added-only, no alert
    assert calls == []

    (root / "drop.txt").unlink()
    summary = await _scan(cid)
    assert summary.missing == 1

    assert len(calls) == 1
    assert calls[0].url == f"{PUBLIC_URL}/collection/{cid}/review"


async def test_scan_alert_has_no_link_when_public_url_unset(cairn_env, monkeypatch):
    """No configured address ⇒ no link, never a guessed or relative one."""
    from src.config import get_settings

    monkeypatch.delenv("CAIRN_PUBLIC_URL", raising=False)
    get_settings.cache_clear()

    calls = _record_dispatch(monkeypatch)
    root = cairn_env / "nourl"
    root.mkdir()
    (root / "drop.txt").write_text("bye")
    cid = await _make_alerting_collection(root)

    await _scan(cid)
    (root / "drop.txt").unlink()
    await _scan(cid)

    assert len(calls) == 1
    assert calls[0].url is None


async def test_stored_override_wins_for_the_alert_link(cairn_env, monkeypatch):
    """The panel-saved address beats the env one, the same as every other overlaid setting."""
    from src.database import get_sessionmaker
    from src.services import app_settings

    calls = _record_dispatch(monkeypatch)
    root = cairn_env / "override"
    root.mkdir()
    (root / "drop.txt").write_text("bye")
    cid = await _make_alerting_collection(root)
    await _scan(cid)

    async with get_sessionmaker()() as s:
        await app_settings.save_public_url(s, "https://panel.example.org/cairn/")

    (root / "drop.txt").unlink()
    await _scan(cid)

    assert len(calls) == 1
    assert calls[0].url == f"https://panel.example.org/cairn/collection/{cid}/review"


# --- 8.12 failure injection: a link error must not cost the operator the alert ---------------


@pytest.mark.parametrize("target", ["panel_link", "effective_settings"])
async def test_link_failure_still_dispatches_a_link_free_alert(cairn_env, monkeypatch, target):
    """Blow up the link path; the alert must still go out exactly once, with ``url=None``.

    Both halves of that path are exercised: the link builder itself and the settings overlay it
    reads the address from. Either failing is a bug in a convenience feature — it must never
    degrade into "the operator was never told a file went missing".
    """
    from src.services import app_settings, panel_url

    def boom(*args, **kwargs):
        raise RuntimeError("link builder exploded")

    if target == "panel_link":
        monkeypatch.setattr(panel_url, "panel_link", boom)
    else:
        monkeypatch.setattr(app_settings, "effective_settings", boom)

    calls = _record_dispatch(monkeypatch)

    root = cairn_env / "boom"
    root.mkdir()
    (root / "keep.txt").write_text("keep")
    (root / "drop.txt").write_text("bye")
    cid = await _make_alerting_collection(root)
    await _scan(cid)

    (root / "drop.txt").unlink()
    summary = await _scan(cid)

    assert len(calls) == 1, "a failing link must not suppress the alert"
    assert calls[0].url is None
    assert calls[0].paths == ["drop.txt"]
    # The run still reaches a terminal state and reports the detection accurately.
    assert summary.result in ("ok", "partial")
    assert summary.missing == 1

    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        results = list(await s.scalars(select(Run.result).order_by(Run.id)))
    assert results and all(r in ("ok", "partial") for r in results)


# --- 8.13 POST /settings/panel-url ----------------------------------------------------------


def _csrf_token(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def _make_client(cairn_env, seed_coro=None):
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    if seed_coro is not None:
        asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warnings)
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


async def _seed_user(is_admin: bool = True) -> None:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        user = await s.scalar(select(User).order_by(User.id).limit(1))
        user.is_admin = is_admin
        await s.commit()


def _stored_public_url():
    async def check():
        from src.database import get_sessionmaker
        from src.models.db import AppSetting

        async with get_sessionmaker()() as s:
            row = await s.get(AppSetting, "public_url")
            return row.value if row else None

    return _run_check(check)


def test_panel_url_save_normalizes_and_persists(cairn_env):
    with _make_client(cairn_env, lambda: _seed_user(True)) as client:
        token = _csrf_token(client)
        r = client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": "https://panel.example.org/cairn/"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "saved=url" in r.headers["location"]

        # Round-trips into the form, normalized — the operator sees what links are built from.
        page = client.get("/settings?tab=notifications&saved=url").text
        assert 'value="https://panel.example.org/cairn"' in page
        assert "https://panel.example.org/cairn/healthz" in page

    assert _stored_public_url() == "https://panel.example.org/cairn"


def test_panel_url_invalid_is_refused_and_leaves_the_stored_value(cairn_env):
    with _make_client(cairn_env, lambda: _seed_user(True)) as client:
        token = _csrf_token(client)
        client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": PUBLIC_URL},
            follow_redirects=False,
        )
        r = client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": "javascript:alert(1)"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert "saved=urlerr" in r.headers["location"]

        page = client.get(r.headers["location"]).text
        assert "Not saved" in page
        # The reason from normalize_public_url is surfaced verbatim, not a generic "invalid".
        assert "must start with" in page
        assert f'value="{PUBLIC_URL}"' in page  # the good value survives

    assert _stored_public_url() == PUBLIC_URL


def test_panel_url_empty_save_deletes_the_row(cairn_env):
    """Clearing must delete, not store "" — an empty row would permanently shadow the env value."""
    with _make_client(cairn_env, lambda: _seed_user(True)) as client:
        token = _csrf_token(client)
        client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": "https://panel.example.org"},
            follow_redirects=False,
        )
        r = client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": "  "},
            follow_redirects=False,
        )
        assert r.status_code == 303 and "saved=url" in r.headers["location"]
        # The env value is visible again in the form.
        assert f'value="{PUBLIC_URL}"' in client.get("/settings?tab=notifications").text

    assert _stored_public_url() is None


def test_panel_url_save_is_admin_only(cairn_env):
    with _make_client(cairn_env, lambda: _seed_user(False)) as client:
        token = _csrf_token(client)
        r = client.post(
            "/settings/panel-url",
            data={"csrf_token": token, "public_url": "https://evil.example.org"},
            follow_redirects=False,
        )
        assert r.status_code == 403
        # Non-admins get the read-only presentation, not a form.
        page = client.get("/settings?tab=notifications").text
        assert "The panel address is managed by an administrator." in page
        assert 'action="/settings/panel-url"' not in page

    assert _stored_public_url() is None


def test_settings_labels_the_example_health_url_when_unconfigured(cairn_env, monkeypatch):
    """With no address configured the shown health URL must read as an example, not as yours."""
    from src.config import get_settings

    monkeypatch.delenv("CAIRN_PUBLIC_URL", raising=False)
    get_settings.cache_clear()

    with _make_client(cairn_env, lambda: _seed_user(True)) as client:
        page = client.get("/settings?tab=notifications").text
        assert "https://cairn.example.com/healthz" in page
        assert "That is an illustrative address" in page
