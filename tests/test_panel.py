"""Control-panel routes: page renders, server-side file search/filter/pagination, htmx
mutations (acknowledge / accept / scan), root validation, verify (ots mocked), and mode toggle.

Uses TestClient with CAIRN_SCHEDULER_ENABLED=0 and a temp DB, mirroring the healthz tests in
tests/test_scheduler.py — including the reset_engine()-after-seed trick to avoid cross-loop
aiosqlite teardown warnings.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_panel.py``
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select


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


def _csrf_token(client) -> str:
    """Pull the CSRF token rendered into the page meta tag (sets the session cookie too)."""
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


async def _seed_collection(
    root: Path, *, ots_mode: str = "perfile", mode: str = "worm"
) -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s, user_id=uid, name=root.name, root=str(root), mode=mode, ots_mode=ots_mode
        )
        return collection.id


async def _seed_files(
    collection_id: int, *, n_ok: int, n_modified: int, n_missing: int, n_new: int = 0
) -> None:
    """Insert files directly (bypassing a real scan so we control statuses/counts)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        idx = 0
        for _ in range(n_new):
            # Freshly-added + fully stamped (mirrors a new collection after scan + OTS upgrade).
            s.add(FileEntry(
                collection_id=collection_id, relpath=f"new/file_{idx:05d}.txt", size=10,
                sha256="d" * 64, status="new", ots_state="complete",
                ots_path=f"/proofs/{collection_id}/new/file_{idx:05d}.txt.ots",
                first_seen=now, last_checked=now,
            ))
            idx += 1
        for _ in range(n_ok):
            s.add(FileEntry(
                collection_id=collection_id, relpath=f"ok/file_{idx:05d}.txt", size=10,
                sha256="a" * 64, status="ok", ots_state="complete",
                ots_path=f"/proofs/{collection_id}/ok/file_{idx:05d}.txt.ots",
                first_seen=now, last_checked=now,
            ))
            idx += 1
        for _ in range(n_modified):
            s.add(FileEntry(
                collection_id=collection_id, relpath=f"mod/file_{idx:05d}.txt", size=10,
                sha256="b" * 64, status="modified", ots_state="incomplete",
                first_seen=now, last_checked=now,
            ))
            idx += 1
        for _ in range(n_missing):
            s.add(FileEntry(
                collection_id=collection_id, relpath=f"gone/file_{idx:05d}.txt", size=10,
                sha256="c" * 64, status="missing", ots_state="none",
                first_seen=now, last_checked=now,
            ))
            idx += 1
        await s.commit()


async def _seed_event(collection_id: int, kind: str = "missing") -> int:
    from src.database import get_sessionmaker
    from src.models.db import Event

    async with get_sessionmaker()() as s:
        e = Event(collection_id=collection_id, kind=kind, detected_at=datetime.now(timezone.utc))
        s.add(e)
        await s.commit()
        return e.id


def _make_client(cairn_env, seed_coro):
    """Run an async seed coroutine on a throwaway loop, drop the engine, return a TestClient."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warning)
    return TestClient(app)


def _run_check(coro_factory):
    """Run a post-test DB check on a fresh loop, disposing the engine in-loop afterwards.

    Mirrors the reset_engine() discipline from tests/test_scheduler.py and disposes the engine
    on the same loop it was created, so aiosqlite's worker thread shuts down cleanly (no
    'Event loop is closed' teardown warning).
    """
    from src import database

    database.reset_engine()

    async def _wrapped():
        try:
            return await coro_factory()
        finally:
            await database.get_engine().dispose()

    return asyncio.run(_wrapped())


# --- page renders ---------------------------------------------------------------------------


def test_pages_render_200(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=3, n_modified=1, n_missing=1)
        await _seed_event(cid, "missing")

    with _make_client(cairn_env, seed) as client:
        async def _cid():
            from src.database import get_sessionmaker
            from src.models.db import Collection

            async with get_sessionmaker()() as s:
                return await s.scalar(select(Collection.id))

        for path in ("/", "/verify", "/learn", "/settings", "/settings?tab=verify", "/collection/new"):
            r = client.get(path)
            assert r.status_code == 200, (path, r.text[:300])
            assert "cairn-app" in r.text
        # collection detail + edit
        r = client.get("/collection/1")
        assert r.status_code == 200
        assert "Files" in r.text
        r = client.get("/collection/1/edit")
        assert r.status_code == 200
        assert "Edit" in r.text


def test_dark_mode_attribute_present(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(root)) as client:
        client.cookies.set("cairn_mode", "dark")
        r = client.get("/")
        assert 'data-mode="dark"' in r.text


# --- mode toggle ----------------------------------------------------------------------------


def test_mode_toggle_sets_cookie(cairn_env):
    root = cairn_env / "c"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(root)) as client:
        r = client.get("/mode/toggle", headers={"referer": "/"}, follow_redirects=False)
        assert r.status_code == 303
        cookie = r.headers.get("set-cookie", "")
        assert "cairn_mode=dark" in cookie


# --- server-side file search / filter / pagination ------------------------------------------


def test_files_pagination_returns_one_page_and_total(cairn_env):
    root = cairn_env / "big"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=60, n_modified=3, n_missing=2)  # 65 > page size 50

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files")
        assert r.status_code == 200
        # At most one page of rows (50), never the full 65.
        row_count = r.text.count('class="file-grid')  # header + rows; header is one
        # header row uses file-grid too, so rows = occurrences - 1
        assert (row_count - 1) <= 50
        assert "of 65 files" in r.text


def test_files_filter_issues_only_modified_or_missing(cairn_env):
    root = cairn_env / "f"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=5, n_modified=2, n_missing=1)

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files", params={"filter": "issues"})
        assert r.status_code == 200
        # 3 issue files (2 modified + 1 missing); no ok-status rows present.
        assert "3 matching" in r.text
        assert "ok/file_" not in r.text
        assert ("mod/file_" in r.text) and ("gone/file_" in r.text)


def test_files_search_matches_relpath(cairn_env):
    root = cairn_env / "s"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=5, n_modified=0, n_missing=0)

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files", params={"q": "file_00002"})
        assert r.status_code == 200
        assert "1 matching" in r.text


def test_new_only_collection_reads_all_clear_with_baseline_button(cairn_env):
    """A collection whose only non-ok files are `new` (informational) is healthy, not "Attention".

    Regression: `new` files used to force the collection status to "attention" with no way out (the
    Accept button was gated on modified+missing only, and a scan never promotes new→ok). Now the
    status reads "All clear" and a "Baseline new files" button is offered to move them into the
    "Verified OK" count.
    """
    root = cairn_env / "newonly"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=0, n_modified=0, n_missing=0, n_new=4)

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1")
        assert r.status_code == 200
        assert "All clear" in r.text
        assert "Attention" not in r.text
        # The baseline affordance is present (and labelled for new files, not "Accept changes").
        assert "Baseline new files" in r.text
        assert "/collection/1/accept" in r.text
        # Regression: the form must be a plain POST→redirect (a real page refresh), not an htmx
        # hx-post with hx-swap="none" that silently discards the redirected page and leaves the UI
        # stale after a successful re-baseline.
        assert 'action="/collection/1/accept"' in r.text
        assert 'hx-post="/collection/1/accept"' not in r.text


def test_tripwire_hides_notarization_column(cairn_env):
    root = cairn_env / "rom"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="none")
        await _seed_files(cid, n_ok=2, n_modified=0, n_missing=0)

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files")
        assert r.status_code == 200
        assert "file-grid--no-ots" in r.text
        assert "Notarization" not in r.text


# --- acknowledge ----------------------------------------------------------------------------


def test_acknowledge_marks_event_and_returns_partial(cairn_env):
    root = cairn_env / "ack"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=1, n_modified=0, n_missing=1)
        await _seed_event(cid, "missing")

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        # event id is 1
        r = client.post("/events/1/ack", headers={"X-CSRF-Token": token})
        assert r.status_code == 200, r.text
        # The acknowledged row partial no longer offers the Acknowledge CTA.
        assert "Acknowledge" not in r.text
        # OOB swaps are present (sidebar badge + need-action pill containers).
        assert "sidebar-alert-badge" in r.text and "open-events-pill" in r.text

    # Confirm persisted.
    async def check():
        from src.database import get_sessionmaker
        from src.models.db import Event

        async with get_sessionmaker()() as s:
            e = await s.get(Event, 1)
            return e.acknowledged_at is not None

    assert _run_check(check)


def test_acknowledge_requires_csrf(cairn_env):
    root = cairn_env / "csrf"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_event(cid, "missing")

    with _make_client(cairn_env, seed) as client:
        r = client.post("/events/1/ack")  # no token
        assert r.status_code == 403


# --- acknowledge all (bulk) -----------------------------------------------------------------


async def _seed_other_user_event(root: Path, username: str) -> None:
    """A second user with their own collection + one open event (for isolation tests)."""
    from src.database import get_sessionmaker
    from src.models.db import Event, User
    from src.services.collections import create_collection

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        u = User(username=username, is_admin=False, is_active=True, created_at=now)
        s.add(u)
        await s.flush()
        collection = await create_collection(
            s, user_id=u.id, name=root.name, root=str(root), mode="worm", ots_mode="none"
        )
        s.add(Event(collection_id=collection.id, kind="missing", detected_at=now))
        await s.commit()


def test_ack_all_acks_open_events_and_refreshes(cairn_env):
    root = cairn_env / "ackall"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=1, n_modified=1, n_missing=2)
        await _seed_event(cid, "missing")
        await _seed_event(cid, "missing")
        await _seed_event(cid, "modified")

    with _make_client(cairn_env, seed) as client:
        # The bulk control is offered while events are open.
        assert "/events/ack-all" in client.get("/").text
        token = _csrf_token(client)
        r = client.post("/events/ack-all", headers={"X-CSRF-Token": token})
        assert r.status_code == 200, r.text
        # Refreshed feed: no remaining Acknowledge CTAs, the pill cleared, OOB swaps present.
        assert "Acknowledge" not in r.text
        assert "need action" not in r.text
        assert "sidebar-alert-badge" in r.text and "open-events-pill" in r.text

    async def open_count():
        from src.database import get_sessionmaker
        from src.models.db import Event

        async with get_sessionmaker()() as s:
            return len(list(await s.scalars(
                select(Event).where(Event.acknowledged_at.is_(None))
            )))

    assert _run_check(open_count) == 0


def test_ack_all_requires_csrf(cairn_env):
    root = cairn_env / "ackallcsrf"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_event(cid, "missing")

    with _make_client(cairn_env, seed) as client:
        r = client.post("/events/ack-all")  # no token
        assert r.status_code == 403


def test_ack_all_is_scoped_to_current_user(cairn_env):
    mine = cairn_env / "mine"
    mine.mkdir()
    theirs = cairn_env / "theirs"
    theirs.mkdir()

    async def seed():
        cid = await _seed_collection(mine, ots_mode="none")  # implicit user's collection
        await _seed_event(cid, "missing")
        await _seed_other_user_event(theirs, "bob")  # another user's open event

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post("/events/ack-all", headers={"X-CSRF-Token": token})
        assert r.status_code == 200

    async def open_flags():
        from src.database import get_sessionmaker
        from src.models.db import Collection, Event

        async with get_sessionmaker()() as s:
            out = {}
            for cname in ("mine", "theirs"):
                cid = await s.scalar(select(Collection.id).where(Collection.name == cname))
                evs = list(await s.scalars(select(Event).where(Event.collection_id == cid)))
                out[cname] = [e.acknowledged_at is None for e in evs]
            return out

    out = _run_check(open_flags)
    assert out["mine"] == [False]   # current user's event acknowledged
    assert out["theirs"] == [True]  # other user's event untouched


def test_ack_all_button_hidden_when_nothing_open(cairn_env):
    root = cairn_env / "noopen"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        # An auto-acknowledged `added` event: in the feed, but not a nag.
        from src.database import get_sessionmaker
        from src.models.db import Event

        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(Event(collection_id=cid, kind="added", detected_at=now, acknowledged_at=now))
            await s.commit()

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text
        assert "/events/ack-all" not in html  # no bulk control
        assert "need action" not in html
        assert "Acknowledge" not in html  # the acked `added` row offers no per-event CTA either


# --- accept ---------------------------------------------------------------------------------


def test_accept_sets_files_ok(cairn_env):
    root = cairn_env / "accept"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=2, n_modified=3, n_missing=1)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post("/collection/1/accept", headers={"X-CSRF-Token": token},
                        follow_redirects=False)
        assert r.status_code == 303

    async def check():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        async with get_sessionmaker()() as s:
            statuses = list(await s.scalars(
                select(FileEntry.status).where(FileEntry.collection_id == 1)
            ))
        return statuses

    statuses = _run_check(check)
    # modified/new → ok, missing removed: all remaining are ok, none modified/missing.
    assert statuses, "files vanished"
    assert all(s == "ok" for s in statuses)
    assert "missing" not in statuses and "modified" not in statuses


# --- scan now ------------------------------------------------------------------------------


def _wait_op_done(client, collection_id: int = 1, tries: int = 300, warmup: int = 5) -> bool:
    """Poll op-status until the background operation has finished (it returns ``HX-Refresh`` once a
    running run goes idle). The ``warmup`` polls give the just-launched task time to create its run
    first, so we never mistake "not started yet" (also op=None) for "done". We poll with
    ``was_running=1`` to mirror the running badge's real poll URL — the endpoint only emits
    ``HX-Refresh`` on a genuine running→idle transition (issue #10)."""
    for i in range(tries):
        r = client.get(f"/collection/{collection_id}/op-status?was_running=1")
        if i >= warmup and r.headers.get("HX-Refresh") == "true":
            return True
    return False  # pragma: no cover


def test_scan_now_runs_async_and_records_run(cairn_env):
    """Scan now returns the live badge immediately (no 303 block) and the background scan records a
    completed kind='scan' run."""
    root = cairn_env / "scan"
    root.mkdir()
    (root / "watched.txt").write_text("data")

    with _make_client(cairn_env, lambda: _seed_collection(root, ots_mode="none")) as client:
        token = _csrf_token(client)
        r = client.post("/collection/1/scan", headers={"X-CSRF-Token": token},
                        follow_redirects=False)
        assert r.status_code == 200  # async: returns the status fragment, not a redirect
        assert 'id="op-status-1"' in r.text
        assert _wait_op_done(client), "background scan did not finish"

    async def count_runs():
        from src.database import get_sessionmaker
        from src.models.db import Run

        async with get_sessionmaker()() as s:
            return list(await s.scalars(select(Run).where(Run.collection_id == 1)))

    runs = _run_check(count_runs)
    assert len(runs) == 1
    assert runs[0].kind == "scan" and runs[0].result == "ok"


def test_scan_now_refused_while_operation_running(cairn_env, monkeypatch):
    """A second scan is refused while a run is already in progress for the collection.

    (A real running run can't simply be pre-seeded: the startup reaper clears any leftover
    ``running`` run, so we stub ``active_run`` to model an in-flight operation.)"""
    root = cairn_env / "busy"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(root, ots_mode="none")) as client:
        from src.models.db import Run
        from src.services import collections as collections_svc

        async def fake_active_run(session, collection_id):
            return Run(collection_id=collection_id, kind="scan", result="running")

        monkeypatch.setattr(collections_svc, "active_run", fake_active_run)
        token = _csrf_token(client)
        r = client.post("/collection/1/scan", headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert "already running" in r.text  # refused, the live badge reports the running op

    async def count_runs():
        from src.database import get_sessionmaker
        from src.models.db import Run

        async with get_sessionmaker()() as s:
            return list(await s.scalars(select(Run).where(Run.collection_id == 1)))

    # The guard refused → no real run row was created (the stub is a transient, never persisted).
    runs = _run_check(count_runs)
    assert runs == []


# --- stamp all (on-demand backfill) --------------------------------------------------------


async def _seed_unstamped(collection_id: int, root: Path, n: int) -> None:
    """Create n real files + tracked rows at ots_state='none' (a deliberately-unstamped baseline)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for i in range(n):
            (root / f"f{i}.txt").write_text(f"data-{i}")
            s.add(FileEntry(
                collection_id=collection_id, relpath=f"f{i}.txt", size=6, sha256=f"{i:064d}",
                status="ok", ots_state="none", first_seen=now, last_checked=now,
            ))
        await s.commit()


def test_stamp_all_endpoint_backfills_unstamped(cairn_env, monkeypatch):
    from src.services import ots

    root = cairn_env / "stampall"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_unstamped(cid, root, 3)
        return cid

    # Mock the ots CLI in-process: write <symlink>.ots for each staged input, like the real binary.
    def fake_run(args, timeout=ots.DEFAULT_TIMEOUT):
        for a in args:
            p = Path(a)
            if p.is_symlink():
                p.with_name(p.name + ".ots").write_bytes(b"proof")
        return 0, "", ""
    monkeypatch.setattr(ots, "_run_ots", fake_run)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post("/collection/1/stamp-all", headers={"X-CSRF-Token": token})
        assert r.status_code == 200  # async: returns the live badge fragment
        assert 'id="op-status-1"' in r.text
        assert _wait_op_done(client), "background stamp backfill did not finish"

    async def states_and_run():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry, Run

        async with get_sessionmaker()() as s:
            states = list(await s.scalars(
                select(FileEntry.ots_state).where(FileEntry.collection_id == 1)
            ))
            runs = list(await s.scalars(select(Run).where(Run.collection_id == 1)))
            return states, runs

    states, runs = _run_check(states_and_run)
    assert states and all(st == "incomplete" for st in states)
    # The backfill is recorded as a typed kind='stamp' run with exact progress.
    assert len(runs) == 1
    assert runs[0].kind == "stamp" and runs[0].total == 3 and runs[0].stamped == 3
    assert runs[0].result == "ok"


def test_stamp_all_button_only_for_perfile(cairn_env):
    perfile = cairn_env / "pf"
    perfile.mkdir()
    tripwire = cairn_env / "tw"
    tripwire.mkdir()

    async def seed():
        await _seed_collection(perfile, ots_mode="perfile")  # collection 1
        await _seed_collection(tripwire, ots_mode="none")     # collection 2

    with _make_client(cairn_env, seed) as client:
        assert "/collection/1/stamp-all" in client.get("/collection/1").text
        assert "/collection/2/stamp-all" not in client.get("/collection/2").text


def test_stamp_all_rejected_for_tripwire_collection(cairn_env):
    tripwire = cairn_env / "tw2"
    tripwire.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(tripwire, ots_mode="none")) as client:
        token = _csrf_token(client)
        r = client.post("/collection/1/stamp-all", headers={"X-CSRF-Token": token})
        assert r.status_code == 400


# --- root validation -----------------------------------------------------------------------


def _mkdir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_validate_root_accepts_existing_dir(cairn_env):
    root = cairn_env / "valid"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(_mkdir(cairn_env / "c"))) as client:
        r = client.get("/collection/validate-root", params={"path": str(root)})
        assert r.status_code == 200
        assert 'data-valid="1"' in r.text


def test_validate_root_rejects_missing_path(cairn_env):
    with _make_client(cairn_env, lambda: _seed_collection(_mkdir(cairn_env / "c"))) as client:
        r = client.get("/collection/validate-root", params={"path": str(cairn_env / "nope")})
        assert r.status_code == 200
        assert 'data-valid="0"' in r.text
        assert "rejected" in r.text


# --- create / verify -----------------------------------------------------------------------


def test_create_collection_via_post(cairn_env):
    base = cairn_env / "store"
    base.mkdir()
    newroot = base / "mycollection"
    newroot.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(_mkdir(cairn_env / "c"))) as client:
        token = _csrf_token(client)
        r = client.post(
            "/collection",
            data={
                "csrf_token": token,
                "name": "My Collection",
                "root": str(newroot),
                "mode": "worm",
                "ots": "perfile",
                "cadence": "3600",
                "excludes": "**/*.tmp\n",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text

    async def names():
        from src.database import get_sessionmaker
        from src.models.db import Collection

        async with get_sessionmaker()() as s:
            return list(await s.scalars(select(Collection.name)))

    assert "My Collection" in _run_check(names)


def test_verify_renders_verdict_ots_mocked(cairn_env, monkeypatch):
    root = cairn_env / "verify"
    root.mkdir()
    (root / "doc.txt").write_text("hello")

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        cid = await _seed_collection(root, ots_mode="perfile")
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(
                collection_id=cid, relpath="doc.txt", size=5, sha256="d" * 64,
                status="ok", ots_state="complete",
                ots_path=str(cairn_env / "proofs" / str(cid) / "doc.txt.ots"),
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
            await s.commit()

    with _make_client(cairn_env, seed) as client:
        # Mock ots.verify so no network/binary is touched.
        from src.services import ots as ots_svc

        def fake_verify(ots_path, digest, **kwargs):
            return ots_svc.VerifyResult(
                verified=True, state="complete", block_height=826123,
                existed_by="2026-02-14 18:22 UTC",
                calendars=["alice.btc.calendar.opentimestamps.org"],
                message="Success!",
            )

        monkeypatch.setattr(ots_svc, "verify", fake_verify)

        # Verify search returns the anchored file.
        r = client.get("/verify/search", params={"q": "doc"})
        assert r.status_code == 200
        assert "doc.txt" in r.text

        token = _csrf_token(client)
        r = client.post("/verify", data={"csrf_token": token, "file_id": 1})
        assert r.status_code == 200, r.text
        assert "Proof verified" in r.text
        assert "826,123" in r.text
        assert "2026-02-14 18:22 UTC" in r.text


def test_verify_deeplink_preselect_renders(cairn_env, monkeypatch):
    root = cairn_env / "dl"
    root.mkdir()
    (root / "a.bin").write_text("x")

    async def seed():
        from src.database import get_sessionmaker
        from src.models.db import FileEntry

        cid = await _seed_collection(root, ots_mode="perfile")
        now = datetime.now(timezone.utc)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(
                collection_id=cid, relpath="a.bin", size=1, sha256="e" * 64,
                status="ok", ots_state="complete", ots_path="/p/a.bin.ots",
                ots_stamped_at=now, first_seen=now, last_checked=now,
            ))
            await s.commit()

    with _make_client(cairn_env, seed) as client:
        r = client.get("/verify", params={"file": 1})
        assert r.status_code == 200
        # Deep-link renders the load-triggered verification POST hook.
        assert 'hx-post="/verify"' in r.text and "file_id" in r.text


# --- sort / paginate / notarized column (improve-file-browser) ------------------------------

_MONO_RE = r'<span class="mono">([^<]+)</span>'


async def _seed_custom(collection_id: int, specs: list[dict]) -> None:
    """Insert files with explicit timestamps/state so sort order is deterministic.

    Each ``spec`` is a dict of ``FileEntry`` overrides (always include ``relpath``); sensible
    defaults fill the rest. Rows are inserted in list order, so the first spec gets the lowest id.
    """
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for spec in specs:
            kw = dict(
                collection_id=collection_id, size=10, sha256="a" * 64, status="ok",
                ots_state="none", first_seen=now, last_checked=now,
            )
            kw.update(spec)
            s.add(FileEntry(**kw))
        await s.commit()


def test_default_order_is_newest_activity_first(cairn_env):
    root = cairn_env / "ord"
    root.mkdir()
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_custom(cid, [
            {"relpath": "a.txt", "last_changed": base},                       # oldest
            {"relpath": "b.txt", "last_changed": base + timedelta(days=3)},   # newest
            {"relpath": "c.txt", "last_changed": base + timedelta(days=1)},   # tie group...
            {"relpath": "d.txt", "last_changed": base + timedelta(days=1)},   # ...broken by path
        ])

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files")
        assert r.status_code == 200
        # last_changed desc, then relpath asc as the stable tiebreak.
        assert re.findall(_MONO_RE, r.text) == ["b.txt", "c.txt", "d.txt", "a.txt"]


def test_sort_by_column_and_direction_with_fallback(cairn_env):
    root = cairn_env / "srt"
    root.mkdir()
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_custom(cid, [
            {"relpath": "x.txt", "size": 30, "last_changed": base + timedelta(days=1),
             "ots_state": "complete", "ots_stamped_at": base + timedelta(days=5)},
            {"relpath": "y.txt", "size": 10, "last_changed": base + timedelta(days=2),
             "ots_state": "complete", "ots_stamped_at": base + timedelta(days=1)},
            {"relpath": "z.txt", "size": 20, "last_changed": base + timedelta(days=3),
             "ots_state": "complete", "ots_stamped_at": base + timedelta(days=3)},
        ])

    with _make_client(cairn_env, seed) as client:
        # size asc -> 10,20,30
        r = client.get("/collection/1/files", params={"sort": "size", "dir": "asc"})
        assert re.findall(_MONO_RE, r.text) == ["y.txt", "z.txt", "x.txt"]
        # notarized desc -> stamp days 5,3,1
        r = client.get("/collection/1/files", params={"sort": "notarized", "dir": "desc"})
        assert re.findall(_MONO_RE, r.text) == ["x.txt", "z.txt", "y.txt"]
        # unknown sort/dir falls back to the default (modified desc) -> last_changed 3,2,1
        r = client.get("/collection/1/files", params={"sort": "bogus", "dir": "sideways"})
        assert re.findall(_MONO_RE, r.text) == ["z.txt", "y.txt", "x.txt"]


def test_pagination_preserves_query_and_sort(cairn_env):
    root = cairn_env / "pg"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_files(cid, n_ok=60, n_modified=3, n_missing=2)  # ok/file_00000..00059

    with _make_client(cairn_env, seed) as client:
        # Page 2 of the 60 "ok" files, sorted by path asc — search + sort + page compose.
        r = client.get(
            "/collection/1/files",
            params={"q": "ok/file_", "sort": "path", "dir": "asc", "page": 1},
        )
        assert r.status_code == 200
        assert "60 matching" in r.text          # the search total survives paging
        assert "Page 2 of 2" in r.text          # page-of-total indicator
        order = re.findall(_MONO_RE, r.text)
        assert len(order) == 10                  # the trailing slice, not the full set
        assert order[0] == "ok/file_00050.txt"
        assert "ok/file_00000.txt" not in r.text
        # Sort is carried across pages (hidden mirrors + pager hx-vals).
        assert 'name="sort" value="path"' in r.text
        assert 'name="dir" value="asc"' in r.text
        assert '"sort": "path"' in r.text


def test_notarized_row_shows_date_and_verify_link(cairn_env):
    root = cairn_env / "nt"
    root.mkdir()
    stamp = datetime(2026, 5, 30, 10, 0, tzinfo=timezone.utc)
    unstamped_changed = datetime(2026, 4, 15, 9, 0, tzinfo=timezone.utc)

    async def seed():
        cid = await _seed_collection(root, ots_mode="perfile")
        await _seed_custom(cid, [
            {"relpath": "anchored.txt", "ots_state": "complete", "ots_stamped_at": stamp,
             "ots_path": "/p/anchored.txt.ots",
             "last_changed": datetime(2026, 1, 1, tzinfo=timezone.utc)},
            {"relpath": "unstamped.txt", "ots_state": "none", "last_changed": unstamped_changed},
        ])

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files", params={"sort": "path", "dir": "asc"})
        assert r.status_code == 200
        # Notarized file: stamp date shown + complete proof deep-links to verify.
        assert "30 May 2026" in r.text
        assert 'href="/verify?file=1"' in r.text
        # Unstamped file falls back to its last-changed date (no row is dateless).
        assert "15 Apr 2026" in r.text


def test_tripwire_row_falls_back_to_modified_date(cairn_env):
    root = cairn_env / "tw"
    root.mkdir()
    changed = datetime(2026, 3, 10, 8, 0, tzinfo=timezone.utc)

    async def seed():
        cid = await _seed_collection(root, ots_mode="none")
        await _seed_custom(cid, [
            {"relpath": "rom.bin", "ots_state": "none", "last_changed": changed},
        ])

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/files")
        assert r.status_code == 200
        assert "file-grid--no-ots" in r.text     # notarization column hidden
        assert "Notarized" not in r.text
        assert "10 Mar 2026" in r.text            # modified-date fallback present


# --- issue review + recovery (add-issue-review-and-recovery) ---------------------------------


async def _seed_missing_with_event(collection_id: int, relpath: str = "gone/photo.jpg") -> int:
    """A missing, previously-notarized file with its open `missing` event (file_id linked)."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        fe = FileEntry(
            collection_id=collection_id, relpath=relpath, size=4321,
            sha256="d" * 64, status="missing", ots_state="complete",
            first_seen=now, last_checked=now,
        )
        s.add(fe)
        await s.commit()
        e = Event(collection_id=collection_id, file_id=fe.id, kind="missing", detected_at=now)
        s.add(e)
        await s.commit()
        return e.id


def test_review_page_lists_missing_file_with_story_and_recovery(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_missing_with_event(cid, "2019/IMG_4421.jpg")

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/review")
        assert r.status_code == 200
        body = r.text
        assert "IMG_4421.jpg" in body                       # the file is listed
        assert "Gone from disk" in body                      # what-happened story
        assert "proof of prior existence kept" in body       # notarized note
        assert "Copy paths" in body and "Copy full paths" in body  # recovery affordance
        assert "/collection/1/review/accept" in body         # bulk accept
        assert "/collection/1/review/ack-all" in body        # bulk acknowledge
        assert "2019/IMG_4421.jpg" in body                   # recovery copy list payload
        # The two bulk actions are disambiguated, not two look-alike buttons: each carries a
        # plain-English consequence pill so Acknowledge (note it) can't be confused with Accept
        # (rewrite the baseline).
        assert "Baseline unchanged" in body
        assert "Rewrites your baseline" in body


def test_dashboard_issue_count_links_to_review(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_missing_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text
        assert "/collection/1/review" in html                # the card legend deep-links to review


def test_review_acknowledge_marks_event_and_refreshes_counts(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_missing_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post("/events/1/ack?view=review", headers={"X-CSRF-Token": token})
        assert r.status_code == 200
        assert "Acknowledged" in r.text                       # row flipped to acked state
        assert 'id="review-open-pill"' in r.text              # OOB pill refresh
        assert 'id="sidebar-alert-badge"' in r.text           # OOB sidebar badge refresh

    def check():
        from src.database import get_sessionmaker
        from src.models.db import Event

        async def go():
            async with get_sessionmaker()() as s:
                e = await s.get(Event, 1)
                return e.acknowledged_at
        return go()

    assert _run_check(check) is not None


def test_review_accept_clears_issues_and_stays_on_review(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_missing_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post("/collection/1/review/accept", headers={"X-CSRF-Token": token},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1/review"
        # After accept, the review page is the all-clear empty state.
        page = client.get("/collection/1/review").text
        assert "All clear" in page


def test_review_empty_state_when_all_clear(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await _seed_collection(root)
        await _seed_files(cid, n_ok=3, n_modified=0, n_missing=0)

    with _make_client(cairn_env, seed) as client:
        r = client.get("/collection/1/review")
        assert r.status_code == 200
        assert "All clear" in r.text
        assert "Acknowledge all" not in r.text                 # no actions when nothing to review


def test_legacy_corpus_urls_redirect_to_collection(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()
    with _make_client(cairn_env, lambda: _seed_collection(root)) as client:
        # Detail + subpaths -> singular /collection/...
        for src, dst in [
            ("/corpus/1", "/collection/1"),
            ("/corpus/1/review", "/collection/1/review"),
        ]:
            r = client.get(src, follow_redirects=False)
            assert r.status_code == 308, src
            assert r.headers["location"] == dst, (src, r.headers["location"])
        # Bare legacy list paths -> plural /collections (never a 405 dead end)
        for src in ["/corpus", "/corpus/", "/corpora"]:
            r = client.get(src, follow_redirects=False)
            assert r.status_code == 308, src
            assert r.headers["location"] == "/collections", (src, r.headers["location"])
        # And following the redirect fully lands on a 200.
        assert client.get("/corpus", follow_redirects=True).status_code == 200


def test_auto_baseline_toggle_persists_through_form(cairn_env):
    root = cairn_env / "abform"
    root.mkdir()

    with _make_client(cairn_env, lambda: _seed_collection(root)) as client:
        token = _csrf_token(client)
        # Turn the toggle ON via the edit form POST.
        r = client.post(
            "/collection/1",
            headers={"X-CSRF-Token": token},
            data={
                "name": "abform", "root": str(root), "mode": "worm", "ots": "perfile",
                "cadence": "900", "verify_cadence": "604800", "auto_baseline": "on",
                "excludes": "", "email_enabled": "false", "email_to": "",
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        # Edit form reflects the saved ON state.
        html = client.get("/collection/1/edit").text
        assert '<option value="on" selected' in html

    def check():
        from src.database import get_sessionmaker
        from src.models.db import Collection

        async def go():
            async with get_sessionmaker()() as s:
                return (await s.get(Collection, 1)).auto_baseline_new
        return go()

    assert _run_check(check) is True
