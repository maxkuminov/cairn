"""UX audit sprint 1, slice B: the dashboard/collection surfaces and the accept-family guard.

Two halves:

* **Honesty of the tiles and links** (#18, #20, #31, #32) — the "Open issues" tile is a link that
  goes somewhere useful, the sidebar badge counts the same population as the tile beside it, no
  coverage claim is computed over a population that includes missing files, the two
  not-yet-confirmed proof states are never summed, and a collection watching nothing never reads
  green.
* **The D14 population fingerprint** (#14) — both accept-family routes act only on the population
  their form was rendered for, and refuse (with no mutation at all) on any drift, including the
  cases a naive `id + status` preimage cannot see: SQLite rowid reuse, a row deleted and re-created
  at the same path with the same bytes, and an ABA on the open-event population.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ux_dashboard.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.conftest import seed_collection  # noqa: F401  (cairn_env comes from conftest)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


# --- harness --------------------------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    """Run an async seed coroutine on a throwaway loop, drop the engine, return a TestClient."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warning)
    return TestClient(app)


async def _with_session(fn):
    """Await ``fn(session)`` against a throwaway engine, disposed on the current loop.

    Deliberately NOT the app's cached engine: these calls interleave with a live ``TestClient``
    (that is the whole point — they play the part of a scan committing between render and submit),
    so they must not disturb the engine the client's event loop is using.
    """
    from src.config import get_settings
    from src.database import _configure_sqlite

    engine = create_async_engine(get_settings().database_url)
    sa_event.listen(engine.sync_engine, "connect", _configure_sqlite)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as s:
            return await fn(s)
    finally:
        await engine.dispose()


def _aside(fn):
    """Sync wrapper around :func:`_with_session` for use outside a running loop."""
    return asyncio.run(_with_session(fn))


def _csrf(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


@contextlib.contextmanager
def _capture_context():
    """Record the template context every route render publishes (the slice-C seam contract)."""
    from src.control_panel import routes

    captured: list[tuple[str, dict]] = []
    orig = routes.templates.TemplateResponse

    def wrapper(request, name, context=None, *a, **kw):
        captured.append((name, dict(context or {})))
        return orig(request, name, context, *a, **kw)

    routes.templates.TemplateResponse = wrapper
    try:
        yield captured
    finally:
        routes.templates.TemplateResponse = orig


async def _mk_files(session, cid, specs):
    """Insert ``FileEntry`` rows from ``(relpath, status, ots_state)`` (+ optional overrides)."""
    from src.models.db import FileEntry

    for spec in specs:
        relpath, status, ots_state = spec[0], spec[1], spec[2]
        extra = spec[3] if len(spec) > 3 else {}
        session.add(
            FileEntry(
                collection_id=cid,
                relpath=relpath,
                size=extra.get("size", 10),
                sha256=extra.get("sha256", "a" * 64),
                status=status,
                ots_state=ots_state,
                first_seen=extra.get("first_seen", NOW),
                last_checked=NOW,
                **({"id": extra["id"]} if "id" in extra else {}),
            )
        )
    await session.commit()


async def _mk_event(session, cid, file_id, kind, *, acked=False):
    from src.models.db import Event

    e = Event(
        collection_id=cid,
        file_id=file_id,
        kind=kind,
        detected_at=NOW,
        acknowledged_at=NOW if acked else None,
    )
    session.add(e)
    await session.commit()
    return e.id


def _fingerprint(collection_id: int, scope: str) -> str:
    """The fingerprint the page would publish, computed through the production helper."""

    async def _go(s):
        from src.control_panel.routes import _population_fingerprint, _read_population
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        return _population_fingerprint(collection, await _read_population(s, collection, scope))

    return _aside(_go)


async def _statuses(session, cid):
    from src.models.db import FileEntry

    return sorted(
        (r, p)
        for r, p in (
            await session.execute(
                select(FileEntry.status, FileEntry.relpath).where(
                    FileEntry.collection_id == cid
                )
            )
        ).all()
    )


# --- #18: the Open-issues tile and the sidebar badge -----------------------------------------


def test_issues_tile_is_a_link_to_the_single_affected_collections_review(cairn_env):
    root = cairn_env / "one"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("ok.txt", "ok", "complete"),
            ("gone.txt", "missing", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text
        assert '<a class="card tile tile--link" href="/collection/1/review">' in body
        assert "· Review" in body
        # Never `/review` — a 404 until #27 (design D4).
        assert 'href="/review"' not in body


def test_issues_tile_points_at_the_collections_list_when_several_are_affected(cairn_env):
    a, b = cairn_env / "a", cairn_env / "b"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("x", "missing", "none")]))
        await _with_session(lambda s: _mk_files(s, cb, [("y", "modified", "none")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text
        assert '<a class="card tile tile--link" href="/collections">' in body
        assert 'href="/review"' not in body


def test_issues_tile_is_inert_at_zero(cairn_env):
    root = cairn_env / "clean"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("ok.txt", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text
        assert "tile--link" not in body
        assert '<div class="card tile">' in body


def test_sidebar_badge_is_labelled_and_counts_missing_plus_modified(cairn_env):
    root = cairn_env / "badge"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone.txt", "missing", "none"),
            ("changed.txt", "modified", "none"),
            ("fine.txt", "ok", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text
        assert '<span class="nav-badge"' in body
        assert 'aria-label="2 files missing or changed — open dashboard"' in body
        # The badge and the tile beside it now summarise the same population.
        assert re.search(r'class="nav-badge"[^>]*>2</span>', body)
        assert ">2</div>" in body  # the Open issues tile value


def test_badge_label_is_singular_at_one(cairn_env):
    root = cairn_env / "one-issue"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        assert 'aria-label="1 file missing or changed — open dashboard"' in client.get("/").text


# --- #20 / D13: coverage is a ratio over one population --------------------------------------


def test_unstamped_files_block_all_confirmed_and_show_the_ratio(cairn_env):
    root = cairn_env / "partial"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a", "ok", "complete"),
            ("b", "ok", "none"),
            ("c", "ok", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        for url in ("/", "/collection/1"):
            body = client.get(url).text
            assert "all confirmed" not in body, url
        detail = client.get("/collection/1").text
        assert "1 / 3" in detail
        assert "2 not stamped" in detail


def test_a_missing_files_proof_does_not_fill_the_coverage_ratio(cairn_env):
    """One missing file with a complete proof + one present unstamped file must read 0 / 1."""
    root = cairn_env / "mixed"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone.txt", "missing", "complete"),
            ("here.txt", "ok", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        detail = client.get("/collection/1").text
        assert "0 / 1" in detail
        assert "all confirmed" not in detail
        assert "1 not stamped" in detail


def test_queued_and_pending_confirmation_are_named_separately_never_summed(cairn_env):
    root = cairn_env / "twostates"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("q1", "ok", "pending"),
            ("q2", "ok", "pending"),
            ("i1", "ok", "incomplete"),
            ("done", "ok", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        for url in ("/", "/collection/1"):
            body = client.get(url).text
            assert "2 queued" in body, url
            assert "1 pending confirmation" in body, url
            # The old wording summed them: 2 + 1 = 3.
            assert "3 pending" not in body, url


def test_fleet_proof_tile_counts_only_notarized_collections(cairn_env):
    tripwire, perfile = cairn_env / "trip", cairn_env / "per"
    tripwire.mkdir()
    perfile.mkdir()

    async def seed():
        ct = await seed_collection(tripwire, ots_mode="none")
        cp = await seed_collection(perfile, ots_mode="perfile")
        # 500 tripwire files, none of which can ever be stamped.
        await _with_session(lambda s: _mk_files(s, ct, [(f"t{i}", "ok", "none") for i in range(5)]))
        await _with_session(lambda s: _mk_files(s, cp, [("p1", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text
        # Numerator and denominator both exclude the tripwire collection, so the fleet tile reads
        # "all confirmed" and ships no un-clearable "not stamped" count.
        assert "all confirmed · across 1 notarized collection" in body
        assert "not stamped" not in body


# --- #31: a collection watching nothing is never green ---------------------------------------


def test_zero_file_collection_never_reads_all_clear(cairn_env):
    root = cairn_env / "empty"
    root.mkdir()

    async def seed():
        await seed_collection(root)

    with _make_client(cairn_env, seed) as client:
        dash = client.get("/").text
        detail = client.get("/collection/1").text
        frag = client.get("/collection/1/op-status").text
        review = client.get("/collection/1/review").text
        for name, body in (
            ("dashboard", dash), ("detail", detail), ("op-status", frag), ("review", review)
        ):
            assert "All clear" not in body, name
        assert "No files indexed yet" in dash
        assert "No files indexed yet" in detail
        assert "No files indexed" in frag
        # The review page's zero-issue card: an empty collection has nothing to review, which is
        # not the same claim as "nothing is missing or changed".
        assert "No files indexed yet" in review
        assert "All files verified" not in dash


# --- #14 / D7: the collection-detail action hierarchy ----------------------------------------


def test_detail_header_offers_no_accept_while_issues_are_open(cairn_env):
    root = cairn_env / "issues"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone", "missing", "none"),
            ("fresh", "new", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1").text
        assert 'action="/collection/1/accept"' not in body
        assert "Accept changes" not in body
        assert "Baseline new files" not in body
        assert 'href="/collection/1/review"' in body


def test_detail_header_hides_the_baseline_form_while_an_event_is_open(cairn_env):
    """Zero issues, `new` files, one open event: `accept` would clear that alert (design D7)."""
    root = cairn_env / "restored"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))
        await _with_session(lambda s: _mk_event(s, cid, None, "missing"))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1").text
        assert "Baseline new files" not in body
        assert 'action="/collection/1/accept"' not in body
        assert 'href="/collection/1/review"' in body


def test_detail_header_offers_the_light_baseline_confirm_when_everything_is_quiet(cairn_env):
    root = cairn_env / "quiet"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1").text
        assert "Baseline new files" in body
        assert "Baseline 1 new file as the expected version?" in body
        assert 'name="population_fp"' in body


# --- #32 / D11: view + filter deep links -----------------------------------------------------


def test_filter_deep_link_lands_filtered_on_the_list_view(cairn_env):
    root = cairn_env / "deeplink"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a", "ok", "none"),
            ("b", "ok", "none"),
            ("c", "missing", "none"),
            ("d", "modified", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1?view=list&filter=issues").text
        assert 'data-view="list"' in body
        assert '<button type="button" class="seg__btn is-active" data-view="list">' in body
        assert 'value="issues" checked' in body
        # The initial list query is filtered, not just the radio.
        assert "2 matching" in body


def test_a_filter_without_an_explicit_view_implies_the_list_view(cairn_env):
    root = cairn_env / "implied"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("c", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        assert 'data-view="list"' in client.get("/collection/1?filter=issues").text
        # An explicit view=tree is honoured as given.
        assert 'data-view="tree"' in client.get("/collection/1?view=tree&filter=issues").text
        # Defaults reproduce today's render; an unknown filter falls back to `all`.
        assert 'data-view="tree"' in client.get("/collection/1?filter=bogus").text


# --- D14: the context contract published for slice C -----------------------------------------


def test_review_route_publishes_the_fingerprint_and_stale_flag(cairn_env):
    root = cairn_env / "ctx"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            plain = client.get("/collection/1/review").text
            client.get("/collection/1/review?stale=1")
            client.get("/collection/1/review?stale=yes")

    ctxs = [c for name, c in captured if name == "collection_review.html"]
    assert len(ctxs) == 3
    for c in ctxs:
        assert re.fullmatch(r"[0-9a-f]{64}", c["population_fp"])
    assert ctxs[0]["stale"] is False
    assert ctxs[1]["stale"] is True
    assert ctxs[2]["stale"] is False  # only `1` is recognized, exactly as view/filter are

    # The template half renders it: the Accept form carries the very fingerprint that was minted,
    # so a POST of the rendered page matches the recount (design D14).
    assert f'name="population_fp" value="{ctxs[0]["population_fp"]}"' in plain


# --- D14: the guard, both routes -------------------------------------------------------------

ROUTES = [
    ("/collection/1/accept", "baseline-new"),
    ("/collection/1/review/accept", "review-accept"),
]


def _post(client, url, fp, token):
    return client.post(
        url,
        data={"population_fp": fp} if fp is not None else {},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def _assert_refused(r):
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == "/collection/1/review?stale=1"


@pytest.mark.parametrize("url,scope", ROUTES)
def test_accept_refuses_when_a_scan_lands_between_render_and_submit(cairn_env, url, scope):
    root = cairn_env / "drift"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("changed", "modified", "none"),
        ]))
        await _with_session(lambda s: _mk_event(s, cid, None, "modified"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(1, scope)  # what the page published at render time

        # ... and now a scan records another missing file, plus its alarming event.
        async def scan(s):
            await _mk_files(s, 1, [("vanished", "missing", "complete")])
            from src.models.db import FileEntry

            fid = await s.scalar(
                select(FileEntry.id).where(FileEntry.relpath == "vanished")
            )
            await _mk_event(s, 1, fid, "missing")

        _aside(scan)
        _assert_refused(_post(client, url, fp, token))

    def check(s):
        return _statuses(s, 1)

    rows = _aside(check)
    # Nothing mutated: the missing row is still there and no `new` file was promoted.
    assert ("missing", "vanished") in rows
    assert ("new", "fresh") in rows
    assert ("modified", "changed") in rows

    def open_events(s):
        from src.models.db import Event

        return s.scalar(
            select(func.count()).select_from(Event).where(Event.acknowledged_at.is_(None))
        )

    assert _aside(open_events) == 2


@pytest.mark.parametrize("url,scope", ROUTES)
@pytest.mark.parametrize("fp", [None, "", "   "])
def test_accept_fails_closed_without_a_fingerprint(cairn_env, url, scope, fp):
    root = cairn_env / "closed"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("changed", "modified", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        _assert_refused(_post(client, url, fp, _csrf(client)))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("modified", "changed") in rows


def test_baseline_accept_succeeds_on_an_unchanged_population(cairn_env):
    """The guard is not simply refusing everything."""
    root = cairn_env / "happy"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1").text
        m = re.search(r'name="population_fp" value="([0-9a-f]{64})"', body)
        assert m, "the detail page did not publish a fingerprint"
        r = _post(client, "/collection/1/accept", m.group(1), _csrf(client))
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1"

    assert _aside(lambda s: _statuses(s, 1)) == [("ok", "fresh")]


def test_review_accept_succeeds_on_an_unchanged_population(cairn_env):
    root = cairn_env / "happy2"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone", "missing", "complete"),
            ("changed", "modified", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        r = _post(
            client, "/collection/1/review/accept", _fingerprint(1, "review-accept"), _csrf(client)
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1/review"

    assert _aside(lambda s: _statuses(s, 1)) == [("ok", "changed")]


def test_review_accept_still_adopts_a_file_added_after_render(cairn_env):
    """The documented `new`-set exception (design D14): a growing collection is not refused."""
    root = cairn_env / "growing"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "complete")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fingerprint(1, "review-accept")
        # A scan adds a file; `added` events are born acknowledged, so the open-event count is
        # untouched and the guard does not fire.
        _aside(lambda s: _mk_files(s, 1, [("arrived", "new", "pending")]))
        r = _post(client, "/collection/1/review/accept", fp, _csrf(client))
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1/review"

    assert _aside(lambda s: _statuses(s, 1)) == [("ok", "arrived")]


def test_a_fingerprint_does_not_travel_between_collections_or_scopes(cairn_env):
    a, b = cairn_env / "ca", cairn_env / "cb"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone", "missing", "none")]))
        await _with_session(lambda s: _mk_files(s, cb, [("gone", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        other = _fingerprint(2, "review-accept")
        r = _post(client, "/collection/1/review/accept", other, token)
        _assert_refused(r)
        # A `baseline-new` fingerprint is refused by the review route (and vice versa).
        _assert_refused(
            _post(client, "/collection/1/review/accept", _fingerprint(1, "baseline-new"), token)
        )

    assert ("missing", "gone") in _aside(lambda s: _statuses(s, 1))


def test_accept_refuses_while_an_operation_is_in_flight(cairn_env):
    root = cairn_env / "inflight"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fingerprint(1, "review-accept")

        async def start_run(s):
            from src.models.db import Run

            # A genuinely in-flight run is one that is still HEARTBEATING: the operation gate now
            # reclaims a claim whose holder has stopped reporting progress (an orphan from a killed
            # process must not wedge the collection), and the module's fixed `NOW` is weeks stale.
            s.add(Run(
                collection_id=1, kind="scan", result="running",
                started=NOW, heartbeat_at=datetime.now(timezone.utc),
            ))
            await s.commit()

        _aside(start_run)
        _assert_refused(_post(client, "/collection/1/review/accept", fp, _csrf(client)))

    assert ("missing", "gone") in _aside(lambda s: _statuses(s, 1))


def test_a_rowid_reused_by_a_different_file_is_refused(cairn_env):
    root = cairn_env / "reuse"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "none", {"id": 7})]))

    with _make_client(cairn_env, seed) as client:
        fp = _fingerprint(1, "review-accept")

        async def swap(s):
            from src.models.db import FileEntry

            await s.delete(await s.get(FileEntry, 7))
            await s.commit()
            await _mk_files(s, 1, [("a-different-file", "missing", "none", {"id": 7})])

        _aside(swap)
        _assert_refused(_post(client, "/collection/1/review/accept", fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "a-different-file")]


def test_the_same_path_and_digest_on_a_reused_rowid_is_still_a_new_generation(cairn_env):
    """The case `id + relpath + status + sha256` alone cannot see: only `first_seen` separates it."""
    root = cairn_env / "generation"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("archive/a.pdf", "missing", "none", {"id": 17, "sha256": "d" * 64}),
        ]))

    with _make_client(cairn_env, seed) as client:
        fp = _fingerprint(1, "review-accept")

        async def regenerate(s):
            from src.models.db import FileEntry

            await s.delete(await s.get(FileEntry, 17))
            await s.commit()
            await _mk_files(s, 1, [(
                "archive/a.pdf", "missing", "none",
                {"id": 17, "sha256": "d" * 64, "first_seen": NOW + timedelta(days=3)},
            )])

        _aside(regenerate)
        _assert_refused(_post(client, "/collection/1/review/accept", fp, _csrf(client)))

    # The replacement generation survives — it was never on the page the operator saw.
    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "archive/a.pdf")]

    def first_seen(s):
        from src.models.db import FileEntry

        return s.scalar(select(FileEntry.first_seen).where(FileEntry.id == 17))

    assert _aside(first_seen).day == (NOW + timedelta(days=3)).day


def test_a_collection_recreated_on_the_same_id_is_refused(cairn_env):
    root = cairn_env / "recreated"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fingerprint(1, "review-accept")

        async def recreate(s):
            from src.models.db import Collection

            c = await s.get(Collection, 1)
            c.created_at = c.created_at + timedelta(seconds=5)
            await s.commit()

        _aside(recreate)
        _assert_refused(_post(client, "/collection/1/review/accept", fp, _csrf(client)))

    assert ("missing", "gone") in _aside(lambda s: _statuses(s, 1))


@pytest.mark.parametrize("url,scope", ROUTES)
def test_an_aba_on_the_event_population_is_refused(cairn_env, url, scope):
    """The protected file set returns to exactly its rendered value while an alert stays open."""
    root = cairn_env / "aba"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("wobble", "ok", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(1, scope)

        async def wobble(s):
            from src.models.db import FileEntry

            fe = await s.scalar(select(FileEntry).where(FileEntry.relpath == "wobble"))
            fe.status = "modified"
            await s.commit()
            await _mk_event(s, 1, fe.id, "modified")  # stays open by design (D9)
            fe.status = "missing"
            await s.commit()
            fe.status = "ok"  # restored: the file set is back to what the page showed
            await s.commit()

        _aside(wobble)
        _assert_refused(_post(client, url, fp, token))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("ok", "wobble") in rows

    def unacked(s):
        from src.models.db import Event

        return s.scalar(
            select(func.count()).select_from(Event).where(Event.acknowledged_at.is_(None))
        )

    assert _aside(unacked) == 1  # the modified alert was NOT swept up


# --- D14 (post-audit): the equal-count event ABA ---------------------------------------------


def test_an_equal_count_event_swap_is_refused(cairn_env):
    """The ABA a *count* cannot see: one open alert replaced by a different one.

    The first encoding hashed `open_events=<k>`. Acknowledge the one open `missing` event and open
    another on the same file with the same kind, and `k` is 1 both times while the incident the
    operator is being asked to clear is a different one — a stale form would validate and silently
    close an alert that was never on the page. Bound by identity + generation, it is refused.
    """
    root = cairn_env / "eventaba"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "complete")]))

        async def first_event(s):
            from src.models.db import FileEntry

            fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "gone"))
            await _mk_event(s, cid, fid, "missing")

        await _with_session(first_event)

    def open_count(s):
        from src.models.db import Event

        return s.scalar(
            select(func.count()).select_from(Event).where(Event.acknowledged_at.is_(None))
        )

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(1, "review-accept")
        assert _aside(open_count) == 1

        async def swap(s):
            """Ack E1, open E2 on the same file with the same kind. File rows never move."""
            from src.models.db import Event, FileEntry

            e1 = await s.scalar(select(Event).where(Event.acknowledged_at.is_(None)))
            e1.acknowledged_at = NOW
            await s.commit()
            fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "gone"))
            await _mk_event(s, 1, fid, "missing")

        _aside(swap)
        # The count the old encoding hashed is unchanged — this is a true equal-count ABA.
        assert _aside(open_count) == 1
        _assert_refused(_post(client, "/collection/1/review/accept", fp, token))

    # Nothing was accepted and the second, unseen incident is still open.
    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "gone")]
    assert _aside(open_count) == 1


# --- D14 (post-audit): the render and the mint come from ONE fetch ---------------------------


def _drift_after_the_population_read(monkeypatch, drift):
    """Patch `_read_population` into a barrier: it does the real read, then lets `drift` commit.

    This is the interleaving a two-SELECT page cannot survive — `SELECT` does not open a
    transaction under Python's legacy sqlite3 transaction control, so a scanner committing here
    would land in a *second* read but not the first. Returns the recorder: one entry per call,
    each `(scope, fingerprint_of_the_snapshot_that_was_returned)`.
    """
    from src.control_panel import routes

    seen: list[tuple[str, str]] = []
    real = routes._read_population

    async def spy(session, collection, scope):
        pop = await real(session, collection, scope)
        seen.append((scope, routes._population_fingerprint(collection, pop)))
        if len(seen) == 1:
            await _with_session(drift)
        return pop

    monkeypatch.setattr(routes, "_read_population", spy)
    return seen


def test_the_review_page_hashes_exactly_the_population_it_rendered(cairn_env, monkeypatch):
    """One fetch feeds the rows, the counts and the fingerprint — with a scan landing right after.

    The failure this pins: rows read in one statement and the fingerprint minted in another, with a
    scan committing in between. The form would then carry the *new* population's fingerprint while
    the page listed the old one, and an unchanged POST would validate and delete a `missing` row
    the operator never saw.
    """
    root = cairn_env / "mint"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("seen", "missing", "complete")]))

        async def alarm(s):
            from src.models.db import FileEntry

            fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "seen"))
            await _mk_event(s, cid, fid, "missing")

        await _with_session(alarm)

    async def scan_lands(s):
        from src.models.db import FileEntry

        await _mk_files(s, 1, [("unseen", "missing", "complete")])
        fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "unseen"))
        await _mk_event(s, 1, fid, "missing")

    with _make_client(cairn_env, seed) as client:
        seen = _drift_after_the_population_read(monkeypatch, scan_lands)
        with _capture_context() as captured:
            body = client.get("/collection/1/review").text

        # Exactly one population read served the whole render.
        assert [scope for scope, _ in seen] == ["review-accept"]
        ctx = [c for name, c in captured if name == "collection_review.html"][0]
        # ...and the published fingerprint is that snapshot's, not a re-read one.
        assert ctx["population_fp"] == seen[0][1]
        assert f'name="population_fp" value="{seen[0][1]}"' in body
        # The row that landed after the single fetch is in neither half: not rendered, not hashed.
        assert "seen" in body and "unseen" not in body
        assert ctx["total_issues"] == 1 and ctx["shown"] == 1
        # The open-event component is read in the same statement, so the alert that opened after
        # it is outside the pill and outside the hash too.
        assert ctx["review_open"] == 1

        # And the drift is caught where it must be: at submit time, under the write lock.
        _assert_refused(_post(client, "/collection/1/review/accept", ctx["population_fp"], _csrf(client)))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("missing", "seen") in rows and ("missing", "unseen") in rows


def test_the_detail_baseline_form_hashes_the_population_that_gated_it(cairn_env, monkeypatch):
    """Same contract on the detail page: the D7 gate and the D14 mint share one read."""
    root = cairn_env / "mintdetail"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))

    async def issue_lands(s):
        await _mk_files(s, 1, [("gone", "missing", "complete")])

    with _make_client(cairn_env, seed) as client:
        seen = _drift_after_the_population_read(monkeypatch, issue_lands)
        with _capture_context() as captured:
            body = client.get("/collection/1").text

        assert [scope for scope, _ in seen] == ["baseline-new"]
        ctx = [c for name, c in captured if name == "collection_detail.html"][0]
        assert ctx["show_baseline"] is True
        assert ctx["population_fp"] == seen[0][1]
        assert f'name="population_fp" value="{seen[0][1]}"' in body

        # The issue that landed after that read is not in the hash, so the POST's own read (which
        # sees it) refuses — the zero-issue assertion the form made no longer holds.
        _assert_refused(_post(client, "/collection/1/accept", ctx["population_fp"], _csrf(client)))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("missing", "gone") in rows


# --- D14 / 3.19: lock contention is a refusal, not a 500 -------------------------------------


@pytest.fixture
def fast_busy_timeout(monkeypatch):
    """Shorten SQLite's busy timeout so the contention test does not wait five seconds."""
    from src import database

    def _configure(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=150")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    monkeypatch.setattr(database, "_configure_sqlite", _configure)


@pytest.mark.parametrize("url,scope", ROUTES)
def test_writer_lock_contention_refuses_instead_of_500ing(cairn_env, fast_busy_timeout, url, scope):
    root = cairn_env / "busy"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("gone", "missing", "complete"),
        ]))

    db_path = str(cairn_env / "db" / "cairn.db")
    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(1, scope)
        holder = sqlite3.connect(db_path, timeout=1)
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN IMMEDIATE")  # hold the writer lock
            holder.execute("UPDATE collections SET name = name WHERE id = 1")
            _assert_refused(_post(client, url, fp, token))
        finally:
            holder.rollback()
            holder.close()

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("missing", "gone") in rows


def test_a_non_lock_operational_error_is_not_reported_as_staleness(cairn_env, monkeypatch):
    root = cairn_env / "broken"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(1, "review-accept")

        from src.control_panel import routes

        async def boom(session, collection_id):
            raise OperationalError(
                "UPDATE collections", {}, sqlite3.OperationalError("disk I/O error")
            )

        monkeypatch.setattr(routes, "_take_write_lock", boom)
        with pytest.raises(OperationalError):
            _post(client, "/collection/1/review/accept", fp, token)

    assert ("missing", "gone") in _aside(lambda s: _statuses(s, 1))


# --- #21 coverage completion: a changed restore is never "All clear" --------------------------


async def _scan_a_changed_restore(root, *, mode: str) -> int:
    """Drive the REAL scanner through present → absent → back-with-different-bytes."""
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    root.mkdir(parents=True, exist_ok=True)
    (root / "deed.pdf").write_text("the real deed")
    # `ots_mode="none"`: the status pill is the subject, and `perfile` would send the post-scan
    # stamp pass at a calendar.
    cid = await seed_collection(root, ots_mode="none", mode=mode)

    async def scan():
        async with get_sessionmaker()() as s:
            return await scan_collection(s, await s.get(Collection, cid))

    await scan()
    (root / "deed.pdf").unlink()
    await scan()
    (root / "deed.pdf").write_text("a different document entirely")
    summary = await scan()
    assert summary.restored_changed == 1, "the fixture did not produce a changed restore"
    return cid


@pytest.mark.parametrize("mode", ["worm", "churn"])
def test_a_changed_restore_keeps_its_collection_off_all_clear(cairn_env, mode):
    """An unresolved changed restore reads "Attention" on every surface, in BOTH modes.

    `_collection_status` derives the pill from `files.status` alone, and churn is the case worth
    pinning: an ordinary churn edit re-baselines to `ok` silently, so if the changed-restore row
    were ever handled like one, the collection would go on reading green while an unacknowledged
    `restored_changed` event sat under it — the alarm raised and the operator never shown it.
    """
    root = cairn_env / f"{mode}-restore"
    observed: dict = {}

    async def seed():
        from src.control_panel.routes import _collection_counts, _collection_status
        from src.database import get_sessionmaker

        cid = await _scan_a_changed_restore(root, mode=mode)
        async with get_sessionmaker()() as s:
            counts = await _collection_counts(s, cid)
        observed["counts"] = counts
        observed["status"] = _collection_status(counts)

    with _make_client(cairn_env, seed) as client:
        dash = client.get("/").text
        detail = client.get("/collection/1").text
        frag = client.get("/collection/1/op-status").text

    assert observed["counts"]["modified"] == 1, "the restored row is still on record as changed"
    assert observed["status"] == "attention", (
        f"{mode}: a file that came back with different bytes leaves the collection needing a look"
    )
    for name, body in (("dashboard", dash), ("detail", detail), ("op-status", frag)):
        assert "All clear" not in body, name
        assert "Attention" in body, name
