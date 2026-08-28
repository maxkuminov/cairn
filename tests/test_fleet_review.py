"""The fleet-wide review page, the tile's destination and the feed ordering (#27 / #18 / #25).

Slice A of `add-fleet-review-and-run-health`. Three surfaces, one theme — *the panel computes a
fact and must show the operator that fact*:

* **`GET /review`** (#27) — every missing or changed file across the viewer's collections, grouped
  by collection. A read surface with per-row verbs: no collection-spanning bulk action exists, and
  the tests assert its absence, because a fleet-wide accept would be the unscoped irreversible verb
  the scoped ones replaced (design D3). Everything a group shows OR authorizes comes out of ONE
  `_read_population` per collection, the row's open-event state included (design D1).
* **The "Open issues" tile** (#18's leftover) — several affected collections now land on `/review`
  instead of the `/collections` placeholder that acted on nothing.
* **The event feed** (#25) — ordered unreviewed-first, never *filtered* (that is #12's rejected
  fix 2: the informational kinds are born acknowledged, so an open-only feed empties on a healthy
  system).

Plus the dashboard's last-activity tile (design D14), which described every run of every kind and
every result as a clean scan.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_fleet_review.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.conftest import seed_collection  # noqa: F401  (cairn_env comes from conftest)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


# --- harness --------------------------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    """Run an async seed on a throwaway loop, drop the engine, return a TestClient."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warnings)
    return TestClient(app)


async def _with_session(fn):
    """Await ``fn(session)`` against a throwaway engine, disposed on the current loop.

    Deliberately not the app's cached engine: several of these calls interleave with a live
    ``TestClient`` — they play the part of a scan or another tab committing between a render and a
    submit — so they must not disturb the engine the client's loop is using.
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
    return asyncio.run(_with_session(fn))


def _csrf(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


@contextlib.contextmanager
def _capture_context():
    """Record the template context every route render publishes."""
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
    """Insert ``FileEntry`` rows from ``(relpath, status)`` tuples (+ optional overrides)."""
    from src.models.db import FileEntry

    out = []
    for spec in specs:
        relpath, status = spec[0], spec[1]
        extra = spec[2] if len(spec) > 2 else {}
        fe = FileEntry(
            collection_id=cid,
            relpath=relpath,
            size=extra.get("size", 10),
            sha256=extra.get("sha256", "a" * 64),
            status=status,
            ots_state=extra.get("ots_state", "complete"),
            first_seen=NOW,
            last_checked=NOW,
            last_changed=NOW,
        )
        session.add(fe)
        out.append(fe)
    await session.commit()
    return [fe.id for fe in out]


async def _mk_event(session, cid, file_id, kind, *, acked=False, at=None):
    from src.models.db import Event

    e = Event(
        collection_id=cid,
        file_id=file_id,
        kind=kind,
        detected_at=at or NOW,
        acknowledged_at=NOW if acked else None,
    )
    session.add(e)
    await session.commit()
    return e.id


async def _mk_run(session, cid, *, kind="scan", result="ok", moved=0, finished=None):
    from src.models.db import Run

    r = Run(
        collection_id=cid,
        kind=kind,
        result=result,
        moved=moved,
        started=NOW,
        finished=finished or NOW,
    )
    session.add(r)
    await session.commit()
    return r.id


def _row_fp(collection_id: int, file_id: int) -> str:
    """The per-row fingerprint a review surface publishes, through the production helpers."""

    async def _go(s):
        from src.control_panel.routes import (
            _FP_FORMS,
            _narrow,
            _population_fingerprint,
            _read_population,
        )
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        pop = await _read_population(s, collection, "review")
        return _population_fingerprint(
            collection, _narrow(pop, "accept-file", _FP_FORMS["accept-file"][1], file_id=file_id)
        )

    return _aside(_go)


def _statuses(cid: int):
    async def _go(s):
        from src.models.db import FileEntry

        return sorted(
            (r, p)
            for r, p in (
                await s.execute(
                    select(FileEntry.relpath, FileEntry.status).where(
                        FileEntry.collection_id == cid
                    )
                )
            ).all()
        )

    return _aside(_go)


def _open_events() -> int:
    async def _go(s):
        from src.models.db import Event

        return await s.scalar(
            select(func.count()).select_from(Event).where(Event.acknowledged_at.is_(None))
        )

    return int(_aside(_go) or 0)


def _groups(captured) -> list[dict]:
    ctxs = [c for name, c in captured if name == "review_fleet.html"]
    assert len(ctxs) == 1, f"expected one fleet render, got {len(ctxs)}"
    return ctxs[0]["groups"]


# --- 2.14 / 2.5: the page lists every affected collection, missing first ----------------------


def test_fleet_page_groups_every_affected_collection_missing_first(cairn_env):
    """#27's whole point: two collections in trouble, one page that lists both and can act."""
    a, b = cairn_env / "alpha", cairn_env / "beta"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        ids = await _with_session(lambda s: _mk_files(s, ca, [
            ("z/changed.txt", "modified"),
            ("a/gone.txt", "missing"),
            ("fine.txt", "ok"),
        ]))
        await _with_session(lambda s: _mk_event(s, ca, ids[1], "missing"))
        await _with_session(lambda s: _mk_files(s, cb, [("b/edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        groups = _groups(captured)

    assert [g["name"] for g in groups] == ["alpha", "beta"]  # worst first: missing DESC
    assert (groups[0]["missing"], groups[0]["modified"]) == (1, 1)
    assert (groups[1]["missing"], groups[1]["modified"]) == (0, 1)
    # Missing before modified within a group, regardless of path order.
    assert [it["relpath"] for it in groups[0]["items"]] == ["a/gone.txt", "z/changed.txt"]
    # The `ok` file is not an issue and is not listed anywhere.
    assert "fine.txt" not in body
    # Every row carries a real, per-row fingerprint — the control fails closed without one.
    for g in groups:
        for it in g["items"]:
            assert re.fullmatch(r"[0-9a-f]{64}", it["fp"])
            assert f'name="population_fp" value="{it["fp"]}"' in body
    # Each group names its collection and links into that collection's own review page, where the
    # scoped bulk verbs live.
    assert 'href="/collection/1/review"' in body and 'href="/collection/2/review"' in body
    assert "Review all in alpha" in body and "Review all in beta" in body
    # BOTH counts in every group header, including a zero (the requirement's literal reading):
    # "1 missing" alone leaves the reader to infer whether the other count is zero or unmentioned,
    # and the two kinds carry different consequences.
    assert body.count("missing</span>") == 2 and body.count("modified</span>") == 2
    assert "0 missing" in body  # beta has no missing files and says so
    # Per-group unreviewed pill, id-scoped so the OOB swap cannot hit another group (design D10).
    assert 'id="review-open-pill-1"' in body and 'id="review-open-pill-2"' in body
    # The accept forms post from the fleet surface, so a refusal comes back here.
    assert 'action="/collection/1/file/2/accept?from=fleet"' in body


def test_the_fleet_page_lights_no_other_nav_item(cairn_env):
    """Task 2.7: `/review` is reached from the tile, the badge and the cards — it has no nav entry,
    and it must not make another one claim to be the current page."""
    root = cairn_env / "nav"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = [c for n, c in captured if n == "review_fleet.html"][0]

    assert ctx["page"] == "review"
    assert "is-active" not in body


def test_unaffected_collections_are_not_listed(cairn_env):
    a, b = cairn_env / "sick", cairn_env / "healthy"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("kept.txt", "ok"), ("n.txt", "new")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            client.get("/review")
        groups = _groups(captured)

    assert [g["name"] for g in groups] == ["sick"]


# --- 2.15: no collection-spanning bulk verb ---------------------------------------------------


def test_fleet_page_offers_no_collection_spanning_bulk_verb(cairn_env):
    """Design D3: triage is fleet-wide, the irreversible verbs stay collection-scoped.

    A fleet bulk would need a cross-collection fingerprint scope and a confirm reading "permanently
    remove N file records across M collections" — #12 R2's unscoped accept rebuilt one level up, on
    the page whose purpose is making counts land somewhere honest.
    """
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        ids = await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, ca, ids[0], "missing"))
        await _with_session(lambda s: _mk_files(s, cb, [("edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/review").text

    for verb in ("/review/ack-all", "/review/adopt-changed", "/review/stop-tracking"):
        assert verb not in body, f"a bulk verb leaked onto the fleet page: {verb}"
    # The dashboard's fleet-wide acknowledge is not borrowed onto this page either.
    assert "/events/ack-all" not in body
    # ...and no accept form posts anywhere but a single named file. Only the POSTing forms are in
    # scope: the chrome's top-bar search is a GET navigation to /verify (sprint 2, #36), not a verb.
    posts = [
        tag for tag in re.findall(r"<form[^>]*>", body) if 'method="get"' not in tag
    ]
    assert posts, "expected the per-row accept forms to be rendered"
    for tag in posts:
        action = re.search(r'action="([^"]+)"', tag).group(1)
        assert re.fullmatch(r"/collection/\d+/file/\d+/accept\?from=fleet", action), action


# --- 2.16 / 2.9: the per-row accept, and where it lands ---------------------------------------


def _accept(client, url, fp, token):
    return client.post(
        url,
        data={"population_fp": fp},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def test_fleet_row_accept_lands_back_on_the_fleet_page(cairn_env):
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", _row_fp(1, 1), token)
        assert r.status_code == 303
        assert r.headers["location"] == "/review"

    # Exactly one file acted on: the missing record is gone, the other collection is untouched.
    assert _statuses(1) == []
    assert _statuses(2) == [("edited.txt", "modified")]


def test_a_stale_fleet_row_accept_refuses_and_returns_to_the_fleet_page(cairn_env):
    """The refusal must land where the operator was, or the fix for one dead end makes another."""
    root = cairn_env / "drift"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _row_fp(1, 1)
        # A scan commits between render and submit, raising a NEW alert on this very row — drift
        # inside the population this per-file form was minted over, so the guard must refuse it.
        _aside(lambda s: _mk_event(s, 1, 1, "missing"))
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", fp, token)
        assert r.status_code == 303
        assert r.headers["location"] == "/review?stale=1"
        # The banner is rendered on the fleet page, not only on the collection one.
        assert "<strong>Your action was NOT applied</strong>" in client.get("/review?stale=1").text

    assert _statuses(1) == [("gone.txt", "missing")]  # refusals mutate nothing
    assert _open_events() == 1


# --- 2.24: the return destination is a whitelist, never the supplied value --------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "?from=https://evil.example/x",
        "?from=//evil.example",
        "?from=/collection/99/review",
        "?from=",
        "?from=FLEET",  # the whitelist is exact, not case-folded
        "?from=fleet%20",
        "",  # no parameter at all
    ],
)
def test_a_hostile_from_value_never_reaches_the_redirect(cairn_env, hostile):
    """`from` carries one bit and selects a destination from a fixed pair (design D2).

    No part of the supplied string may reach the ``Location`` header, and the value must not change
    what the POST *does*: the same scoped, fingerprint-guarded, single-file accept runs either way.
    """
    root = cairn_env / "hostile"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone.txt", "missing"),
            ("stays.txt", "missing"),
        ]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        r = _accept(client, f"/collection/1/file/1/accept{hostile}", _row_fp(1, 1), token)
        assert r.status_code == 303
        # The route constant, byte-for-byte — not the supplied value, and never off-host.
        assert r.headers["location"] == "/collection/1/review"

    # ...and exactly the same single-file accept was performed.
    assert _statuses(1) == [("stays.txt", "missing")]


def test_a_hostile_from_value_is_ignored_on_the_refusal_path_too(cairn_env):
    """The refusal redirect is the other half of the same whitelist."""
    root = cairn_env / "hostilestale"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        r = _accept(client, "/collection/1/file/1/accept?from=//evil.example", "", token)
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1/review?stale=1"

    assert _statuses(1) == [("gone.txt", "missing")]  # fail-closed: nothing mutated


# --- 2.17 / 2.10 / 2.11: acknowledging from the fleet view ------------------------------------


def test_ack_from_the_fleet_view_swaps_the_row_and_refreshes_that_groups_pill(cairn_env):
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        ids = await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, ca, ids[0], "missing"))
        other = await _with_session(lambda s: _mk_files(s, cb, [("edited.txt", "modified")]))
        await _with_session(lambda s: _mk_event(s, cb, other[0], "modified"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        with _capture_context() as captured:
            client.get("/review")
        before = _groups(captured)
        event_id = before[0]["items"][0]["event_id"]
        assert event_id and before[0]["items"][0]["acked"] is False

        r = client.post(
            f"/events/{event_id}/ack?view=fleet", headers={"X-CSRF-Token": token}
        )
        assert r.status_code == 200
        # The row comes back, reviewed, with its accept control still on it and still bound to the
        # fleet surface.
        assert 'id="review-row-1"' in r.text
        assert "Reviewed" in r.text
        assert 'action="/collection/1/file/1/accept?from=fleet"' in r.text
        # ...and the OOB swaps: THIS group's pill (never a bare `review-open-pill`, which would
        # refresh whichever group rendered first) plus the sidebar badge (#12's rejected fix 8).
        assert 'id="review-open-pill-1" hx-swap-oob="innerHTML"' in r.text
        assert 'id="review-open-pill" hx-swap-oob' not in r.text
        assert 'id="sidebar-alert-badge" hx-swap-oob="innerHTML"' in r.text
        assert "All reviewed" in r.text  # this collection's own count, not the fleet's

        with _capture_context() as captured:
            client.get("/review")
        after = _groups(captured)

    # Acknowledgement is a reading-log write: the file-derived counts are correct to stay put.
    assert [(g["missing"], g["modified"], g["issues"]) for g in after] == [
        (g["missing"], g["modified"], g["issues"]) for g in before
    ]
    assert after[0]["review_open"] == 0 and after[1]["review_open"] == 1


def test_the_collection_review_ack_response_is_unchanged(cairn_env):
    """`pill_id` defaults, so the collection page's rendered markup is byte-for-byte what it was."""
    root = cairn_env / "unchanged"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "missing"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        page = client.get("/collection/1/review").text
        # The collection page's own row still posts without a `from`/`view=fleet` marker.
        assert 'action="/collection/1/file/1/accept"' in page
        assert "?view=review" in page and "?from=fleet" not in page

        r = client.post("/events/1/ack?view=review", headers={"X-CSRF-Token": token})
        assert 'id="review-open-pill" hx-swap-oob="innerHTML"' in r.text
        assert "review-open-pill-1" not in r.text


# --- 2.18: the feed leads with what the pill counts (#25) -------------------------------------


def test_feed_leads_with_unreviewed_events_without_filtering_the_log(cairn_env):
    """Order, never a WHERE (design D4 / #12's rejected fix 2).

    The live pass caught the sharp end: "Mark all 8 reviewed" offered above a feed showing none of
    those 8, every one pushed off the bottom by newer born-acknowledged informational rows.
    """
    root = cairn_env / "feed"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [
            ("gone1.txt", "missing"),
            ("gone2.txt", "missing"),
            ("gone3.txt", "missing"),
        ]))

        async def events(s):
            # Three OLD unacknowledged alarms...
            for fid in ids:
                await _mk_event(s, cid, fid, "missing", at=NOW - timedelta(days=5))
            # ...buried under 25 NEWER born-acknowledged informational rows.
            for i in range(25):
                await _mk_event(
                    s, cid, None, "added", acked=True, at=NOW - timedelta(minutes=25 - i)
                )

        await _with_session(events)

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            client.get("/")
        ctx = [c for name, c in captured if name == "dashboard.html"][0]

    feed = ctx["events"]
    assert len(feed) == 20  # the cap is unchanged
    assert ctx["open_events"] == 3  # a real COUNT over the whole population, not the 20 rows
    # All three of the events the bulk verb names are on the page, at the top.
    assert [e["acked"] for e in feed[:3]] == [False, False, False]
    assert {e["relpath"] for e in feed[:3]} == {"gone1.txt", "gone2.txt", "gone3.txt"}
    # And the activity log survives beneath them — the feed is not filtered to open events.
    assert all(e["acked"] for e in feed[3:])
    assert len(feed[3:]) == 17


def test_a_fully_acknowledged_feed_still_renders_its_history(cairn_env):
    """An open-only default would render "No events recorded yet." on a healthy system."""
    root = cairn_env / "quiet"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)

        async def events(s):
            for i in range(4):
                await _mk_event(
                    s, cid, None, "added", acked=True, at=NOW - timedelta(minutes=4 - i)
                )

        await _with_session(events)

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/").text
        ctx = [c for name, c in captured if name == "dashboard.html"][0]

    assert ctx["open_events"] == 0
    assert len(ctx["events"]) == 4
    # Newest-first within the acknowledged group, as before.
    assert [e["acked"] for e in ctx["events"]] == [True] * 4
    assert "No events recorded yet" not in body


# --- 2.19: the tile's destination -------------------------------------------------------------


def test_open_issues_tile_destination_by_affected_collection_count(cairn_env):
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        # One affected collection -> straight to its own review page (where the bulk verbs are).
        with _capture_context() as captured:
            one = client.get("/").text
        assert [c for n, c in captured if n == "dashboard.html"][0]["tiles"][
            "issues_href"
        ] == "/collection/1/review"
        assert '<a class="card tile tile--link" href="/collection/1/review">' in one

        # A second affected collection -> the fleet page, which lists every file the tile counts.
        _aside(lambda s: _mk_files(s, 2, [("edited.txt", "modified")]))
        with _capture_context() as captured:
            two = client.get("/").text
        assert [c for n, c in captured if n == "dashboard.html"][0]["tiles"][
            "issues_href"
        ] == "/review"
        assert '<a class="card tile tile--link" href="/review">' in two


def test_open_issues_tile_is_inert_at_zero(cairn_env):
    root = cairn_env / "clean"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/").text

    assert [c for n, c in captured if n == "dashboard.html"][0]["tiles"]["issues_href"] is None
    assert "tile--link" not in body


# --- 2.20: scoping ----------------------------------------------------------------------------


def test_fleet_page_is_scoped_to_the_viewers_collections(cairn_env, monkeypatch):
    """In `multi` mode the page shows the viewer's own collections and nothing else.

    Built from `list_collections(user_id=…)`, so a collection the viewer does not own is never in
    the list — no `_get_owned_collection` 404 path is needed, and none of another user's file paths
    can reach the render.
    """
    from src.config import get_settings

    mine, theirs = cairn_env / "mine", cairn_env / "theirs"
    mine.mkdir()
    theirs.mkdir()

    async def seed():
        cid = await seed_collection(mine)
        await _with_session(lambda s: _mk_files(s, cid, [("my-secret.txt", "missing")]))

        async def other(s):
            from src.models.db import User
            from src.services.collections import create_collection

            u = User(username="bob", is_admin=False)
            s.add(u)
            await s.commit()
            c = await create_collection(
                s, user_id=u.id, name="Bobs Files", root=str(theirs), ots_mode="none"
            )
            await _mk_files(s, c.id, [("bobs-secret.txt", "missing")])

        await _with_session(other)

    with _make_client(cairn_env, seed) as client:
        monkeypatch.setenv("CAIRN_AUTH_MODE", "multi")
        monkeypatch.setenv("CAIRN_SECRET_KEY", "0" * 64)
        get_settings.cache_clear()
        try:
            with _capture_context() as captured:
                body = client.get("/review").text
            groups = _groups(captured)
        finally:
            get_settings.cache_clear()

    assert [g["name"] for g in groups] == ["mine"]
    assert "my-secret.txt" in body
    assert "Bobs Files" not in body and "bobs-secret.txt" not in body


# --- 2.21 / 2.22: the two caps (design D11) ---------------------------------------------------


def test_one_very_large_collection_does_not_crowd_out_the_others(cairn_env):
    """The per-group cap, with a "+N more" note whose N is the SNAPSHOT remainder."""
    from src.control_panel.routes import FLEET_COLLECTION_ROW_LIMIT as CAP

    big, small = cairn_env / "big", cairn_env / "small"
    big.mkdir()
    small.mkdir()
    over = CAP + 7

    async def seed():
        cb = await seed_collection(big)
        cs = await seed_collection(small)
        await _with_session(lambda s: _mk_files(
            s, cb, [(f"gone/{i:05d}.jpg", "missing") for i in range(over)]
        ))
        await _with_session(lambda s: _mk_files(s, cs, [("edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        groups = _groups(captured)

    assert [g["name"] for g in groups] == ["big", "small"]
    assert groups[0]["shown"] == CAP
    assert len(groups[0]["items"]) == CAP
    # The counts and the remainder come from the population, never from the render list.
    assert groups[0]["missing"] == over and groups[0]["issues"] == over
    assert groups[0]["more"] == over - CAP
    assert f"Showing the first {CAP} of {over} in big — {over - CAP} more." in body
    # The other affected collection still renders its rows, which is the whole point of the cap.
    assert groups[1]["shown"] == 1
    assert "edited.txt" in body
    assert "Review all in big" in body  # the way to the full set


def test_a_collection_past_the_total_budget_is_still_listed_with_its_counts(cairn_env):
    """A collection must never disappear from a fleet-wide issue list because of a render budget."""
    from src.control_panel.routes import (
        FLEET_COLLECTION_ROW_LIMIT as CAP,
        FLEET_ROW_LIMIT as TOTAL,
    )

    n_full = TOTAL // CAP  # collections that will exhaust the budget
    roots = []
    for i in range(n_full + 1):
        r = cairn_env / f"c{i}"
        r.mkdir()
        roots.append(r)

    async def seed():
        for i, r in enumerate(roots):
            cid = await seed_collection(r)
            await _with_session(lambda s, c=cid: _mk_files(
                s, c, [(f"gone/{j:05d}.txt", "missing") for j in range(CAP)]
            ))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        groups = _groups(captured)

    assert len(groups) == n_full + 1
    assert sum(g["shown"] for g in groups) <= TOTAL
    last = groups[-1]
    assert last["shown"] == 0 and last["items"] == []
    # ...but it is still on the page, with its counts and its way in.
    assert last["missing"] == CAP and last["issues"] == CAP
    assert last["more"] == CAP
    assert f"Review all in {last['name']}" in body
    assert f'href="/collection/{last["id"]}/review"' in body
    # A group with ZERO rows is not "showing the first 0" — that describes a listing, and this is a
    # listing that could not start. It says what happened and points at the page that can show them.
    assert "Budget spent" in body
    assert f"Open {last['name']}'s own review" in body
    assert f"Showing the first 0 of {CAP}" not in body


# --- 2.23: the two empty states ---------------------------------------------------------------


def test_empty_state_with_collections_but_no_issues(cairn_env):
    root = cairn_env / "allclear"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/review").text

    assert "No open issues across your collections" in body
    # Nothing needs adding — that is the *other* empty state, and offering it here misreads a
    # healthy fleet as an unconfigured one.
    assert 'href="/collection/new"' not in body.split('class="empty-state"')[1]


def test_empty_state_with_no_collections_at_all(cairn_env):
    async def seed():
        from src.database import get_sessionmaker, ensure_implicit_user

        async with get_sessionmaker()() as s:
            await ensure_implicit_user(s)

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = [c for n, c in captured if n == "review_fleet.html"][0]

    assert ctx["has_collections"] is False
    assert "No collections yet" in body
    assert "No open issues across your collections" not in body
    assert 'href="/collection/new"' in body.split('class="empty-state"')[1]


# --- 2.25: one snapshot per group, rows and fingerprints alike --------------------------------


def test_the_fleet_page_never_reads_events_a_second_time(cairn_env, monkeypatch):
    """`_latest_events_by_file` is the collection page's second statement; this page takes none.

    With two reads, a scan insert or a concurrent acknowledgement between them lets the row's
    DISPLAYED reviewed state and the fingerprint its accept control AUTHORIZES describe two
    different states of the same file — the guard's own failure mode, reintroduced beside the guard.
    """
    from src.control_panel import routes

    root = cairn_env / "onesnap"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "missing"))

    async def _boom(*a, **kw):  # pragma: no cover - the assertion is that it is never called
        raise AssertionError("fleet_review must not take a second events read")

    with _make_client(cairn_env, seed) as client:
        monkeypatch.setattr(routes, "_latest_events_by_file", _boom)
        with _capture_context() as captured:
            client.get("/review")
        item = _groups(captured)[0]["items"][0]

    assert item["event_id"] == 1 and item["acked"] is False


def test_a_rows_displayed_state_and_its_fingerprint_move_together(cairn_env):
    """Both are slices of one `_read_population`, so they can never contradict each other.

    After the row's event is acknowledged the row renders as reviewed AND its fingerprint changes —
    and the fingerprint minted while the event was open is refused, which is what proves the two
    described the same snapshot.
    """
    root = cairn_env / "together"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "missing"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        with _capture_context() as captured:
            client.get("/review")
        before = _groups(captured)[0]["items"][0]
        assert before["acked"] is False and before["event_id"] == 1

        # Another session marks that alert reviewed.
        client.post("/events/1/ack?view=review", headers={"X-CSRF-Token": token})

        with _capture_context() as captured:
            client.get("/review")
        after = _groups(captured)[0]["items"][0]
        # The population read no longer carries the event: the row shows reviewed (no open event)
        # and its authorization moved with it.
        assert after["acked"] is True and after["event_id"] is None
        assert after["fp"] != before["fp"]

        # The pre-ack fingerprint is refused — it described a population that no longer exists.
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", before["fp"], token)
        assert r.status_code == 303 and r.headers["location"] == "/review?stale=1"
        # ...and the current one is accepted.
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", after["fp"], token)
        assert r.status_code == 303 and r.headers["location"] == "/review"

    assert _statuses(1) == []
    assert _open_events() == 0


# --- 2.26 / D14: the last-activity tile says what it actually found ---------------------------


def _last_activity_sub(client) -> str:
    with _capture_context() as captured:
        client.get("/")
    return [c for n, c in captured if n == "dashboard.html"][0]["tiles"]["last_activity_sub"]


@pytest.mark.parametrize(
    ("kind", "result", "expected"),
    [
        # A clean scan reads exactly as it did: no result suffix on the ordinary outcome.
        ("scan", "ok", "act scan"),
        # #29's defect in the one tile #29's task list did not name: a run that skipped files must
        # not render identically to one that checked everything.
        ("scan", "partial", "act scan · partial"),
        ("scan", "error", "act scan · failed"),
        # Neutral, never a fault (design D7): since the operation claim landed, `interrupted` is
        # what restarting the app mid-scan produces.
        ("scan", "interrupted", "act scan · interrupted"),
        # A stamp pass says nothing about when the files were last CHECKED, so it must not be
        # labelled "scan".
        ("stamp", "ok", "act stamp"),
        ("upgrade", "ok", "act upgrade"),
    ],
)
def test_last_activity_tile_names_the_run_it_found(cairn_env, kind, result, expected):
    root = cairn_env / "act"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_run(s, cid, kind=kind, result=result))

    with _make_client(cairn_env, seed) as client:
        assert _last_activity_sub(client) == expected


def test_last_activity_tile_keeps_the_moved_suffix(cairn_env):
    root = cairn_env / "act"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_run(s, cid, kind="scan", result="ok", moved=3))

    with _make_client(cairn_env, seed) as client:
        assert _last_activity_sub(client) == "act scan · 3 moved"


def test_last_activity_tile_reports_a_partial_run_with_its_moves(cairn_env):
    root = cairn_env / "act"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_run(s, cid, kind="scan", result="partial", moved=2))

    with _make_client(cairn_env, seed) as client:
        assert _last_activity_sub(client) == "act scan · partial · 2 moved"


def test_last_activity_tile_describes_the_newest_finished_run(cairn_env):
    """Generic by design (D14): narrowing it to scans would drop the activity it exists to show."""
    root = cairn_env / "act"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_run(
            s, cid, kind="scan", result="ok", finished=NOW - timedelta(hours=3)
        ))
        await _with_session(lambda s: _mk_run(
            s, cid, kind="upgrade", result="partial", finished=NOW - timedelta(minutes=5)
        ))

    with _make_client(cairn_env, seed) as client:
        assert _last_activity_sub(client) == "act upgrade · partial"
