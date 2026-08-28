"""UX audit sprint 1, slice B: the dashboard/collection surfaces and the accept-family guard.

Two halves:

* **Honesty of the tiles and links** (#18, #20, #31, #32) — the "Open issues" tile is a link that
  goes somewhere useful, the sidebar badge counts the same population as the tile beside it, no
  coverage claim is computed over a population that includes missing files, the two
  not-yet-confirmed proof states are never summed, and a collection watching nothing never reads
  green.
* **The D14 population fingerprint** (#14, extended by #16/#30) — each of the four scoped accept
  forms (`baseline-new`, `adopt-changed`, `stop-tracking`, `accept-file`) acts only on the
  population its own form was rendered for, and refuses (with no mutation at all) on any drift
  *within that population*, including the cases a naive `id + status` preimage cannot see: SQLite
  rowid reuse, a row deleted and re-created at the same path with the same bytes, and an ABA on the
  open-event set. Equally load-bearing in the other direction: a scoped verb is NOT refused by
  drift it cannot reach, because refusals an operator cannot account for are how a guard gets
  worked around. `baseline-new` is the deliberate exception — every open event on the collection,
  detached ones included, refuses it.

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


def _fingerprint(collection_id: int, form: str, file_id: int | None = None) -> str:
    """The fingerprint a page would publish for ``form``, through the production helpers.

    Minted the way the MINT side mints it: the page's one wide read, narrowed purely to the form's
    own population. Passing the read scope straight to the encoder — as this helper used to — would
    hash a population no form is ever minted for.
    """

    async def _go(s):
        from src.control_panel.routes import (
            _FP_FORMS,
            _narrow,
            _population_fingerprint,
            _read_population,
        )
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        read_scope, statuses = _FP_FORMS[form]
        pop = await _read_population(s, collection, read_scope)
        return _population_fingerprint(collection, _narrow(pop, form, statuses, file_id=file_id))

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


def test_review_route_publishes_one_fingerprint_per_form_and_the_stale_flag(cairn_env):
    """One snapshot, three kinds of fingerprint: the two bulk forms and one per rendered row."""
    root = cairn_env / "ctx"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("gone", "missing", "none"),
            ("changed", "modified", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            plain = client.get("/collection/1/review").text
            client.get("/collection/1/review?stale=1")
            client.get("/collection/1/review?stale=yes")

    ctxs = [c for name, c in captured if name == "collection_review.html"]
    assert len(ctxs) == 3
    for c in ctxs:
        assert re.fullmatch(r"[0-9a-f]{64}", c["adopt_fp"])
        assert re.fullmatch(r"[0-9a-f]{64}", c["stop_fp"])
        # Two verbs, two populations, two fingerprints: neither can validate the other's endpoint.
        assert c["adopt_fp"] != c["stop_fp"]
        # The button labels' numbers come from the same snapshot as the rows (design D9).
        assert c["adopt_count"] == 1 and c["stop_count"] == 1
        assert len(c["items"]) == 2
        for it in c["items"]:
            assert re.fullmatch(r"[0-9a-f]{64}", it["fp"])
        assert len({it["fp"] for it in c["items"]}) == 2  # each row's form is its own
    # The retired accept-all key is gone with its route and its button: a live fingerprint with no
    # form to mint it is exactly what this change removed.
    assert "population_fp" not in ctxs[0]
    assert ctxs[0]["stale"] is False
    assert ctxs[1]["stale"] is True
    assert ctxs[2]["stale"] is False  # only `1` is recognized, exactly as view/filter are

    # The template half renders every one of them, so a POST of the rendered page matches its
    # endpoint's recount (design D14).
    assert f'name="population_fp" value="{ctxs[0]["adopt_fp"]}"' in plain
    assert f'name="population_fp" value="{ctxs[0]["stop_fp"]}"' in plain
    for it in ctxs[0]["items"]:
        assert f'name="population_fp" value="{it["fp"]}"' in plain


# --- D14: the guard, now four scoped forms ---------------------------------------------------
#
# `review-accept` is retired with its route and its button, so the suite is re-pointed at the four
# form scopes — but deliberately NOT blanket-parameterized across them. The event term is not the
# same for every verb (design D2/D3): a scoped verb hashes only the alerts of the files it will
# touch, while `baseline-new` hashes the collection's whole open-event set. Running every scenario
# against every verb would assert that a scoped verb refuses on drift it cannot reach, which is
# precisely what the delta's "is not refused by drift it cannot reach" scenarios forbid.
#
# So: the form-INDEPENDENT scenarios below run against all four forms, each drifting a row in the
# form's OWN population, and the event scenarios are written one per verb against the population
# that verb actually hashes.

FORMS = ["baseline-new", "adopt-changed", "stop-tracking", "accept-file"]

# The file status each form's own population is made of.
FORM_STATUS = {
    "baseline-new": "new",
    "adopt-changed": "modified",
    "stop-tracking": "missing",
    # A per-file accept is offered on the review page's rows; `missing` is the loud one of the two.
    "accept-file": "missing",
}
SUBJECT = "subject"  # the relpath the form's own row is seeded at...
SUBJECT_ID = 5  # ...at a known id, so the per-file URL is knowable before the seed runs


def _target(form: str, cid: int = 1, fid: int = SUBJECT_ID) -> str:
    """The endpoint that performs ``form``. One constant scope per URL (design D1)."""
    return {
        "baseline-new": f"/collection/{cid}/accept",
        "adopt-changed": f"/collection/{cid}/review/adopt-changed",
        "stop-tracking": f"/collection/{cid}/review/stop-tracking",
        "accept-file": f"/collection/{cid}/file/{fid}/accept",
    }[form]


def _fp_for(form: str, cid: int = 1, fid: int = SUBJECT_ID) -> str:
    return _fingerprint(cid, form, file_id=fid if form == "accept-file" else None)


async def _seed_subject(cid: int, form: str, **extra) -> None:
    """Seed the single row ``form`` acts on, at :data:`SUBJECT_ID`."""
    extra.setdefault("id", SUBJECT_ID)
    await _with_session(
        lambda s: _mk_files(s, cid, [(SUBJECT, FORM_STATUS[form], "complete", extra)])
    )


def _open_count(s):
    from src.models.db import Event

    return s.scalar(
        select(func.count()).select_from(Event).where(Event.acknowledged_at.is_(None))
    )


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


def _assert_performed(r, form):
    assert r.status_code == 303, r.status_code
    expected = "/collection/1" if form == "baseline-new" else "/collection/1/review"
    assert r.headers["location"] == expected


# --- (a) form-independent scenarios: all four forms, each drifting its own population ---------


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("fp", [None, "", "   "])
def test_accept_fails_closed_without_a_fingerprint(cairn_env, form, fp):
    root = cairn_env / "closed"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


@pytest.mark.parametrize("form", FORMS)
def test_each_form_accepts_its_own_unchanged_population(cairn_env, form):
    """The guard is not simply refusing everything — and each verb lands where it belongs."""
    root = cairn_env / "happy"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        _assert_performed(_post(client, _target(form), _fp_for(form), _csrf(client)), form)

    rows = _aside(lambda s: _statuses(s, 1))
    if FORM_STATUS[form] == "missing":
        assert rows == []  # the record was removed
    else:
        assert rows == [("ok", SUBJECT)]


@pytest.mark.parametrize("form", FORMS)
def test_a_reused_row_identifier_does_not_validate_a_stale_fingerprint(cairn_env, form):
    """`files.id` is a reusable rowid, so identifier + status alone cannot bind a population."""
    root = cairn_env / "reuse"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)

        async def swap(s):
            from src.models.db import FileEntry

            await s.delete(await s.get(FileEntry, SUBJECT_ID))
            await s.commit()
            await _mk_files(
                s, 1, [("a-different-file", FORM_STATUS[form], "none", {"id": SUBJECT_ID})]
            )

        _aside(swap)
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    # The replacement survives untouched — it was never on the page the operator saw.
    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], "a-different-file")]


@pytest.mark.parametrize("form", FORMS)
def test_a_recreated_record_does_not_validate_a_form_minted_for_its_predecessor(cairn_env, form):
    """Same path, same digest, reused id: only `first_seen` separates the two generations."""
    root = cairn_env / "generation"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form, sha256="d" * 64)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)

        async def regenerate(s):
            from src.models.db import FileEntry

            await s.delete(await s.get(FileEntry, SUBJECT_ID))
            await s.commit()
            await _mk_files(s, 1, [(
                SUBJECT, FORM_STATUS[form], "complete",
                {"id": SUBJECT_ID, "sha256": "d" * 64, "first_seen": NOW + timedelta(days=3)},
            )])

        _aside(regenerate)
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]

    def first_seen(s):
        from src.models.db import FileEntry

        return s.scalar(select(FileEntry.first_seen).where(FileEntry.id == SUBJECT_ID))

    assert _aside(first_seen).day == (NOW + timedelta(days=3)).day


@pytest.mark.parametrize("form", FORMS)
def test_a_collection_recreated_on_the_same_id_is_refused(cairn_env, form):
    root = cairn_env / "recreated"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)

        async def recreate(s):
            from src.models.db import Collection

            c = await s.get(Collection, 1)
            c.created_at = c.created_at + timedelta(seconds=5)
            await s.commit()

        _aside(recreate)
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


@pytest.mark.parametrize("form", FORMS)
def test_a_fingerprint_does_not_travel_between_collections(cairn_env, form):
    a, b = cairn_env / "ca", cairn_env / "cb"
    a.mkdir()
    b.mkdir()
    other_id = SUBJECT_ID + 1

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _seed_subject(ca, form)
        await _seed_subject(cb, form, id=other_id)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form, cid=2, fid=other_id)
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


@pytest.mark.parametrize("form", FORMS)
def test_accept_refuses_while_an_operation_is_in_flight(cairn_env, form):
    root = cairn_env / "inflight"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)

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
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


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


@pytest.mark.parametrize("form", FORMS)
def test_writer_lock_contention_refuses_instead_of_500ing(cairn_env, fast_busy_timeout, form):
    root = cairn_env / "busy"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    db_path = str(cairn_env / "db" / "cairn.db")
    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fp_for(form)
        holder = sqlite3.connect(db_path, timeout=1)
        try:
            holder.execute("PRAGMA journal_mode=WAL")
            holder.execute("BEGIN IMMEDIATE")  # hold the writer lock
            holder.execute("UPDATE collections SET name = name WHERE id = 1")
            _assert_refused(_post(client, _target(form), fp, token))
        finally:
            holder.rollback()
            holder.close()

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


@pytest.mark.parametrize("form", FORMS)
def test_a_non_lock_operational_error_is_not_reported_as_staleness(cairn_env, monkeypatch, form):
    root = cairn_env / "broken"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fp_for(form)

        from src.control_panel import routes

        async def boom(session, collection_id):
            raise OperationalError(
                "UPDATE collections", {}, sqlite3.OperationalError("disk I/O error")
            )

        monkeypatch.setattr(routes, "_take_write_lock", boom)
        with pytest.raises(OperationalError):
            _post(client, _target(form), fp, token)

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], SUBJECT)]


# --- (b) event scenarios: one per verb, against the population that verb actually hashes -------


def test_a_stale_baseline_form_is_refused_by_an_alert_on_a_file_it_does_not_name(cairn_env):
    """`baseline-new`'s event term is the COLLECTION's, and this is the ABA that needs it.

    An unrelated file goes ``ok -> modified -> missing -> restored``. Restoring acknowledges only
    its `missing` event (the restore branch's `kind='missing'` scoping, deliberately untouched by
    this change), so its earlier `modified` event is still open while the file itself is back to
    `ok`: the `new` set is exactly what the page showed and the issue count is back to zero. Under
    a *narrowed* event term that alert would drop out of the hash — B is not `new` — and the stale
    form would baseline beside an alarm nobody has read, which is the one thing the gate exists to
    prevent. Hashed over the whole collection, it is refused.
    """
    root = cairn_env / "aba-baseline"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("wobble", "ok", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fp_for("baseline-new")

        async def wobble(s):
            from src.models.db import FileEntry

            fe = await s.scalar(select(FileEntry).where(FileEntry.relpath == "wobble"))
            fe.status = "modified"
            await s.commit()
            await _mk_event(s, 1, fe.id, "modified")  # stays open by design
            fe.status = "missing"
            await s.commit()
            fe.status = "ok"  # restored: the file set is back to what the page showed
            await s.commit()

        _aside(wobble)
        _assert_refused(_post(client, _target("baseline-new"), fp, token))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("ok", "wobble") in rows
    assert _aside(_open_count) == 1  # the modified alert was NOT swept up


def test_baseline_has_no_outside_scope_even_a_detached_alert_refuses_it(cairn_env):
    """The matrix's deliberate blank: `baseline-new` has no drift it "cannot reach".

    Asserted explicitly rather than omitted. A detached open event (`file_id IS NULL`, left behind
    by an earlier stop-tracking) belongs to no file, so no narrowed verb can see it — but it is
    still an unread alarm, and the D7 gate is about unread alarms, not about reachability. The
    fingerprint is minted here *with* the detached event already in the set, so the refusal comes
    from the endpoint's explicit zero assertion and not merely from a hash mismatch.
    """
    root = cairn_env / "detached-baseline"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))
        await _with_session(lambda s: _mk_event(s, cid, None, "missing"))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("baseline-new")
        _assert_refused(_post(client, _target("baseline-new"), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [("new", "fresh")]
    assert _aside(_open_count) == 1


@pytest.mark.parametrize("form", ["adopt-changed", "stop-tracking", "accept-file"])
def test_a_detached_open_event_is_invisible_to_the_scoped_verbs(cairn_env, form):
    """The other half of design D3: no scoped verb acknowledges a detached alert, or is refused
    by one. *Mark all reviewed* — which mutates no file record and carries no fingerprint — is
    what clears it."""
    root = cairn_env / "detached-scoped"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)
        _aside(lambda s: _mk_event(s, 1, None, "missing"))  # detached, after the mint
        _assert_performed(_post(client, _target(form), fp, _csrf(client)), form)

    assert _aside(_open_count) == 1  # still open: no scoped verb touched it


def _assert_own_population_aba_is_refused(cairn_env, dirname, form):
    """One open alert on the form's own row is acknowledged and a fresh one of the same kind opened.

    The file records never move and the open-event COUNT returns to exactly what it was, so only
    identity + generation (`id` + `kind` + `detected_at`) can tell the two incidents apart. A stale
    form that validated here would silently close an alert that was never on the page.
    """
    root = cairn_env / dirname
    root.mkdir()
    kind = FORM_STATUS[form]

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)
        await _with_session(lambda s: _mk_event(s, cid, SUBJECT_ID, kind))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fp_for(form)
        assert _aside(_open_count) == 1

        async def swap(s):
            from src.models.db import Event

            e1 = await s.scalar(select(Event).where(Event.acknowledged_at.is_(None)))
            e1.acknowledged_at = NOW
            await s.commit()
            await _mk_event(s, 1, SUBJECT_ID, kind)

        _aside(swap)
        assert _aside(_open_count) == 1  # a true equal-count ABA
        _assert_refused(_post(client, _target(form), fp, token))

    assert _aside(lambda s: _statuses(s, 1)) == [(kind, SUBJECT)]
    assert _aside(_open_count) == 1  # the second, unseen incident is still open


def test_a_stale_adopt_form_is_refused_by_an_aba_on_its_own_modified_file(cairn_env):
    _assert_own_population_aba_is_refused(cairn_env, "aba-adopt", "adopt-changed")


def test_a_stale_stop_tracking_form_is_refused_by_an_aba_on_its_own_missing_file(cairn_env):
    _assert_own_population_aba_is_refused(cairn_env, "aba-stop", "stop-tracking")


def test_a_stale_per_file_form_is_refused_by_an_aba_on_its_own_row(cairn_env):
    _assert_own_population_aba_is_refused(cairn_env, "aba-file", "accept-file")


def test_adopt_is_not_refused_by_drift_it_cannot_reach(cairn_env):
    """A file goes missing and opens an alert; the modified set never moves ⇒ adopt proceeds.

    Refusing here would be a refusal the operator cannot account for, and refusals nobody can
    account for are how a guard gets worked around. The newly opened missing alert must survive:
    adopting a changed file is not a licence to close an alarm about a deleted one.
    """
    root = cairn_env / "adopt-unreachable"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("changed", "modified", "none")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("adopt-changed")

        async def a_file_goes_missing(s):
            from src.models.db import FileEntry

            await _mk_files(s, 1, [("vanished", "missing", "complete")])
            fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "vanished"))
            await _mk_event(s, 1, fid, "missing")

        _aside(a_file_goes_missing)
        _assert_performed(_post(client, "/collection/1/review/adopt-changed", fp, _csrf(client)),
                          "adopt-changed")

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("ok", "changed") in rows  # adopted
    assert ("missing", "vanished") in rows  # untouched
    assert _aside(_open_count) == 1  # and its alert is still open


def test_stop_tracking_is_not_refused_by_drift_it_cannot_reach(cairn_env):
    """A file goes modified and opens an alert; the missing set never moves ⇒ removal proceeds."""
    root = cairn_env / "stop-unreachable"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone", "missing", "complete")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("stop-tracking")

        async def a_file_changes(s):
            from src.models.db import FileEntry

            await _mk_files(s, 1, [("edited", "modified", "none")])
            fid = await s.scalar(select(FileEntry.id).where(FileEntry.relpath == "edited"))
            await _mk_event(s, 1, fid, "modified")

        _aside(a_file_changes)
        _assert_performed(_post(client, "/collection/1/review/stop-tracking", fp, _csrf(client)),
                          "stop-tracking")

    assert _aside(lambda s: _statuses(s, 1)) == [("modified", "edited")]
    assert _aside(_open_count) == 1  # the modified alert was not swept up


def test_a_per_file_action_is_not_refused_by_drift_on_another_row(cairn_env):
    """An alert opens on a different row and that row's state changes; the submitted row has not
    moved ⇒ the per-file accept proceeds, touching only the row it names."""
    root = cairn_env / "perfile-unreachable"
    root.mkdir()
    other_id = SUBJECT_ID + 1

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            (SUBJECT, "missing", "complete", {"id": SUBJECT_ID}),
            ("neighbour", "ok", "complete", {"id": other_id}),
        ]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("accept-file")

        async def the_neighbour_moves(s):
            from src.models.db import FileEntry

            fe = await s.get(FileEntry, other_id)
            fe.status = "modified"
            await s.commit()
            await _mk_event(s, 1, other_id, "modified")

        _aside(the_neighbour_moves)
        _assert_performed(_post(client, _target("accept-file"), fp, _csrf(client)), "accept-file")

    assert _aside(lambda s: _statuses(s, 1)) == [("modified", "neighbour")]
    assert _aside(_open_count) == 1  # the neighbour's alert is untouched


@pytest.mark.parametrize("form", ["adopt-changed", "stop-tracking", "accept-file"])
def test_a_file_in_a_population_no_action_names_refuses_nothing(cairn_env, form):
    """The documented `new`-set exception, now true by construction: no review-page verb touches
    the not-yet-baselined set, so a growing collection is neither refused nor silently promoted."""
    root = cairn_env / "growing"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)
        # `added` events are born acknowledged, so the open-event set is untouched too.
        _aside(lambda s: _mk_files(s, 1, [("arrived", "new", "pending")]))
        _assert_performed(_post(client, _target(form), fp, _csrf(client)), form)

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "arrived") in rows  # neither promoted nor otherwise touched


# --- (c) cross-form replay: every ordered pair of the four form scopes -------------------------


@pytest.mark.parametrize(
    "minted,submitted", [(a, b) for a in FORMS for b in FORMS if a != b]
)
def test_no_accept_form_validates_any_action_but_its_own(cairn_env, minted, submitted):
    root = cairn_env / "crossform"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        # All four populations non-empty at once, with the per-file subject at a known id.
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"),
            ("changed", "modified", "none"),
            (SUBJECT, "missing", "complete", {"id": SUBJECT_ID}),
        ]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(minted)
        _assert_refused(_post(client, _target(submitted), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [
        ("missing", SUBJECT), ("modified", "changed"), ("new", "fresh"),
    ]


def test_a_bulk_form_and_its_rows_own_form_do_not_validate_each_other(cairn_env):
    """The pair whose two populations are byte-identical.

    A collection whose only issue is one missing row: `stop-tracking` and that row's `accept-file`
    cover the same single record and the same events. Only the header separates them, so a refusal
    here cannot be a coincidence of the population.
    """
    root = cairn_env / "identical"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(
            s, cid, [(SUBJECT, "missing", "complete", {"id": SUBJECT_ID})]
        ))
        await _with_session(lambda s: _mk_event(s, cid, SUBJECT_ID, "missing"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        bulk, per_row = _fp_for("stop-tracking"), _fp_for("accept-file")
        assert bulk != per_row
        _assert_refused(_post(client, _target("accept-file"), bulk, token))
        _assert_refused(_post(client, _target("stop-tracking"), per_row, token))

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", SUBJECT)]
    assert _aside(_open_count) == 1


def test_the_header_alone_separates_two_byte_identical_populations():
    """A pure-encoder check that the *scope string* and the `file=` term do the work.

    In the route matrix above, two forms' populations always differ by at least one record, so a
    refusal there could in principle come from the population rather than from the header. Here the
    file records and the events are the same objects in both preimages by construction.
    """
    from types import SimpleNamespace

    from src.control_panel.routes import (
        _PopEvent,
        _PopFile,
        _Population,
        _population_fingerprint,
    )

    collection = SimpleNamespace(id=1, created_at=NOW)
    common = {
        "files": [_PopFile(
            id=SUBJECT_ID, relpath=SUBJECT, status="missing", sha256="a" * 64,
            first_seen=NOW, size=10, last_checked=NOW, last_changed=None, ots_state="complete",
        )],
        "open_events": [_PopEvent(id=1, kind="missing", detected_at=NOW, file_id=SUBJECT_ID)],
        "issues": 0,
        "total_files": 1,
    }
    adopt = _population_fingerprint(collection, _Population(scope="adopt-changed", **common))
    stop = _population_fingerprint(collection, _Population(scope="stop-tracking", **common))
    assert adopt != stop

    row_a = _population_fingerprint(
        collection, _Population(scope="accept-file", file_id=SUBJECT_ID, **common)
    )
    row_b = _population_fingerprint(
        collection, _Population(scope="accept-file", file_id=SUBJECT_ID + 1, **common)
    )
    assert row_a != row_b


# --- (d) cross-row replay for the per-file form ------------------------------------------------


def test_a_per_file_form_does_not_validate_a_submission_at_another_row(cairn_env):
    """Two rows sharing status, digest and size: neither row's form works at the other's address."""
    root = cairn_env / "crossrow"
    root.mkdir()
    a_id, b_id = SUBJECT_ID, SUBJECT_ID + 1

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("row-a", "missing", "complete", {"id": a_id, "sha256": "c" * 64, "size": 4096}),
            ("row-b", "missing", "complete", {"id": b_id, "sha256": "c" * 64, "size": 4096}),
        ]))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp_a = _fingerprint(1, "accept-file", file_id=a_id)
        fp_b = _fingerprint(1, "accept-file", file_id=b_id)
        assert fp_a != fp_b
        _assert_refused(_post(client, _target("accept-file", fid=b_id), fp_a, token))
        _assert_refused(_post(client, _target("accept-file", fid=a_id), fp_b, token))

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "row-a"), ("missing", "row-b")]


# --- 2.11: the drift each verb MUST be refused by, and the per-file 404s -----------------------


@pytest.mark.parametrize("drift", ["enters", "leaves"])
def test_adopt_is_refused_when_its_own_set_gains_or_loses_a_file(cairn_env, drift):
    root = cairn_env / "adopt-drift"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("changed", "modified", "none"),
            ("steady", "ok", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("adopt-changed")

        async def move(s):
            from src.models.db import FileEntry

            relpath = "steady" if drift == "enters" else "changed"
            fe = await s.scalar(select(FileEntry).where(FileEntry.relpath == relpath))
            fe.status = "modified" if drift == "enters" else "ok"
            await s.commit()

        _aside(move)
        _assert_refused(_post(client, "/collection/1/review/adopt-changed", fp, _csrf(client)))

    rows = dict((p, st) for st, p in _aside(lambda s: _statuses(s, 1)))
    if drift == "enters":
        assert rows == {"changed": "modified", "steady": "modified"}
    else:
        assert rows == {"changed": "ok", "steady": "ok"}


def test_a_per_file_form_is_refused_after_that_row_was_already_accepted(cairn_env):
    """Another tab got there first: the row is not in the recount, so the record set is empty."""
    root = cairn_env / "already"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            (SUBJECT, "missing", "complete", {"id": SUBJECT_ID}),
            ("other", "missing", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("accept-file")

        async def another_tab(s):
            from src.models.db import FileEntry

            await s.delete(await s.get(FileEntry, SUBJECT_ID))
            await s.commit()

        _aside(another_tab)
        _assert_refused(_post(client, _target("accept-file"), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "other")]


def test_a_per_file_form_is_refused_after_the_row_was_restored(cairn_env):
    """`missing -> ok` between render and submit: a restored file is never silently deleted."""
    root = cairn_env / "restored"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(
            s, cid, [(SUBJECT, "missing", "complete", {"id": SUBJECT_ID})]
        ))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for("accept-file")

        async def restore(s):
            from src.models.db import FileEntry

            fe = await s.get(FileEntry, SUBJECT_ID)
            fe.status = "ok"
            await s.commit()

        _aside(restore)
        _assert_refused(_post(client, _target("accept-file"), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [("ok", SUBJECT)]


def test_a_per_file_post_at_another_collections_row_is_404(cairn_env):
    a, b = cairn_env / "mine", cairn_env / "theirs"
    a.mkdir()
    b.mkdir()
    other_id = SUBJECT_ID + 1

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(
            s, ca, [(SUBJECT, "missing", "complete", {"id": SUBJECT_ID})]
        ))
        await _with_session(lambda s: _mk_files(
            s, cb, [("theirs", "missing", "complete", {"id": other_id})]
        ))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        fp = _fingerprint(2, "accept-file", file_id=other_id)
        r = _post(client, f"/collection/1/file/{other_id}/accept", fp, token)
        assert r.status_code == 404

    assert _aside(lambda s: _statuses(s, 2)) == [("missing", "theirs")]


def test_a_per_file_post_at_another_users_collection_is_404(cairn_env):
    root = cairn_env / "mine"
    other = cairn_env / "someone-else"
    root.mkdir()
    other.mkdir()
    other_id = SUBJECT_ID + 1

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(
            s, cid, [(SUBJECT, "missing", "complete", {"id": SUBJECT_ID})]
        ))

        async def foreign(s):
            from src.models.db import Collection, FileEntry, User

            u = User(username="other", password_hash="x", is_admin=False)
            s.add(u)
            await s.commit()
            c = Collection(user_id=u.id, name="theirs", root=str(other), mode="worm",
                           ots_mode="perfile")
            s.add(c)
            await s.commit()
            s.add(FileEntry(
                id=other_id, collection_id=c.id, relpath="theirs", size=10, sha256="a" * 64,
                status="missing", ots_state="complete", first_seen=NOW, last_checked=NOW,
            ))
            await s.commit()
            return c.id

        return await _with_session(foreign)

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        r = _post(client, f"/collection/2/file/{other_id}/accept", "a" * 64, token)
        assert r.status_code == 404

    assert _aside(lambda s: _statuses(s, 2)) == [("missing", "theirs")]


# --- D14 (post-audit): the render and the mint come from ONE fetch ---------------------------


def _drift_after_the_population_read(monkeypatch, drift):
    """Patch `_read_population` into a barrier: it does the real read, then lets `drift` commit.

    This is the interleaving a two-SELECT page cannot survive — `SELECT` does not open a
    transaction under Python's legacy sqlite3 transaction control, so a scanner committing here
    would land in a *second* read but not the first. Returns the recorder: one entry per call, each
    `(read_scope, collection-stand-in, the population that was returned)`, so the test can re-derive
    any form's fingerprint from the snapshot the page actually rendered.
    """
    from types import SimpleNamespace

    from src.control_panel import routes

    seen: list[tuple] = []
    real = routes._read_population

    async def spy(session, collection, scope, file_id=None):
        pop = await real(session, collection, scope, file_id=file_id)
        seen.append((scope, SimpleNamespace(id=collection.id, created_at=collection.created_at), pop))
        if len(seen) == 1:
            await _with_session(drift)
        return pop

    monkeypatch.setattr(routes, "_read_population", spy)
    return seen


def test_the_review_page_hashes_exactly_the_population_it_rendered(cairn_env, monkeypatch):
    """One fetch feeds the rows, the counts and EVERY fingerprint — with a scan landing right after.

    The failure this pins: rows read in one statement and a fingerprint minted in another, with a
    scan committing in between. The form would then carry the *new* population's fingerprint while
    the page listed the old one, and an unchanged POST would validate and delete a `missing` row the
    operator never saw. With three kinds of fingerprint on this page now, it also pins that no two
    of them describe different states.
    """
    from src.control_panel import routes

    root = cairn_env / "mint"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("seen", "missing", "complete", {"id": SUBJECT_ID}),
        ]))

        async def alarm(s):
            await _mk_event(s, cid, SUBJECT_ID, "missing")

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
        assert [scope for scope, _, _ in seen] == ["review"]
        _, coll, pop = seen[0]
        ctx = [c for name, c in captured if name == "collection_review.html"][0]

        # ...and every published fingerprint is a pure slice of that snapshot, not a re-read one.
        def slice_fp(form, file_id=None):
            statuses = routes._FP_FORMS[form][1]
            return routes._population_fingerprint(
                coll, routes._narrow(pop, form, statuses, file_id=file_id)
            )

        assert ctx["stop_fp"] == slice_fp("stop-tracking")
        assert ctx["adopt_fp"] == slice_fp("adopt-changed")
        assert ctx["items"][0]["fp"] == slice_fp("accept-file", file_id=SUBJECT_ID)
        assert f'name="population_fp" value="{ctx["stop_fp"]}"' in body
        # The row that landed after the single fetch is in neither half: not rendered, not hashed.
        assert "seen" in body and "unseen" not in body
        assert ctx["total_issues"] == 1 and ctx["shown"] == 1 and ctx["stop_count"] == 1
        # The open-event component is read in the same statement, so the alert that opened after
        # it is outside the pill and outside the hash too.
        assert ctx["review_open"] == 1

        # And the drift is caught where it must be: at submit time, under the write lock. The new
        # missing row joined the stop-tracking population, so that form is refused...
        token = _csrf(client)
        _assert_refused(_post(client, _target("stop-tracking"), ctx["stop_fp"], token))
        # ...while the per-file form for the row that never moved is still good.
        _assert_performed(
            _post(client, _target("accept-file"), ctx["items"][0]["fp"], token), "accept-file"
        )

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "unseen")]


def test_the_detail_baseline_form_hashes_the_population_that_gated_it(cairn_env, monkeypatch):
    """Same contract on the detail page: the D7 gate and the D14 mint share one read."""
    from src.control_panel import routes

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

        assert [scope for scope, _, _ in seen] == ["baseline-new"]
        _, coll, pop = seen[0]
        ctx = [c for name, c in captured if name == "collection_detail.html"][0]
        assert ctx["show_baseline"] is True
        assert ctx["population_fp"] == routes._population_fingerprint(
            coll, routes._narrow(pop, "baseline-new", ("new",))
        )
        assert f'name="population_fp" value="{ctx["population_fp"]}"' in body

        # The issue that landed after that read is not in the hash, so the POST's own read (which
        # sees it) refuses — the zero-issue assertion the form made no longer holds.
        _assert_refused(_post(client, "/collection/1/accept", ctx["population_fp"], _csrf(client)))

    rows = _aside(lambda s: _statuses(s, 1))
    assert ("new", "fresh") in rows and ("missing", "gone") in rows


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
