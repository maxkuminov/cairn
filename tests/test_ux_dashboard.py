"""The dashboard/collection surfaces and the accept-family guard.

Three parts; `unify-issue-model` extended the second and added the third:

* **Honesty of the surfaces** (#18, #20, #31, #32) — the sidebar badge counts the same population
  as the dashboard's needs-you total beside it, no coverage claim is computed over a population
  that includes missing files, the two not-yet-confirmed proof states are never summed, and a
  collection watching nothing never reads green.
* **The D14 population fingerprint** (#14, extended by #16/#30 and by the folder scope of
  `unify-issue-model` D6) — each of the three accept forms (`adopt-changed`, `stop-tracking`,
  `accept-file`) acts only on the population its own form was rendered for, and refuses (with no
  mutation at all) on any drift *within that population*, including the cases a naive
  `id + status` preimage cannot see: SQLite rowid reuse, a row deleted and re-created at the same
  path with the same bytes, and an ABA on the open-event set. Equally load-bearing in the other
  direction: a scoped verb is NOT refused by drift it cannot reach, because refusals an operator
  cannot account for are how a guard gets worked around. The two bulk forms additionally name a
  **folder scope** `(dir, deep)`, which travels with the submission and is therefore bound into
  the fingerprint rather than trusted: a fingerprint minted for one scope validates no other —
  including where the two hold exactly the same files — and the records mutated are exactly the
  ones the recount hashed.
* **One kind of open item** (`unify-issue-model` D1/D3/D4/D10) — the acknowledge-only and
  baseline addresses are gone and mutate nothing, no surface offers to acknowledge or to
  baseline, and the dashboard leads with what needs the operator.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ux_dashboard.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.conftest import seed_collection  # noqa: F401  (cairn_env comes from conftest)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


# --- harness --------------------------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    """Run an async seed coroutine on a throwaway loop, drop the engine, return a TestClient.

    Each loop's engine is disposed ON that loop before the loop goes away. An aiosqlite connection
    left pooled past its loop is closed later by the garbage collector, from a worker thread that
    then reports to a closed loop — harmless, but it lands as an "unhandled thread exception"
    warning on whichever unrelated test happens to be running.
    """
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    async def _seed():
        try:
            await seed_coro()
        finally:
            await database.get_engine().dispose()

    asyncio.run(_seed())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warning)

    class _Client(TestClient):
        def __exit__(self, *args):
            with contextlib.suppress(Exception):
                self.portal.call(database.get_engine().dispose)
            database.reset_engine()
            return super().__exit__(*args)

    return _Client(app)


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


async def _mk_event(session, cid, file_id, kind, *, acked=False, detected_at=None, detail=None):
    from src.models.db import Event

    e = Event(
        collection_id=cid,
        file_id=file_id,
        kind=kind,
        detail=detail,
        detected_at=detected_at or NOW,
        acknowledged_at=NOW if acked else None,
    )
    session.add(e)
    await session.commit()
    return e.id


async def _mk_issue(session, cid, relpath, status, *, detected_at=None, **extra):
    """One issue the way a scan leaves it: the file record AND its open alert.

    Returns the file's id.
    """
    from src.models.db import FileEntry

    await _mk_files(session, cid, [(relpath, status, extra.pop("ots_state", "complete"), extra)])
    fid = await session.scalar(
        select(FileEntry.id).where(FileEntry.collection_id == cid, FileEntry.relpath == relpath)
    )
    await _mk_event(session, cid, fid, status, detected_at=detected_at)
    return fid


def _scope(dir: str = "", deep: bool = True):
    """A folder scope built DIRECTLY — deliberately not through the route's parser.

    The tests need to name scopes the parser refuses (to prove a fingerprint that would reproduce
    under the raw scope is still refused), so this is the tuple and nothing else.
    """
    from src.control_panel.routes import _Scope

    return _Scope(dir, deep)


def _fingerprint(collection_id: int, form: str, file_id: int | None = None, folder=None) -> str:
    """The fingerprint a page would publish for ``form``, through the production helpers.

    Minted the way the MINT side mints it: the page's one wide read, narrowed purely to the form's
    own population — its status, and for the two bulk forms its folder scope (``None`` = the whole
    collection, which is what a page-wide control carries).
    """

    async def _go(s):
        from src.control_panel.routes import (
            _narrow,
            _population_fingerprint,
            _read_population,
        )
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        pop = await _read_population(s, collection)
        return _population_fingerprint(
            collection, _narrow(pop, form, file_id=file_id, folder=folder)
        )

    return _aside(_go)


def _narrowed_relpaths(collection_id: int, form: str, folder=None) -> list[str]:
    """The relpaths ``form`` would hash at ``folder`` right now (what its recount covers)."""

    async def _go(s):
        from src.control_panel.routes import _narrow, _read_population
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        pop = await _read_population(s, collection)
        return [f.relpath for f in _narrow(pop, form, folder=folder).files]

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


# --- #18: the sidebar badge ------------------------------------------------------------------
#
# The "Open issues" and "New files" tiles this section used to pin are gone (unify-issue-model
# D10/D4): the dashboard's needs-you section is what states the number now, and its tests live in
# the dashboard section at the end of this module.


def _badge_number(body: str) -> int | None:
    """The number the sidebar badge renders, or ``None`` when the badge is absent."""
    m = re.search(r'class="nav-badge"[^>]*>([\d,]+)</span>', body)
    return int(m.group(1).replace(",", "")) if m else None


def _needs_total(body: str) -> int | None:
    """The number the dashboard's needs-you section leads with, or ``None`` when it has none."""
    m = re.search(r'id="needs-total">([\d,]+)<', body)
    return int(m.group(1).replace(",", "")) if m else None


def _section(body: str, element_id: str) -> str:
    """The markup of the dashboard ``<section>`` carrying ``element_id`` ("" when absent)."""
    m = re.search(rf'<section[^>]*id="{element_id}"[^>]*>(.*?)</section>', body, re.S)
    return m.group(0) if m else ""


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
        # The badge and the needs-you total beside it summarise the same population.
        assert _badge_number(body) == 2
        assert _needs_total(body) == 2


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
#
# The detail page used to offer "Baseline N new files" once everything was quiet. That form, its
# route and its fingerprint scope are gone (unify-issue-model D3/D4): accepting is irreversible and
# is reachable only from a review view, which lists the files it acts on.

# Every state-changing control on the detail page: `action=` / `hx-post=` targets, in page order.
_DETAIL_POST_TARGETS = re.compile(r'(?:action|hx-post)="([^"]+)"')


def _detail_post_targets(body: str) -> list[str]:
    return [t for t in _DETAIL_POST_TARGETS.findall(body) if not t.startswith("/verify")]


def _assert_no_accept_control(body: str, where: str) -> None:
    """No accept/baseline form of any kind — by address, by field and by label."""
    assert 'name="population_fp"' not in body, where
    for target in _DETAIL_POST_TARGETS.findall(body):
        for fragment in ("accept", "adopt-changed", "stop-tracking", "/ack", "baseline"):
            assert fragment not in target, (where, target)
    for label in ("Baseline ", "Accept change", "Accept deletion", "Accept changes",
                  "Stop tracking", "Adopt "):
        assert label not in body, (where, label)
    assert not re.search(r"Accept [\d,]+ (change|deletion)", body), where


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
        assert "Baseline " not in body
        assert 'href="/collection/1/review"' in body
        _assert_no_accept_control(body, "detail with issues")
        # While issues exist the review link is THE primary action of the header — the one
        # filled button on the page — and it states the count it leads to.
        header = body.split('class="page-header__actions"', 1)[1].split("</div>", 1)[0]
        assert header.count("btn--primary") == 1
        assert re.search(
            r'<a class="btn btn--primary" href="/collection/1/review">.*?Review 1 issue</a>',
            header, re.S,
        )


@pytest.mark.parametrize(
    "state",
    ["issues", "issues-and-new", "only-new", "only-new-with-a-detached-open-event", "nothing",
     "all-ok"],
)
@pytest.mark.parametrize("ots_mode", ["perfile", "none"])
def test_the_detail_page_offers_no_accept_action_in_any_state(cairn_env, state, ots_mode):
    """web-panel "The detail page offers no accept action in any state".

    The retired page offered "Baseline N new files" exactly when a collection had `new` files, no
    issues and no open event — so that is the state pinned hardest here, alongside the one that
    used to *hide* the form (a detached open event) and the ones that never had it.
    """
    root = cairn_env / "detail-states"
    root.mkdir()
    files = {
        "issues": [("gone", "missing", "complete"), ("changed", "modified", "complete")],
        "issues-and-new": [("gone", "missing", "complete"), ("fresh", "new", "complete")],
        "only-new": [("fresh-a", "new", "complete"), ("fresh-b", "new", "pending")],
        "only-new-with-a-detached-open-event": [("fresh", "new", "complete")],
        "nothing": [],
        "all-ok": [("kept", "ok", "complete")],
    }[state]

    async def seed():
        cid = await seed_collection(root, ots_mode=ots_mode)
        await _with_session(lambda s: _mk_files(s, cid, files))
        if state == "only-new-with-a-detached-open-event":
            await _with_session(lambda s: _mk_event(s, cid, None, "missing"))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/collection/1").text
        ctx = [c for name, c in captured if name == "collection_detail.html"][0]

    _assert_no_accept_control(body, state)
    # The page's only state-changing controls are the scan and (notarized collections) the stamp.
    expected = ["/collection/1/scan"]
    if ots_mode == "perfile":
        expected.append("/collection/1/stamp-all")
    assert _detail_post_targets(body) == expected
    # ...and the route publishes nothing an accept form could be built from.
    for key in ("population_fp", "show_baseline", "adopt_fp", "stop_fp"):
        assert key not in ctx, key
    assert "not yet baselined" not in body

    has_issues = state.startswith("issues")
    header = body.split('class="page-header__actions"', 1)[1].split("</div>", 1)[0]
    if has_issues:
        assert '<a class="btn btn--primary" href="/collection/1/review">' in header
    else:
        # No issues ⇒ no primary action at all, and nothing links to a review of nothing.
        assert "btn--primary" not in header
        assert 'href="/collection/1/review"' not in body


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
        r = c["r"]
        adopt, stop = r["accept_changes"], r["accept_deletions"]
        assert re.fullmatch(r"[0-9a-f]{64}", adopt["fp"])
        assert re.fullmatch(r"[0-9a-f]{64}", stop["fp"])
        # Two verbs, two populations, two fingerprints: neither can validate the other's endpoint.
        assert adopt["fp"] != stop["fp"]
        # The button labels' numbers come from the same snapshot as the rows (design D9).
        assert adopt["count"] == 1 and stop["count"] == 1
        # A page-wide control names the page's own scope: here, the whole collection.
        assert (adopt["dir"], adopt["deep"]) == ("", "1") == (stop["dir"], stop["deep"])
        assert len(r["rows"]) == 2
        for it in r["rows"]:
            assert re.fullmatch(r"[0-9a-f]{64}", it["fp"])
        assert len({it["fp"] for it in r["rows"]}) == 2  # each row's form is its own
    # The retired keys are gone with their routes and their buttons: a live fingerprint with no
    # form to mint it is exactly what the scoped-verb change removed, and the acknowledge-only
    # count went with `unify-issue-model`.
    for key in ("population_fp", "adopt_fp", "stop_fp", "review_open", "open_events"):
        assert key not in ctxs[0], key
    assert ctxs[0]["stale"] is False
    assert ctxs[1]["stale"] is True
    assert ctxs[2]["stale"] is False  # only `1` is recognized, exactly as view/filter are

    # The template half renders every one of them, so a POST of the rendered page matches its
    # endpoint's recount (design D14). They are also exactly what the production helpers mint.
    r = ctxs[0]["r"]
    assert r["accept_changes"]["fp"] == _fingerprint(1, "adopt-changed")
    assert r["accept_deletions"]["fp"] == _fingerprint(1, "stop-tracking")
    assert f'name="population_fp" value="{r["accept_changes"]["fp"]}"' in plain
    assert f'name="population_fp" value="{r["accept_deletions"]["fp"]}"' in plain
    for it in r["rows"]:
        assert it["fp"] == _fingerprint(1, "accept-file", file_id=it["id"])
        assert f'name="population_fp" value="{it["fp"]}"' in plain
    assert plain.count('name="population_fp"') == 4  # two bulk controls + two rows, nothing else


# --- D14: the guard, three scoped forms ------------------------------------------------------
#
# `review-accept` and `baseline-new` are both retired with their routes and their buttons, so the
# suite runs against the three surviving form scopes — but deliberately NOT blanket-parameterized
# across them. The event term follows each verb's own population (design D2/D3): a scoped verb
# hashes only the alerts of the files it will touch. Running every scenario against every verb with
# drift on *any* file would assert that a scoped verb refuses on drift it cannot reach, which is
# precisely what the delta's "is not refused by drift it cannot reach" scenarios forbid.
#
# So: the form-INDEPENDENT scenarios below run against all three forms, each drifting a row in the
# form's OWN population, and the event scenarios are written one per verb against the population
# that verb actually hashes. The folder scope of the two bulk forms has its own section further
# down; everything here submits the whole-collection scope a page-wide control carries.

FORMS = ["adopt-changed", "stop-tracking", "accept-file"]
BULK_FORMS = ["adopt-changed", "stop-tracking"]

# The file status each form's own population is made of.
FORM_STATUS = {
    "adopt-changed": "modified",
    "stop-tracking": "missing",
    # A per-file accept is offered on the review page's rows; `missing` is the loud one of the two.
    "accept-file": "missing",
}
SUBJECT = "subject"  # the relpath the form's own row is seeded at...
SUBJECT_ID = 5  # ...at a known id, so the per-file URL is knowable before the seed runs


def test_the_guard_knows_exactly_the_three_surviving_forms():
    """`baseline-new` and its whole-collection event exception went with their route (D3)."""
    from src.control_panel import routes

    assert sorted(routes._FP_FORMS) == sorted(FORMS)
    assert routes._FP_BULK_FORMS == frozenset(BULK_FORMS)
    assert routes._FP_FORMS == {
        "adopt-changed": ("modified",),
        "stop-tracking": ("missing",),
        "accept-file": ("missing", "modified"),
    }
    # No form's population includes a file that is merely new.
    assert all("new" not in statuses for statuses in routes._FP_FORMS.values())
    for retired in ("_FP_READ_SCOPES", "_FP_ACCEPT_SCOPES"):
        assert not hasattr(routes, retired), retired


def _target(form: str, cid: int = 1, fid: int = SUBJECT_ID) -> str:
    """The endpoint that performs ``form``. One constant verb per URL (design D1)."""
    return {
        "adopt-changed": f"/collection/{cid}/review/adopt-changed",
        "stop-tracking": f"/collection/{cid}/review/stop-tracking",
        "accept-file": f"/collection/{cid}/file/{fid}/accept",
    }[form]


def _fp_for(form: str, cid: int = 1, fid: int = SUBJECT_ID, folder=None) -> str:
    if form == "accept-file":
        return _fingerprint(cid, form, file_id=fid)
    return _fingerprint(cid, form, folder=folder)


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


async def _snapshot(session):
    """Every file record and every event, in full — what "mutated nothing" is compared against."""
    from src.models.db import Event, FileEntry

    files = sorted(
        tuple(row)
        for row in (
            await session.execute(
                select(
                    FileEntry.id, FileEntry.collection_id, FileEntry.relpath, FileEntry.status,
                    FileEntry.sha256, FileEntry.first_seen,
                )
            )
        ).all()
    )
    events = sorted(
        tuple(row)
        for row in (
            await session.execute(
                select(
                    Event.id, Event.collection_id, Event.file_id, Event.kind, Event.detail,
                    Event.acknowledged_at, Event.acknowledged_by,
                )
            )
        ).all()
    )
    return files, events


def _post(client, url, fp, token, **fields):
    """POST an accept form. ``fields`` are the form's other hidden inputs (``dir``, ``deep``,
    ``return_dir``, ``return_deep``); one left out is simply not submitted, so the route's own
    default applies — which for a bulk form is the whole collection (``dir=""``, ``deep="1"``)."""
    data = {"population_fp": fp} if fp is not None else {}
    data.update({k: v for k, v in fields.items() if v is not None})
    return client.post(
        url,
        data=data,
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def _assert_refused(r, location="/collection/1/review?stale=1"):
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == location


def _assert_performed(r, form=None, location="/collection/1/review"):
    """Every surviving form lands back on a freshly rendered review page (``form`` is unused: it
    is kept so call sites still say which verb they expected to run)."""
    assert r.status_code == 303, r.status_code
    assert r.headers["location"] == location


# --- (a) form-independent scenarios: all three forms, each drifting its own population --------


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


@pytest.mark.parametrize("form", FORMS)
def test_a_detached_open_event_is_invisible_to_the_scoped_verbs(cairn_env, form):
    """Design D3 of the scoped-verb change, which survives: no verb acknowledges a detached
    alert, or is refused by one.

    A detached open event (`file_id IS NULL` — the record it was about has been removed) belongs
    to no file, so it is in no form's population. With `baseline-new` gone there is no longer any
    form whose event term is the whole collection's — this is now true of every accept the panel
    offers.
    """
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


@pytest.mark.parametrize("form", FORMS)
def test_a_file_in_a_population_no_action_names_refuses_nothing(cairn_env, form):
    """True by construction: no accept the panel offers touches a file that is merely new, so a
    growing collection is neither refused nor silently promoted."""
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


# --- (c) cross-form replay: every ordered pair of the three form scopes ------------------------

CROSS_FORM_PAIRS = [(a, b) for a in FORMS for b in FORMS if a != b]


def test_the_cross_form_matrix_is_every_ordered_pair():
    assert len(CROSS_FORM_PAIRS) == 6 == len(set(CROSS_FORM_PAIRS))


@pytest.mark.parametrize("minted,submitted", CROSS_FORM_PAIRS)
def test_no_accept_form_validates_any_action_but_its_own(cairn_env, minted, submitted):
    root = cairn_env / "crossform"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        # All three populations non-empty at once (plus a merely-new file no form names), each
        # with its own open alert, and the per-file subject at a known id.
        await _with_session(lambda s: _mk_files(s, cid, [("fresh", "new", "complete")]))
        await _with_session(lambda s: _mk_issue(s, cid, "changed", "modified", ots_state="none"))
        await _with_session(lambda s: _mk_issue(s, cid, SUBJECT, "missing", id=SUBJECT_ID))

    with _make_client(cairn_env, seed) as client:
        before = _aside(_snapshot)
        fp = _fp_for(minted)
        _assert_refused(_post(client, _target(submitted), fp, _csrf(client)))

    assert _aside(lambda s: _statuses(s, 1)) == [
        ("missing", SUBJECT), ("modified", "changed"), ("new", "fresh"),
    ]
    assert _aside(_snapshot) == before  # no record removed, no change accepted, no event closed


@pytest.mark.parametrize(
    "bulk,status", [("stop-tracking", "missing"), ("adopt-changed", "modified")]
)
def test_a_bulk_form_and_its_rows_own_form_do_not_validate_each_other(cairn_env, bulk, status):
    """The pairs whose two populations are byte-identical.

    A collection whose only issue is one row: the bulk form for that row's state and that row's
    `accept-file` cover the same single record and the same events. Only the header separates
    them, so a refusal here cannot be a coincidence of the population.
    """
    root = cairn_env / "identical"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(
            s, cid, [(SUBJECT, status, "complete", {"id": SUBJECT_ID})]
        ))
        await _with_session(lambda s: _mk_event(s, cid, SUBJECT_ID, status))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        bulk_fp, per_row = _fp_for(bulk), _fp_for("accept-file")
        assert bulk_fp != per_row
        _assert_refused(_post(client, _target("accept-file"), bulk_fp, token))
        _assert_refused(_post(client, _target(bulk), per_row, token))

    assert _aside(lambda s: _statuses(s, 1)) == [(status, SUBJECT)]
    assert _aside(_open_count) == 1


def _pure_population(**overrides):
    """One missing row + its one open alert, as plain tuples — no datastore involved."""
    from src.control_panel.routes import _PopEvent, _PopFile

    common = {
        "files": [_PopFile(
            id=SUBJECT_ID, relpath=f"a/{SUBJECT}", status="missing", sha256="a" * 64,
            first_seen=NOW, size=10, last_checked=NOW, last_changed=None, ots_state="complete",
            mtime=None,
        )],
        "open_events": [_PopEvent(id=1, kind="missing", detected_at=NOW, file_id=SUBJECT_ID)],
        "total_files": 1,
    }
    common.update(overrides)
    return common


def test_the_header_alone_separates_two_byte_identical_populations():
    """A pure-encoder check that the *scope string* and the `file=` term do the work.

    In the route matrix above, two forms' populations always differ by at least one record, so a
    refusal there could in principle come from the population rather than from the header. Here the
    file records and the events are the same objects in both preimages by construction.
    """
    from types import SimpleNamespace

    from src.control_panel.routes import _Population, _population_fingerprint

    collection = SimpleNamespace(id=1, created_at=NOW)
    common = _pure_population()
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
    assert len({adopt, stop, row_a, row_b}) == 4


@pytest.mark.parametrize("form", BULK_FORMS)
def test_the_folder_scope_alone_separates_two_byte_identical_populations(form):
    """The same check one level down (design D6): the `dir=` / `deep=` header terms do the work.

    One record at `a/subject`. The whole collection, `a/`'s subtree and the files directly in `a/`
    all hold exactly that record, so nothing but the header can tell their fingerprints apart —
    and it must, or a form rendered for one folder would validate a submission naming another.
    """
    from types import SimpleNamespace

    from src.control_panel.routes import _Population, _population_fingerprint

    collection = SimpleNamespace(id=1, created_at=NOW)
    common = _pure_population()

    def fp(folder):
        return _population_fingerprint(
            collection, _Population(scope=form, folder=folder, **common)
        )

    scopes = [
        _scope("", True), _scope("", False), _scope("a", True), _scope("a", False),
        _scope("b", True), _scope("a/b", True),
        # Names carrying the encoding's own separators: the byte-length prefix on `dir` is what
        # keeps these from being read as a different header.
        _scope("a\x1fdeep=0", True), _scope("a\x1fdeep=1", False),
        _scope("a\x1e", True), _scope("é", True), _scope("e\u0301", True),
    ]
    prints = [fp(s) for s in scopes]
    assert len(set(prints)) == len(scopes)
    # An absent folder is the whole collection — the same statement as the explicit root scope,
    # so the mint side and the recount side cannot disagree about what "no folder" means.
    assert fp(None) == fp(_scope("", True))
    # ...and the scope terms are the bulk forms' only: a per-file form's header ignores a folder.
    row = _population_fingerprint(
        collection, _Population(scope="accept-file", file_id=SUBJECT_ID, **common)
    )
    row_with_folder = _population_fingerprint(
        collection,
        _Population(scope="accept-file", file_id=SUBJECT_ID, folder=_scope("a"), **common),
    )
    assert row == row_with_folder and row not in prints


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
@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_bulk_form_is_refused_when_its_own_set_gains_or_loses_a_file(cairn_env, form, drift):
    """web-panel "A scoped action is refused after its own population goes stale", both verbs.

    The loud half is `stop-tracking` + `enters`: the page listed one missing file, a scan records
    a second, and the already-rendered "Accept 1 deletion" must not remove a record — either
    one — or close an alert.
    """
    root = cairn_env / "own-drift"
    root.mkdir()
    status = FORM_STATUS[form]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issue(s, cid, "listed", status))
        await _with_session(lambda s: _mk_files(s, cid, [("steady", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        fp = _fp_for(form)

        async def move(s):
            from src.models.db import FileEntry

            relpath = "steady" if drift == "enters" else "listed"
            fe = await s.scalar(select(FileEntry).where(FileEntry.relpath == relpath))
            fe.status = status if drift == "enters" else "ok"
            await s.commit()
            if drift == "enters":
                await _mk_event(s, 1, fe.id, status)  # the scan that recorded it opened its alert

        _aside(move)
        before = _aside(_snapshot)
        _assert_refused(_post(client, _target(form), fp, _csrf(client)))

    rows = dict((p, st) for st, p in _aside(lambda s: _statuses(s, 1)))
    if drift == "enters":
        assert rows == {"listed": status, "steady": status}
        assert _aside(_open_count) == 2  # neither alert was closed
    else:
        assert rows == {"listed": "ok", "steady": "ok"}
    assert _aside(_snapshot) == before


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
        # The same non-disclosure for every accept address — carrying the fingerprint that IS
        # current for that collection, so the only thing standing in the way is the ownership check.
        before = _aside(_snapshot)
        for form in FORMS:
            fp = _fp_for(form, cid=2, fid=other_id)
            r = _post(client, _target(form, cid=2, fid=other_id), fp, token, dir="", deep="1")
            assert r.status_code == 404, form
            assert "location" not in r.headers, form
        assert _aside(_snapshot) == before

    assert _aside(lambda s: _statuses(s, 2)) == [("missing", "theirs")]


# --- D14 (post-audit): the render and the mint come from ONE fetch ---------------------------


def _drift_after_the_population_read(monkeypatch, drift):
    """Patch `_read_population` into a barrier: it does the real read, then lets `drift` commit.

    This is the interleaving a two-SELECT page cannot survive — `SELECT` does not open a
    transaction under Python's legacy sqlite3 transaction control, so a scanner committing here
    would land in a *second* read but not the first. Returns the recorder: one entry per call, each
    `(file_id, collection-stand-in, the population that was returned)`, so the test can re-derive
    any form's fingerprint from the snapshot the page actually rendered.
    """
    from types import SimpleNamespace

    from src.control_panel import routes

    seen: list[tuple] = []
    real = routes._read_population

    async def spy(session, collection, file_id=None):
        pop = await real(session, collection, file_id=file_id)
        seen.append(
            (file_id, SimpleNamespace(id=collection.id, created_at=collection.created_at), pop)
        )
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
    operator never saw. With three kinds of fingerprint on this page, it also pins that no two
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
        await _mk_issue(s, 1, "unseen", "missing")

    with _make_client(cairn_env, seed) as client:
        seen = _drift_after_the_population_read(monkeypatch, scan_lands)
        with _capture_context() as captured:
            body = client.get("/collection/1/review").text

        # Exactly one population read served the whole render — the whole collection's, not a
        # per-row read.
        assert [file_id for file_id, _, _ in seen] == [None]
        _, coll, pop = seen[0]
        ctx = [c for name, c in captured if name == "collection_review.html"][0]
        r = ctx["r"]

        # ...and every published fingerprint is a pure slice of that snapshot, not a re-read one.
        def slice_fp(form, file_id=None, folder=None):
            return routes._population_fingerprint(
                coll, routes._narrow(pop, form, file_id=file_id, folder=folder)
            )

        assert r["accept_deletions"]["fp"] == slice_fp("stop-tracking")
        # Nothing is changed here, so no accept-changes control exists to carry a fingerprint.
        assert r["accept_changes"] is None
        assert r["rows"][0]["fp"] == slice_fp("accept-file", file_id=SUBJECT_ID)
        assert f'name="population_fp" value="{r["accept_deletions"]["fp"]}"' in body
        # The row that landed after the single fetch is in neither half: not rendered, not hashed,
        # and not counted by the control that would remove it.
        assert "seen" in body and "unseen" not in body
        assert r["total"] == 1 and r["shown"] == 1 and r["accept_deletions"]["count"] == 1
        assert "Accept 1 deletion</button>" in body
        assert [it["relpath"] for it in r["rows"]] == ["seen"]
        # The open-event component is read in the same statement, so the alert that opened after
        # it is outside the hash too: the row's event is the one the snapshot held.
        assert [e.id for e in pop.open_events] == [1]
        assert r["rows"][0]["event_id"] == 1
        # The copy list is the same slice.
        assert ctx["copy_relpaths"] == "seen" and ctx["copy_count"] == 1

        # And the drift is caught where it must be: at submit time, under the write lock. The new
        # missing row joined the stop-tracking population, so that form is refused...
        token = _csrf(client)
        _assert_refused(_post(client, _target("stop-tracking"), r["accept_deletions"]["fp"], token))
        assert _aside(lambda s: _statuses(s, 1)) == [("missing", "seen"), ("missing", "unseen")]
        assert _aside(_open_count) == 2
        # ...while the per-file form for the row that never moved is still good.
        _assert_performed(
            _post(client, _target("accept-file"), r["rows"][0]["fp"], token), "accept-file"
        )

    assert _aside(lambda s: _statuses(s, 1)) == [("missing", "unseen")]
    assert _aside(_open_count) == 1  # the unseen file's alert was never closed


def test_every_group_action_is_a_slice_of_the_one_snapshot(cairn_env, monkeypatch):
    """The same contract for the folder groups, each of which carries its own count and form.

    A grouped page publishes a fingerprint per group per kind, on top of the page-wide ones and
    the rows'. All of them — and every count a control states — come from the single read; and
    the drift that lands after it refuses exactly the controls whose population it joined: the
    group it landed in and the page-wide control, never the neighbouring group.
    """
    from src.control_panel import routes

    root = cairn_env / "mintgroups"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        for relpath in ("a/one", "a/two", "b/one"):
            await _with_session(lambda s, r=relpath: _mk_issue(s, cid, r, "missing"))
        await _with_session(lambda s: _mk_issue(s, cid, "b/edited", "modified"))

    async def scan_lands(s):
        await _mk_issue(s, 1, "a/three", "missing")

    with _make_client(cairn_env, seed) as client:
        seen = _drift_after_the_population_read(monkeypatch, scan_lands)
        with _capture_context() as captured:
            body = client.get("/collection/1/review").text

        assert len(seen) == 1
        _, coll, pop = seen[0]
        r = [c for name, c in captured if name == "collection_review.html"][0]["r"]

        def slice_fp(form, folder=None):
            return routes._population_fingerprint(coll, routes._narrow(pop, form, folder=folder))

        assert r["flat"] is False
        groups = {g["dir"]: g for g in r["groups"]}
        assert sorted(groups) == ["a", "b"]
        a, b = groups["a"], groups["b"]
        # Counts are the snapshot's: `a/three` landed after the read and is in none of them.
        assert (a["total"], a["missing"], a["modified"]) == (2, 2, 0)
        assert (b["total"], b["missing"], b["modified"]) == (2, 1, 1)
        assert a["accept_changes"] is None  # an empty population is not offered an action
        assert a["accept_deletions"]["count"] == 2
        assert b["accept_deletions"]["count"] == 1 and b["accept_changes"]["count"] == 1
        assert r["accept_deletions"]["count"] == 3 and r["accept_changes"]["count"] == 1
        # Every fingerprint is the slice of that one snapshot at the scope its control names.
        assert a["accept_deletions"]["fp"] == slice_fp("stop-tracking", _scope("a"))
        assert b["accept_deletions"]["fp"] == slice_fp("stop-tracking", _scope("b"))
        assert b["accept_changes"]["fp"] == slice_fp("adopt-changed", _scope("b"))
        assert r["accept_deletions"]["fp"] == slice_fp("stop-tracking", _scope(""))
        assert r["accept_changes"]["fp"] == slice_fp("adopt-changed", _scope(""))
        for g in (a, b):
            for action in (g["accept_changes"], g["accept_deletions"]):
                if action is not None:
                    assert (action["dir"], action["deep"]) == (g["dir"], "1")
                    assert f'name="population_fp" value="{action["fp"]}"' in body
        assert "a/three" not in body and "three" not in [it["name"] for it in r["rows"]]
        assert "Accept 2 deletions</button>" in body and "Accept 3 deletions</button>" in body

        token = _csrf(client)
        stop = _target("stop-tracking")
        before = _aside(_snapshot)
        # The newcomer joined group `a` and the page-wide population: both are refused...
        _assert_refused(_post(client, stop, a["accept_deletions"]["fp"], token, dir="a", deep="1"))
        _assert_refused(_post(client, stop, r["accept_deletions"]["fp"], token, dir="", deep="1"))
        assert _aside(_snapshot) == before
        # ...while group `b`, which it did not reach, still acts — on `b` only.
        _assert_performed(
            _post(client, stop, b["accept_deletions"]["fp"], token, dir="b", deep="1")
        )

    assert _aside(lambda s: _statuses(s, 1)) == [
        ("missing", "a/one"), ("missing", "a/three"), ("missing", "a/two"),
        ("modified", "b/edited"),
    ]
    assert _aside(_open_count) == 4  # only `b/one`'s alert was closed


def test_the_dashboard_and_the_detail_page_mint_no_fingerprint(cairn_env):
    """Neither page carries an accept form, so neither publishes anything that could authorize one.

    The needs-you section reads each affected collection's population to order and describe it,
    and renders links only (design D10).
    """
    root = cairn_env / "nomint"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issue(s, cid, "a/gone", "missing"))
        await _with_session(lambda s: _mk_issue(s, cid, "b/edited", "modified"))

    with _make_client(cairn_env, seed) as client:
        for url in ("/", "/collection/1", "/collections"):
            body = client.get(url).text
            assert "population_fp" not in body, url
            assert not re.search(r"\b[0-9a-f]{64}\b", body.split("</head>", 1)[1]), url


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
    """An unresolved changed restore reads "1 changed" on every surface, in BOTH modes.

    `_collection_status` derives the pill from `files.status` alone, and churn is the case worth
    pinning: an ordinary churn edit re-baselines to `ok` silently, so if the changed-restore row
    were ever handled like one, the collection would go on reading green while an unresolved
    `restored_changed` event sat under it — the alarm raised and the operator never shown it.
    With the event count gone from the panel (unify-issue-model D1) the file's status is the ONLY
    thing that keeps it in view, so it must also be listed where it can be resolved.
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
        with _capture_context() as captured:
            review = client.get("/collection/1/review").text
        rows = [c for name, c in captured if name == "collection_review.html"][0]["r"]["rows"]

    assert observed["counts"]["modified"] == 1, "the restored row is still on record as changed"
    assert observed["status"] == "attention", (
        f"{mode}: a file that came back with different bytes leaves the collection needing a look"
    )
    for name, body in (("dashboard", dash), ("detail", detail), ("op-status", frag)):
        assert "All clear" not in body, name
        # The pill states the fact, not a grade (design D11).
        assert "1 changed" in body, name
        assert "Attention" not in body, name
    # It counts as an issue like any other changed file...
    assert _badge_number(dash) == 1 and _needs_total(dash) == 1
    # ...and it is listed for review under its own label, with an action to resolve it.
    assert [(it["relpath"], it["kind"], it["status"]) for it in rows] == [
        ("deed.pdf", "came_back", "modified")
    ]
    assert "Came back different" in review
    assert "Nothing is changed or missing" not in review
    assert f'name="population_fp" value="{rows[0]["fp"]}"' in review


# =============================================================================================
# UX audit sprint 2 (#38, #40): a fleet-wide control says how far it reaches, and each figure
# names what it measures (#37's new-file count is retired — see below)
# =============================================================================================


# #37's "New files — watched, not yet baselined" stat (and the dashboard tile beside it) is
# retired by unify-issue-model D4: a new file is watched from the scan that found it, and the
# panel no longer presents the baseline as work. What replaces those tests is the opposite claim.


@pytest.mark.parametrize("ots_mode", ["perfile", "none"])
def test_new_files_are_not_work_on_any_surface(cairn_env, ots_mode):
    """web-panel "New files are not work": a collection whose only non-`ok` files are new reads
    all clear, and no page describes them as waiting for a baseline or offers an action on them."""
    root = cairn_env / "newfiles"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root, ots_mode=ots_mode)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("kept.txt", "ok", "complete" if ots_mode == "perfile" else "none"),
            ("fresh-a.txt", "new", "complete" if ots_mode == "perfile" else "none"),
            ("fresh-b.txt", "new", "pending" if ots_mode == "perfile" else "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        pages = {
            url: client.get(url).text
            for url in ("/", "/collections", "/collection/1", "/collection/1/op-status",
                        "/collection/1/review", "/review")
        }

    for url, body in pages.items():
        assert "not yet baselined" not in body, url
        assert "Baseline " not in body and "baseline new" not in body.lower(), url
        assert 'name="population_fp"' not in body, url
        # A new file is not described as notarized on the strength of being new, either.
        assert "new files are notarized" not in body.lower(), url
    for url in ("/", "/collections", "/collection/1", "/collection/1/op-status"):
        assert "All clear" in pages[url], url
    # No count of outstanding work exists for them: no badge, no needs-you total, no review link.
    dash = pages["/"]
    assert _badge_number(dash) is None and _needs_total(dash) is None
    assert "No files are changed or missing" in _section(dash, "needs-you")
    assert "Nothing is changed or missing" in pages["/collection/1/review"]
    assert "No open issues" in pages["/review"]
    # The stat row no longer carries a "New files" tile; the files stay listable via the filter.
    detail = pages["/collection/1"]
    stat_row = detail.split('class="stat-row"')[1].split('class="card card--flush browser"')[0]
    assert "New files" not in stat_row and "Matching baseline" not in stat_row
    assert 'name="filter" value="new"' in detail
    # The card still DESCRIBES them (it does not ask): the "N new" legend survives.
    assert "2 new</span>" in dash


# --- #38: the fleet-wide scan control states its scope ----------------------------------------


def test_the_scan_control_names_its_scope_and_the_collection_count(cairn_env):
    a, b = cairn_env / "a", cairn_env / "b"
    a.mkdir()
    b.mkdir()

    async def seed():
        for root in (a, b):
            cid = await seed_collection(root)
            await _with_session(lambda s, cid=cid: _mk_files(s, cid, [("f.txt", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    assert "Scan all collections" in html
    assert "Run scan now" not in html
    assert "confirm('Scan all 2 collections now?')" in html
    # The POST itself is untouched — the confirm is a statement of scope, not a new guard. The
    # count is a render-time statement, so the form carries no fingerprint binding it (design D5):
    # a scan is read-only detection, and scanning a collection added since render is what the
    # operator wants.
    form = html.split('action="/scan"')[1].split("</form>")[0]
    assert form.count('type="hidden"') == 1 and 'name="csrf_token"' in form
    assert "fingerprint" not in form


def test_the_scan_confirm_counts_one_collection_in_the_singular(cairn_env):
    root = cairn_env / "solo"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("f.txt", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    assert "confirm('Scan all 1 collection now?')" in html


# --- #40: the status bar is labelled and the coverage ratio names its denominator -------------


def test_the_segbar_is_labelled_with_the_counts_it_is_sized_from(cairn_env):
    root = cairn_env / "labelled"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a.txt", "ok", "complete"),
            ("b.txt", "ok", "complete"),
            ("c.txt", "new", "pending"),
            ("d.txt", "modified", "complete"),
            ("e.txt", "missing", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    label = "File status: 2 ok, 1 new, 1 modified, 1 missing"
    assert f'role="img" title="{label}" aria-label="{label}"' in html
    # The label's counts are the values the segments are sized from — same `c.counts`, one source.
    segbar = html.split('class="segbar"')[1].split("</div>\n    <div class=\"legend\"")[0]
    assert "width:40.0%" in segbar  # 2 of 5 ok
    assert segbar.count("segbar__seg") == 4


def test_a_status_whose_share_rounds_to_zero_still_gets_pixels(cairn_env):
    """One modified file in a very large collection must not render an all-green bar.

    The bar would then contradict its own label, which is the #40 misread in miniature.
    """
    root = cairn_env / "sliver"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        specs = [(f"ok-{i}.txt", "ok", "complete") for i in range(30000)]
        specs.append(("changed.txt", "modified", "complete"))
        await _with_session(lambda s: _mk_files(s, cid, specs))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    assert "1 modified" in html
    # The segment is rendered (its width rounds to 0.0%), and CSS floors every segment's width.
    segbar = html.split('class="segbar"')[1].split("</div>\n    <div class=\"legend\"")[0]
    assert "width:0.0%;background:var(--warn)" in segbar
    css = (
        Path(__file__).resolve().parents[1]
        / "src/control_panel/static/css/panel.css"
    ).read_text()
    assert ".segbar__seg { min-width: 3px; }" in css


def test_the_anchored_ratio_names_the_population_its_denominator_counts(cairn_env):
    root = cairn_env / "ratio"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a.txt", "ok", "complete"),
            ("b.txt", "new", "none"),
            ("gone.txt", "missing", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    # `stampable` excludes the missing file (sprint-1 D5); the wording now says which files it counts.
    assert "1 / 2</strong> present files anchored" in html
    assert "</strong> anchored ·" not in html


def test_the_all_confirmed_claim_names_its_population_when_files_are_missing(cairn_env):
    """The Max-Documents reading of #40: "all confirmed" over a count below the card's own total."""
    root = cairn_env / "allconfirmed"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a.txt", "ok", "complete"),
            ("b.txt", "ok", "complete"),
            ("gone.txt", "missing", "complete"),
        ]))

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    assert "all <strong style=\"color:var(--text)\">2</strong> present files anchored" in html
    assert "all confirmed" not in html.split('class="collection-card__footer"')[1]


# =============================================================================================
# unify-issue-model D3: the retired addresses answer not-found and mutate nothing
# =============================================================================================
#
# The acknowledge-only verbs (one event, all of a user's events, all of a collection's events) and
# the baseline verb are deleted, not redirected. A tab left open across the deploy still has their
# buttons; pressing one must do nothing at all.

RETIRED_ADDRESSES = [
    "/events/1/ack",
    "/events/1/ack?view=review",
    "/events/2/ack",
    "/events/ack-all",
    "/collection/1/review/ack-all",
    "/collection/1/accept",
    # Retired one change earlier (split-accept-into-scoped-verbs) and still gone.
    "/collection/1/review/accept",
]


@pytest.mark.parametrize("address", RETIRED_ADDRESSES)
def test_a_retired_address_answers_not_found_and_mutates_nothing(cairn_env, address):
    """web-panel "The retired addresses mutate nothing" — with a VALID CSRF token.

    A 403 from the CSRF check would also "mutate nothing", and would prove nothing about the
    route: the request here is one the panel would have honoured yesterday, token included, and it
    carries a fingerprint that is currently valid for a real form of this collection.
    """
    root = cairn_env / "retired"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issue(s, cid, "gone", "missing", id=SUBJECT_ID))
        await _with_session(lambda s: _mk_issue(s, cid, "edited", "modified"))
        await _with_session(lambda s: _mk_files(s, cid, [
            ("fresh", "new", "complete"), ("kept", "ok", "complete"),
        ]))
        await _with_session(lambda s: _mk_event(s, cid, None, "missing"))  # a detached open alert

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        before = _aside(_snapshot)
        assert _aside(_open_count) == 3
        for fp in (None, _fp_for("stop-tracking"), _fp_for("accept-file")):
            data = {"csrf_token": token}
            if fp is not None:
                data["population_fp"] = fp
            r = client.post(
                address, data=data, headers={"X-CSRF-Token": token}, follow_redirects=False
            )
            assert r.status_code in (404, 405), (address, r.status_code)
            assert "location" not in r.headers  # nothing forwards to a surviving verb
            # No event acknowledged, no file status changed, no row deleted.
            assert _aside(_snapshot) == before, address

    assert _aside(lambda s: _statuses(s, 1)) == [
        ("missing", "gone"), ("modified", "edited"), ("new", "fresh"), ("ok", "kept"),
    ]
    assert _aside(_open_count) == 3


def test_no_route_acknowledges_an_event_or_baselines_a_collection(cairn_env):
    """The same statement about the route table itself, so a re-added route cannot hide behind
    an id the parametrized requests above happened not to use."""
    from src.main import app

    # Both views of the table: the published schema, and the route objects themselves (walked
    # through included routers), so a route hidden from the schema is still seen.
    found = {path for path, ops in app.openapi()["paths"].items() if "post" in ops}
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if "POST" in (getattr(route, "methods", None) or ()) and getattr(route, "path", None):
            found.add(route.path)
        inner = getattr(route, "original_router", None)
        stack.extend(getattr(route, "routes", None) or getattr(inner, "routes", None) or [])
    posts = sorted(found)
    assert "/collection/{collection_id}/scan" in posts, "the route table was not read"
    assert any(p.startswith("/corpus") for p in posts), "included routers were not walked"
    assert not [p for p in posts if p.startswith("/events")], posts
    assert not [p for p in posts if "ack" in re.split(r"[/\-]", p)], posts
    # Exactly three routes accept anything, and each performs one constant verb.
    accepting = [p for p in posts if "accept" in p or "adopt" in p or "stop-tracking" in p]
    assert accepting == [
        "/collection/{collection_id}/file/{file_id}/accept",
        "/collection/{collection_id}/review/adopt-changed",
        "/collection/{collection_id}/review/stop-tracking",
    ]


# =============================================================================================
# unify-issue-model D6: the folder scope of the two bulk verbs
# =============================================================================================
#
# A bulk form now names a folder — `dir`, and whether it means the whole subtree (`deep=1`) or
# only the files directly in it (`deep=0`). Those are operator-controlled bytes on a destructive
# endpoint, so every test below is about the same property from a different side: the scope
# selects nothing by itself. It is bound into the fingerprint, recomputed against under the write
# lock, and the records mutated are the ones that recount hashed.


def _bulk(client, form, fp, token, scope, **fields):
    """Submit a bulk form naming ``scope``, with the fields a rendered control carries."""
    return _post(
        client, _target(form), fp, token,
        dir=scope.dir, deep="1" if scope.deep else "0", **fields,
    )


async def _mk_issues(session, cid, status, relpaths):
    for relpath in relpaths:
        await _mk_issue(session, cid, relpath, status)


async def _open_alert_paths(session):
    """The relpaths of the files that still have an open alert attached."""
    from src.models.db import Event, FileEntry

    rows = await session.execute(
        select(FileEntry.relpath)
        .join(Event, Event.file_id == FileEntry.id)
        .where(Event.acknowledged_at.is_(None))
    )
    return sorted(r for (r,) in rows.all())


def _other_kind(form: str) -> str:
    return "modified" if FORM_STATUS[form] == "missing" else "missing"


def _rows_after(form, acted, untouched, others=()):
    """What `_statuses` reads once ``form`` has acted on ``acted`` and on nothing else."""
    rows = [(FORM_STATUS[form], p) for p in untouched] + list(others)
    if form == "adopt-changed":
        rows += [("ok", p) for p in acted]  # a deletion's record is gone; a change is set healthy
    return sorted(rows)


@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_folder_scoped_action_acts_only_inside_its_folder(cairn_env, form):
    """web-panel "A folder-scoped action acts only inside its folder"."""
    root = cairn_env / "folder"
    root.mkdir()
    inside = ["a/deep/two", "a/one"]
    outside = ["aa", "ab/one", "b/one"]  # another folder, a look-alike sibling, a top-level file
    other = _other_kind(form)

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], inside + outside))
        # A file of the OTHER kind inside the very folder being acted on: same folder, not this
        # verb's population.
        await _with_session(lambda s: _mk_issue(s, cid, "a/other-kind", other))

    with _make_client(cairn_env, seed) as client:
        scope = _scope("a")
        assert _narrowed_relpaths(1, form, scope) == inside
        _assert_performed(_bulk(client, form, _fp_for(form, folder=scope), _csrf(client), scope))

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(
        form, inside, outside, [(other, "a/other-kind")]
    )
    # Only the acted-on files' alerts were closed: every other one is still open, still attached.
    assert _aside(_open_alert_paths) == sorted(outside + ["a/other-kind"])
    assert _aside(_open_count) == len(outside) + 1


@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_folders_fingerprint_does_not_validate_another_scope(cairn_env, form):
    """web-panel "A folder's fingerprint does not validate another folder" — different files."""
    root = cairn_env / "otherscope"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(
            s, cid, FORM_STATUS[form], ["a/sub/y", "a/x", "b/z", "top"]
        ))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        minted = _fp_for(form, folder=_scope("a"))
        before = _aside(_snapshot)
        for named in (
            _scope("b"),            # a different folder
            _scope(""),             # the whole collection
            _scope("a", False),     # the same folder, subtree/direct flipped
            _scope("a/sub"),        # a folder inside it
            _scope("", False),      # only the top-level files
            _scope("A"),            # a different folder that differs only by case
        ):
            _assert_refused(_bulk(client, form, minted, token, named))
            assert _aside(_snapshot) == before, named

    assert _aside(_open_count) == 4


# The three scopes that hold EXACTLY the same files once every issue lives directly under `a/`.
SAME_FILES_SCOPES = [("", True), ("a", True), ("a", False)]


@pytest.mark.parametrize("form", BULK_FORMS)
def test_cross_scope_replay_is_refused_where_the_scopes_hold_exactly_the_same_files(
    cairn_env, form
):
    """The half of that scenario a population check cannot see.

    Every issue sits directly in `a/`, so the whole collection, `a/`'s subtree and "the files
    directly in `a/`" are the same records and the same alerts. A fingerprint over the population
    alone would be identical for all three; the scope named in the header is what keeps a form
    rendered for one from validating a submission naming another. All six ordered pairs.
    """
    root = cairn_env / "samefiles"
    root.mkdir()
    files = ["a/x", "a/y"]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], files))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        scopes = [_scope(d, deep) for d, deep in SAME_FILES_SCOPES]
        # The premise: byte-identical populations...
        for s in scopes:
            assert _narrowed_relpaths(1, form, s) == files, s
        # ...and three different fingerprints.
        prints = {s: _fp_for(form, folder=s) for s in scopes}
        assert len(set(prints.values())) == 3

        before = _aside(_snapshot)
        pairs = [(m, n) for m in scopes for n in scopes if m != n]
        assert len(pairs) == 6
        for minted, named in pairs:
            _assert_refused(_bulk(client, form, prints[minted], token, named))
            assert _aside(_snapshot) == before, (minted, named)

    assert _aside(lambda s: _statuses(s, 1)) == [(FORM_STATUS[form], p) for p in files]
    assert _aside(_open_count) == 2


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("dir,deep", SAME_FILES_SCOPES)
def test_each_of_those_scopes_accepts_its_own_fingerprint(cairn_env, form, dir, deep):
    """...and the guard is not simply refusing every scoped submission."""
    root = cairn_env / "samefiles-own"
    root.mkdir()
    files = ["a/x", "a/y"]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], files))

    with _make_client(cairn_env, seed) as client:
        scope = _scope(dir, deep)
        _assert_performed(_bulk(client, form, _fp_for(form, folder=scope), _csrf(client), scope))

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(form, files, [])
    assert _aside(_open_count) == 0


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("newcomer", ["a/two", "a/sub/deeper/two"])
def test_a_folder_scoped_action_is_refused_by_drift_inside_its_folder(cairn_env, form, newcomer):
    """web-panel "A folder-scoped action is refused by drift inside its folder only" (first half).

    The page listed `a/one`; a scan then records another file of that kind under `a/` — directly,
    or any depth below. The already-rendered "Accept 1 …" for `a/` must not act on a file it never
    listed, nor on the one it did.
    """
    root = cairn_env / "drift-in"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], ["a/one", "b/one"]))

    with _make_client(cairn_env, seed) as client:
        scope = _scope("a")
        fp = _fp_for(form, folder=scope)
        _aside(lambda s: _mk_issue(s, 1, newcomer, FORM_STATUS[form]))
        before = _aside(_snapshot)
        _assert_refused(_bulk(client, form, fp, _csrf(client), scope))

    assert _aside(_snapshot) == before
    assert _aside(lambda s: _statuses(s, 1)) == sorted(
        (FORM_STATUS[form], p) for p in ("a/one", "b/one", newcomer)
    )
    assert _aside(_open_count) == 3


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize(
    "newcomer,kind",
    [
        ("b/two", "same"),          # a different folder
        ("ab/two", "same"),         # a sibling whose name merely starts with the folder's
        ("two", "same"),            # the collection's top level
        ("a/other", "other"),       # INSIDE the folder, but the kind this verb does not act on
    ],
)
def test_a_folder_scoped_action_is_not_refused_by_drift_outside_its_folder(
    cairn_env, form, newcomer, kind
):
    """...and the second half: drift the folder's own population never saw refuses nothing.

    The action is performed on the folder it was rendered for, and the newcomer — and its newly
    opened alert — is left exactly as the scan recorded it.
    """
    root = cairn_env / "drift-out"
    root.mkdir()
    newcomer_status = FORM_STATUS[form] if kind == "same" else _other_kind(form)

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], ["a/one", "b/one"]))

    with _make_client(cairn_env, seed) as client:
        scope = _scope("a")
        fp = _fp_for(form, folder=scope)
        _aside(lambda s: _mk_issue(s, 1, newcomer, newcomer_status))
        _assert_performed(_bulk(client, form, fp, _csrf(client), scope))

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(
        form, ["a/one"], ["b/one"], [(newcomer_status, newcomer)]
    )
    assert _aside(_open_alert_paths) == sorted(["b/one", newcomer])
    assert _aside(_open_count) == 2


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("dir", ["a", ""])
def test_a_direct_files_scope_leaves_its_subfolders_alone(cairn_env, form, dir):
    """web-panel "A scope of the files directly in a folder leaves its subfolders alone".

    Run for a named folder and for the collection's top level (`dir=""`, `deep=0`), which is the
    scope a "top level (not in any folder)" group carries.
    """
    root = cairn_env / "direct"
    root.mkdir()
    files = ["a/one", "a/sub/deep", "a/sub/deeper/still", "a/two", "b/x", "top1", "top2"]
    direct = ["a/one", "a/two"] if dir == "a" else ["top1", "top2"]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], files))

    with _make_client(cairn_env, seed) as client:
        scope = _scope(dir, False)
        assert _narrowed_relpaths(1, form, scope) == direct
        _assert_performed(_bulk(client, form, _fp_for(form, folder=scope), _csrf(client), scope))

    untouched = [p for p in files if p not in direct]
    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(form, direct, untouched)
    assert _aside(_open_alert_paths) == sorted(untouched)


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("newcomer,refused", [("a/three", True), ("a/sub/three", False)])
def test_a_direct_files_scope_is_refused_by_drift_directly_in_it_only(
    cairn_env, form, newcomer, refused
):
    """The direct-files scope's own boundary, from the drift side: a file landing in a subfolder
    never joined its population, a file landing directly in the folder did."""
    root = cairn_env / "direct-drift"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(
            s, cid, FORM_STATUS[form], ["a/one", "a/sub/deep"]
        ))

    with _make_client(cairn_env, seed) as client:
        scope = _scope("a", False)
        fp = _fp_for(form, folder=scope)
        _aside(lambda s: _mk_issue(s, 1, newcomer, FORM_STATUS[form]))
        before = _aside(_snapshot)
        r = _bulk(client, form, fp, _csrf(client), scope)
        if refused:
            _assert_refused(r)
            assert _aside(_snapshot) == before
        else:
            _assert_performed(r)

    if not refused:
        assert _aside(lambda s: _statuses(s, 1)) == _rows_after(
            form, ["a/one"], ["a/sub/deep", newcomer]
        )
        assert _aside(_open_alert_paths) == sorted(["a/sub/deep", newcomer])


# (the folder acted on, the files under exactly that folder, the files a pattern match — `LIKE`
# with `%` / `_`, a bare prefix test, or a case-insensitive compare — would wrongly sweep in)
LITERAL_NAMES = [
    ("100%", ["100%/f"], ["100/f", "1000/f", "100x/y/f", "100%25/f"]),
    ("%", ["%/f"], ["anything/f", "x/y/f", "%%/f"]),
    ("a_b", ["a_b/f", "a_b/g/h"], ["axb/f", "a/b/f", "ab/f", "a__b/f"]),
    ("axb", ["axb/f"], ["a_b/f", "a%b/f"]),
    ("_", ["_/f"], ["a/f", "b/f", "__/f"]),
    ("foo", ["foo/bar/g", "foo/f"], ["foobar/f", "foo.d/f", "fo/f", "foo-/f", "foo.txt"]),
    ("foobar", ["foobar/f"], ["foo/f", "foo/bar/f", "foobar2/f"]),
    ("docs", ["docs/f"], ["DOCS/f", "Docs/f"]),
    ("a\\b", ["a\\b/f"], ["a/b/f", "ab/f", "a\\\\b/f"]),
]


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("folder,own,lookalikes", LITERAL_NAMES, ids=[n[0] for n in LITERAL_NAMES])
def test_a_folder_name_is_matched_literally(cairn_env, form, folder, own, lookalikes):
    """web-panel "A folder name is matched literally".

    Real folders are called `100%` and `my_notes`. Under pattern matching `100%` would also take
    `1000/` and `a_b` would take `axb/`; under a bare prefix test `foo` would take `foobar/`; and
    SQLite's `LIKE` is case-insensitive for ASCII. Each look-alike here is a folder such a rule
    would wrongly include, and every one of them must survive with its alert open.
    """
    root = cairn_env / "literal"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], own + lookalikes))

    with _make_client(cairn_env, seed) as client:
        scope = _scope(folder)
        assert _narrowed_relpaths(1, form, scope) == sorted(own)
        r = _bulk(
            client, form, _fp_for(form, folder=scope), _csrf(client), scope,
            return_dir=folder, return_deep="1",
        )
        # Performed — and returned to the folder it was working in, with the name carried only as
        # one percent-encoded query value.
        assert r.status_code == 303
        location = r.headers["location"]
        parts = urlsplit(location)
        assert (parts.scheme, parts.netloc, parts.path) == ("", "", "/collection/1/review")
        assert parse_qs(parts.query, keep_blank_values=True, strict_parsing=True) == {
            "dir": [folder]
        }
        assert location == "/collection/1/review?" + urlencode({"dir": folder})

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(form, own, lookalikes)
    assert _aside(_open_alert_paths) == sorted(lookalikes)


LONG_DIR = "d" * 4097  # one byte over the limit
MULTIBYTE_DIR = "é" * 2049  # 2,049 characters, 4,098 bytes: the limit is in bytes


@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_malformed_scope_is_refused_not_interpreted(cairn_env, form):
    """web-panel "A malformed scope is refused, not interpreted".

    For every malformed submission the fingerprints tried are the ones most likely to be let
    through by a parser that *repairs* its input: the fingerprint of the nearest well-formed scope
    (what stripping the slash, dropping the NUL or clamping the flag would produce), and the
    fingerprint computed for the raw scope exactly as submitted — which, for the over-long folder,
    covers a real record. None may act.
    """
    root = cairn_env / "malformed"
    root.mkdir()
    status = FORM_STATUS[form]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(
            s, cid, status, ["a/x", "a/sub/y", "top", f"{LONG_DIR}/f"]
        ))

    def fp(dir, deep=True):
        return _fp_for(form, folder=_scope(dir, deep))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        # The premise for the over-long case: its raw scope names a real, non-empty population.
        assert _narrowed_relpaths(1, form, _scope(LONG_DIR)) == [f"{LONG_DIR}/f"]
        cases = [
            # (submitted dir, submitted deep, fingerprints tried)
            ("/a", "1", [fp("a"), fp("/a")]),                      # absolute
            ("/", "1", [fp(""), fp("/")]),
            ("//a", "1", [fp("a"), fp("//a")]),
            ("a/", "1", [fp("a"), fp("a/")]),                      # trailing slash
            ("a/sub/", "0", [fp("a/sub", False), fp("a/sub/", False)]),
            ("a\x00", "1", [fp("a"), fp("a\x00")]),                # NUL
            ("\x00", "1", [fp(""), fp("\x00")]),
            ("a\x00/sub", "1", [fp("a/sub"), fp("a"), fp("a\x00/sub")]),
            (LONG_DIR, "1", [fp(LONG_DIR), fp(""), fp(LONG_DIR[:4096])]),  # over-long
            ("d" * 5000, "1", [fp("d" * 5000), fp("")]),
            (MULTIBYTE_DIR, "1", [fp(MULTIBYTE_DIR), fp("")]),
            ("a", "2", [fp("a"), fp("a", False)]),                 # a flag no page renders
            ("a", "true", [fp("a"), fp("a", False)]),
            ("a", "01", [fp("a"), fp("a", False)]),
            ("a", " 1", [fp("a"), fp("a", False)]),
            ("a", "1 ", [fp("a"), fp("a", False)]),
            ("a", "-1", [fp("a"), fp("a", False)]),
            ("", "2", [fp(""), fp("", False)]),
        ]
        before = _aside(_snapshot)
        for dir, deep, candidates in cases:
            for candidate in candidates:
                r = _post(client, _target(form), candidate, token, dir=dir, deep=deep)
                # Refused exactly as a fingerprint mismatch is: same redirect, same marker.
                _assert_refused(r)
                assert _aside(_snapshot) == before, (dir[:20], deep)

    assert _aside(_open_count) == 4


@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_folder_at_the_length_limit_is_still_a_folder(cairn_env, form):
    """The other side of the over-long refusal: 4,096 bytes is a scope, 4,097 is not."""
    root = cairn_env / "limit"
    root.mkdir()
    at_limit = "d" * 4096

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(
            s, cid, FORM_STATUS[form], [f"{at_limit}/f", "other/f"]
        ))

    with _make_client(cairn_env, seed) as client:
        scope = _scope(at_limit)
        _assert_performed(_bulk(client, form, _fp_for(form, folder=scope), _csrf(client), scope))

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(form, [f"{at_limit}/f"], ["other/f"])


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("blank_deep", [None, ""], ids=["absent", "blank"])
def test_an_absent_deep_field_only_ever_means_the_whole_subtree(cairn_env, form, blank_deep):
    """The route's default for a `deep` that was not submitted (or was submitted blank) is `"1"`.

    That default is safe only because it is bound like any submitted value: a submission without
    the field validates exactly the fingerprint minted for the SUBTREE scope and no other — in
    particular not the direct-files fingerprint, which would otherwise be widened to the subtree.
    """
    root = cairn_env / "nodeep"
    root.mkdir()
    files = ["a/sub/y", "a/x"]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, FORM_STATUS[form], files))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        before = _aside(_snapshot)
        direct = _fp_for(form, folder=_scope("a", False))
        # `deep` not submitted, and `deep` submitted blank (which the framework hands the route as
        # "not submitted"): neither validates the direct-files fingerprint.
        _assert_refused(_post(client, _target(form), direct, token, dir="a"))
        _assert_refused(_post(client, _target(form), direct, token, dir="a", deep=""))
        assert _aside(_snapshot) == before
        subtree = _fp_for(form, folder=_scope("a", True))
        _assert_performed(_post(client, _target(form), subtree, token, dir="a", deep=blank_deep))

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(form, files, [])


@pytest.mark.parametrize("form", BULK_FORMS)
def test_a_dir_that_names_no_files_acts_on_nothing(cairn_env, form):
    """A folder with no file of the verb's kind in it: whatever is submitted, nothing is mutated.

    Three submissions. A real folder's fingerprint naming the empty one, and the empty folder's
    fingerprint naming the real one, are ordinary mismatches. The third — the empty folder's own
    fingerprint, naming itself — is one no page can mint (an empty population is not offered an
    action); what is pinned for it is the property that matters: no record and no alert changes.
    """
    root = cairn_env / "nofiles"
    root.mkdir()
    status = FORM_STATUS[form]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, status, ["a/x"]))
        # `other/` exists, but holds only the kind this verb does not act on.
        await _with_session(lambda s: _mk_issue(s, cid, "other/f", _other_kind(form)))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        before = _aside(_snapshot)
        real, empty, wrong_kind = _scope("a"), _scope("nope"), _scope("other")
        assert _narrowed_relpaths(1, form, empty) == []
        assert _narrowed_relpaths(1, form, wrong_kind) == []

        _assert_refused(_bulk(client, form, _fp_for(form, folder=real), token, empty))
        _assert_refused(_bulk(client, form, _fp_for(form, folder=real), token, wrong_kind))
        _assert_refused(_bulk(client, form, _fp_for(form, folder=empty), token, real))
        _assert_refused(_bulk(client, form, _fp_for(form, folder=empty), token, wrong_kind))
        assert _aside(_snapshot) == before

        for scope in (empty, wrong_kind):
            r = _bulk(client, form, _fp_for(form, folder=scope), token, scope)
            assert r.status_code == 303
            assert urlsplit(r.headers["location"]).path == "/collection/1/review"
            assert _aside(_snapshot) == before, scope

        # And no page offers such a control in the first place.
        page = client.get("/collection/1/review?dir=nope").text
        assert 'name="population_fp"' not in page
        assert "Nothing to review in this folder" in page

    assert _aside(lambda s: _statuses(s, 1)) == sorted(
        [(status, "a/x"), (_other_kind(form), "other/f")]
    )
    assert _aside(_open_count) == 2


# --- D6: the redirect is built from a fixed path; the scope is only ever a query value -----------

AWKWARD_DIR = "fold er/ç&d=1"
AWKWARD_QUERY = "dir=fold+er%2F%C3%A7%26d%3D1"


@pytest.mark.parametrize("form", FORMS)
def test_an_accept_returns_to_the_scope_it_was_rendered_on(cairn_env, form):
    """Success and refusal both land back on the review scope the form was rendered on, with the
    folder carried URL-encoded as the `dir` value of the fixed review path."""
    root = cairn_env / "return"
    root.mkdir()
    base = "/collection/1/review"

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        url = _target(form)
        before = _aside(_snapshot)
        stale_fp = "0" * 64
        # (return_dir, return_deep) → the query the redirect carries.
        for return_dir, return_deep, query in [
            (AWKWARD_DIR, "0", f"{AWKWARD_QUERY}&deep=0"),
            (AWKWARD_DIR, "1", AWKWARD_QUERY),
            ("a", "1", "dir=a"),
            ("", "0", "dir=&deep=0"),  # the "top level (not in any folder)" group's own page
            ("", "1", ""),            # the whole collection: no scope to carry
            (None, None, ""),         # fields absent: the route's defaults are the whole collection
        ]:
            r = _post(client, url, stale_fp, token, return_dir=return_dir, return_deep=return_deep)
            _assert_refused(r, f"{base}?{query}&stale=1" if query else f"{base}?stale=1")
        assert _aside(_snapshot) == before

        r = _post(client, url, _fp_for(form), token, return_dir=AWKWARD_DIR, return_deep="0")
        _assert_performed(r, form, f"{base}?{AWKWARD_QUERY}&deep=0")
        assert r.headers["location"].isascii()
        assert parse_qs(urlsplit(r.headers["location"]).query, strict_parsing=True) == {
            "dir": [AWKWARD_DIR], "deep": ["0"],
        }

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(
        "stop-tracking" if FORM_STATUS[form] == "missing" else "adopt-changed", [SUBJECT], []
    )


HOSTILE_ORIGINS = [
    None, "", "//evil.example", "https://evil.example/x", "/review", "review", "fleet/", "FLEET",
    "fleet ", " fleet", "fleet\n", "x&stale=1", "\\\\evil.example", "javascript:alert(1)",
]
HOSTILE_RETURN_DIRS = [
    "//evil.example", "/\\evil.example", "https://evil.example/", "x&stale=1", "x&stale=0",
    "x?stale=1#frag", "..", "../../etc/passwd", "\\\\evil.example",
    "a\r\nLocation: https://evil.example", "x%26stale%3D1", "x&dir=y&deep=0", "@evil.example",
]


def _assert_safe_review_location(location: str, *, stale: bool, return_dir: str) -> None:
    """The redirect stays on the collection's own review path; `return_dir` is at most a value."""
    assert location.isascii() and "\r" not in location and "\n" not in location, location
    assert location.startswith("/collection/1/review") and not location.startswith("//"), location
    parts = urlsplit(location)
    assert (parts.scheme, parts.netloc, parts.fragment) == ("", "", ""), location
    assert parts.path == "/collection/1/review", location
    query = parse_qs(parts.query, keep_blank_values=True)
    assert set(query) <= {"dir", "deep", "stale"}, location
    # The staleness marker is the route's to set, never the submission's.
    assert query.get("stale", []) == (["1"] if stale else []), location
    # Where the folder is echoed at all, it is echoed whole, as ONE value.
    if "dir" in query:
        assert query["dir"] == [return_dir], location
    else:
        assert "evil" not in location, location
    assert query.get("deep", ["0"]) == ["0"], location


def _with_origin(url: str, origin) -> str:
    return url if origin is None else f"{url}?{urlencode({'from': origin})}"


@pytest.mark.parametrize("form", FORMS)
def test_no_submitted_value_changes_the_redirects_host_or_path(cairn_env, form):
    """web-panel "The return destination is chosen from a fixed set, never supplied".

    `from=` selects between two literals and never reaches the header; `return_dir` is the only
    submitted text that can appear in it, and only percent-encoded as the `dir` query value — so
    it cannot become a host (`//evil.example`), a second parameter (`x&stale=1`, which would turn
    a success into something that renders as a refusal, or hide one), a fragment or a header.
    """
    root = cairn_env / "hostile"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        url = _target(form)
        before = _aside(_snapshot)
        stale_fp = "0" * 64

        # Refusals mutate nothing, so the whole matrix runs against one seeded state.
        for origin in HOSTILE_ORIGINS:
            for return_dir in HOSTILE_RETURN_DIRS:
                for return_deep in ("1", "0", "2"):
                    r = _post(
                        client, _with_origin(url, origin), stale_fp, token,
                        return_dir=return_dir, return_deep=return_deep,
                    )
                    assert r.status_code == 303
                    _assert_safe_review_location(
                        r.headers["location"], stale=True, return_dir=return_dir
                    )
        # The one literal that selects the other destination — and it selects ONLY that: nothing
        # the submission carries is appended to it.
        for return_dir in HOSTILE_RETURN_DIRS:
            r = _post(client, _with_origin(url, "fleet"), stale_fp, token, return_dir=return_dir)
            _assert_refused(r, "/review?stale=1")
        assert _aside(_snapshot) == before

        # Two exact refusals, so the matrix above cannot pass on a redirect that drops everything.
        r = _post(client, _with_origin(url, "//evil.example"), stale_fp, token,
                  return_dir="x&stale=1", return_deep="1")
        _assert_refused(r, "/collection/1/review?dir=x%26stale%3D1&stale=1")
        r = _post(client, _with_origin(url, "x&stale=1"), stale_fp, token,
                  return_dir="//evil.example", return_deep="1")
        _assert_refused(r, "/collection/1/review?stale=1")

        # And a SUCCESS carrying the same hostile values lands on the same fixed path, unmarked.
        r = _post(client, _with_origin(url, "https://evil.example/x"), _fp_for(form), token,
                  return_dir="x&stale=1", return_deep="1")
        _assert_performed(r, form, "/collection/1/review?dir=x%26stale%3D1")
        _assert_safe_review_location(r.headers["location"], stale=False, return_dir="x&stale=1")

    assert _aside(lambda s: _statuses(s, 1)) == _rows_after(
        "stop-tracking" if FORM_STATUS[form] == "missing" else "adopt-changed", [SUBJECT], []
    )


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("return_dir", ["//evil.example", "x&stale=1", "a"])
def test_a_success_from_the_fleet_page_returns_to_the_fleet_page_only(cairn_env, form, return_dir):
    root = cairn_env / "fleet-return"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_subject(cid, form)

    with _make_client(cairn_env, seed) as client:
        r = _post(client, _with_origin(_target(form), "fleet"), _fp_for(form), _csrf(client),
                  return_dir=return_dir, return_deep="0")
        _assert_performed(r, form, "/review")


# --- D6: the records mutated are exactly the records the recount hashed --------------------------


@pytest.mark.parametrize("form", BULK_FORMS)
@pytest.mark.parametrize("deep", [True, False])
def test_the_records_mutated_are_exactly_the_records_the_recount_hashed(
    cairn_env, monkeypatch, form, deep
):
    """One encoding of "which records" on a destructive path (design D6).

    After the comparison succeeds the route hands the service the ids of the records its recount
    just hashed. It must not ask the service to re-select by status or by a path pattern — a
    second expression of the folder rule is a second chance to disagree — so the status-selecting
    service entry points are booby-trapped here, the id list is captured, and the datastore diff
    is compared against it record by record.
    """
    from src.control_panel import routes
    from src.models.db import Event, FileEntry

    root = cairn_env / "exactly"
    root.mkdir()
    status, other = FORM_STATUS[form], _other_kind(form)
    same_kind = ["A/six", "a/one", "a/sub/three", "a/two", "a_/four", "ab/five", "b/seven", "top"]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, status, same_kind))
        await _with_session(lambda s: _mk_issues(s, cid, other, ["a/other", "a/sub/other"]))
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a/fresh", "new", "complete"), ("a/kept", "ok", "complete"),
        ]))

    async def ids_of(s, relpaths):
        rows = await s.execute(select(FileEntry.id).where(FileEntry.relpath.in_(relpaths)))
        return sorted(i for (i,) in rows.all())

    async def open_event_file_ids(s):
        rows = await s.execute(select(Event.file_id).where(Event.acknowledged_at.is_(None)))
        return sorted(i for (i,) in rows.all())

    with _make_client(cairn_env, seed) as client:
        scope = _scope("a", deep)
        hashed_paths = _narrowed_relpaths(1, form, scope)
        assert hashed_paths == (["a/one", "a/sub/three", "a/two"] if deep else ["a/one", "a/two"])
        hashed_ids = _aside(lambda s: ids_of(s, hashed_paths))
        before_files, _ = _aside(_snapshot)
        open_before = _aside(open_event_file_ids)

        handed: list[list[int]] = []
        real = routes.scanner_svc.accept_files

        async def spy(session, collection, file_ids, user_id):
            handed.append(list(file_ids))
            return await real(session, collection, file_ids, user_id)

        async def reselected(*args, **kwargs):
            raise AssertionError(
                "a bulk accept re-selected its records instead of using the hashed ids"
            )

        monkeypatch.setattr(routes.scanner_svc, "accept_files", spy)
        monkeypatch.setattr(routes.scanner_svc, "accept_collection", reselected)
        monkeypatch.setattr(routes.scanner_svc, "accept_file", reselected)

        _assert_performed(_bulk(client, form, _fp_for(form, folder=scope), _csrf(client), scope))

    # Handed over once, and it is the hashed id set — nothing more, nothing less.
    assert len(handed) == 1
    assert sorted(handed[0]) == hashed_ids and len(handed[0]) == len(set(handed[0]))

    # The datastore agrees: the records that changed (or vanished) are exactly those ids...
    after_files, _ = _aside(_snapshot)
    before_by_id = {row[0]: row for row in before_files}
    after_by_id = {row[0]: row for row in after_files}
    changed = sorted(i for i, row in before_by_id.items() if after_by_id.get(i) != row)
    assert changed == hashed_ids
    assert set(after_by_id) <= set(before_by_id)  # nothing was created
    for i in hashed_ids:
        if form == "stop-tracking":
            assert i not in after_by_id
        else:
            assert after_by_id[i][3] == "ok"
    # ...and the alerts that closed are exactly those records' alerts.
    assert _aside(open_event_file_ids) == sorted(i for i in open_before if i not in hashed_ids)
    assert len(open_before) - len(hashed_ids) == _aside(_open_count)


# --- D6/D7: the forms a page actually renders, submitted exactly as rendered ---------------------

_FORM_RE = re.compile(r'<form method="post"[^>]*?action="([^"]+)"[^>]*>(.*?)</form>', re.S)
_HIDDEN_RE = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]*)"')


def _rendered_accept_forms(body: str) -> list[dict]:
    """Every accept form on a page: its action, its hidden fields and its button label."""
    forms = []
    for action, inner in _FORM_RE.findall(body):
        fields = {name: html.unescape(value) for name, value in _HIDDEN_RE.findall(inner)}
        if "population_fp" not in fields:
            continue
        label = re.search(r"<button[^>]*>\s*(.*?)\s*</button>", inner, re.S).group(1)
        forms.append({"action": html.unescape(action), "fields": fields, "label": label})
    return forms


def _submit(client, form):
    return client.post(form["action"], data=form["fields"], follow_redirects=False)


def test_rendered_bulk_forms_carry_their_own_scope_and_act_when_submitted_unchanged(cairn_env):
    """The wiring between the template and the route, end to end.

    Every control on the grouped page is checked against the production mint for the scope it
    names, then two of them are submitted with exactly the fields the browser would send: a
    group's control from the whole-collection page, and a page-wide control and a row from a
    folder-scoped page (which must return to that folder).
    """
    root = cairn_env / "rendered"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issues(s, cid, "missing", ["a/x", "a/y", "b 1/z"]))
        await _with_session(lambda s: _mk_issues(s, cid, "modified", ["b 1/w", "b 1/sub/v"]))

    with _make_client(cairn_env, seed) as client:
        forms = _rendered_accept_forms(client.get("/collection/1/review").text)
        bulk = [f for f in forms if "/review/" in f["action"]]
        rows = [f for f in forms if "/file/" in f["action"]]
        assert len(rows) == 5 and len(bulk) == 5

        # Each bulk control names a scope, and its fingerprint is the production mint for it.
        by_key = {}
        for f in bulk:
            verb = f["action"].rsplit("/", 1)[1]
            fields = f["fields"]
            assert set(fields) == {
                "csrf_token", "population_fp", "dir", "deep", "return_dir", "return_deep"
            }
            assert (fields["return_dir"], fields["return_deep"]) == ("", "1")
            scope = _scope(fields["dir"], fields["deep"] == "1")
            assert fields["population_fp"] == _fingerprint(1, verb, folder=scope), f["action"]
            by_key[(verb, fields["dir"])] = f
        assert sorted(by_key) == [
            ("adopt-changed", ""), ("adopt-changed", "b 1"),
            ("stop-tracking", ""), ("stop-tracking", "a"), ("stop-tracking", "b 1"),
        ]
        # The number on each button is the size of the population its fingerprint covers.
        assert by_key[("stop-tracking", "")]["label"] == "Accept 3 deletions"
        assert by_key[("stop-tracking", "a")]["label"] == "Accept 2 deletions"
        assert by_key[("stop-tracking", "b 1")]["label"] == "Accept 1 deletion"
        assert by_key[("adopt-changed", "")]["label"] == "Accept 2 changes"
        assert by_key[("adopt-changed", "b 1")]["label"] == "Accept 2 changes"
        for f in rows:
            assert set(f["fields"]) == {"csrf_token", "population_fp", "return_dir", "return_deep"}
            assert f["label"] in ("Accept change", "Accept deletion")

        # Group `a/`'s "Accept 2 deletions", exactly as rendered.
        _assert_performed(_submit(client, by_key[("stop-tracking", "a")]))
        assert _aside(lambda s: _statuses(s, 1)) == [
            ("missing", "b 1/z"), ("modified", "b 1/sub/v"), ("modified", "b 1/w"),
        ]
        # Every other control on that page was rendered before the accept. The ones whose own
        # population it changed are now stale; group `b 1/`'s were not touched and still work —
        # but they are left for the scoped page below.
        before = _aside(_snapshot)
        _assert_refused(_submit(client, by_key[("stop-tracking", "")]))
        assert _aside(_snapshot) == before

        # The folder's own page: its page-wide controls carry the folder as BOTH scopes.
        scoped = _rendered_accept_forms(client.get("/collection/1/review?dir=b+1").text)
        page_wide = {
            f["action"].rsplit("/", 1)[1]: f
            for f in scoped
            if "/review/" in f["action"]
            and (f["fields"]["dir"], f["fields"]["deep"]) == ("b 1", "1")
        }
        assert sorted(page_wide) == ["adopt-changed", "stop-tracking"]
        for f in scoped:
            assert (f["fields"]["return_dir"], f["fields"]["return_deep"]) == ("b 1", "1")
        _assert_performed(
            _submit(client, page_wide["adopt-changed"]), location="/collection/1/review?dir=b+1"
        )
        assert _aside(lambda s: _statuses(s, 1)) == [
            ("missing", "b 1/z"), ("ok", "b 1/sub/v"), ("ok", "b 1/w"),
        ]
        # ...and the last row, from a freshly rendered scoped page, returns to the folder too.
        scoped = _rendered_accept_forms(client.get("/collection/1/review?dir=b+1").text)
        (row,) = [f for f in scoped if "/file/" in f["action"]]
        assert row["label"] == "Accept deletion"
        _assert_performed(_submit(client, row), location="/collection/1/review?dir=b+1")

    assert _aside(lambda s: _statuses(s, 1)) == [("ok", "b 1/sub/v"), ("ok", "b 1/w")]
    assert _aside(_open_count) == 0


# =============================================================================================
# unify-issue-model D1/D10/D11: the dashboard leads with what needs the operator
# =============================================================================================


def _dashboard(client):
    """The rendered dashboard and the context its route published."""
    with _capture_context() as captured:
        body = client.get("/").text
    return body, [c for name, c in captured if name == "dashboard.html"][0]


def _needs_rows(body: str) -> list[str]:
    """The review links of the needs-you rows, in the order they are listed."""
    return re.findall(r'<a class="needs__row" href="([^"]+)"', _section(body, "needs-you"))


def _activity_markup(body: str) -> str:
    return body.split('id="recent-activity"', 1)[1].split("</main>", 1)[0]


async def _mk_run(session, cid, *, kind="scan", result="ok", moved=0, finished=None):
    from src.models.db import Run

    session.add(Run(
        collection_id=cid, kind=kind, result=result, moved=moved,
        started=NOW, finished=finished or NOW,
    ))
    await session.commit()


def test_needs_you_lists_every_affected_collection_most_deserving_first(cairn_env):
    """web-panel "The needs-you section lists every affected collection".

    The order is the fleet review page's: Locked before Working, then the collection whose newest
    issue was found most recently, then by name — not by id, and not by how many files each holds.
    The collections are created in an order that agrees with none of those.
    """
    created = [("zeta", "worm"), ("work", "churn"), ("beta", "worm"), ("alpha", "worm"),
               ("clean", "worm")]

    async def seed():
        ids = {}
        for name, mode in created:
            root = cairn_env / name
            root.mkdir()
            ids[name] = await seed_collection(root, mode=mode)

        async def issues(s):
            # zeta: Locked, newest finding an hour ago.
            await _mk_issue(s, ids["zeta"], "docs/gone", "missing",
                            detected_at=NOW - timedelta(hours=1))
            await _mk_issue(s, ids["zeta"], "docs/edited", "modified",
                            detected_at=NOW - timedelta(days=2))
            await _mk_files(s, ids["zeta"], [("kept", "ok", "complete")])
            # work: the newest finding of all and the most files — but Working, so it is last.
            for i in range(4):
                await _mk_issue(s, ids["work"], f"w/gone-{i}", "missing", detected_at=NOW)
            # beta and alpha: Locked, found at the same instant, so the name decides.
            for name in ("beta", "alpha"):
                await _mk_issue(s, ids[name], "x", "modified",
                                detected_at=NOW - timedelta(hours=5))
            # clean: nothing changed or missing (a new file is not an issue).
            await _mk_files(s, ids["clean"], [
                ("fine", "ok", "complete"), ("fresh", "new", "complete"),
            ])

        await _with_session(issues)

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)
        with _capture_context() as captured:
            client.get("/review")
        fleet = [c for name, c in captured if name == "review_fleet.html"][0]

    expected = ["zeta", "alpha", "beta", "work"]  # ids 1, 4, 3, 2
    assert [n["name"] for n in ctx["needs"]] == expected
    assert [n["id"] for n in ctx["needs"]] == [1, 4, 3, 2]
    assert [n["policy"]["label"] for n in ctx["needs"]] == ["Locked", "Locked", "Locked", "Working"]
    # The same order the fleet-wide review page lists them in.
    assert [s["collection_name"] for s in fleet["sections"]] == expected
    # Each with its counts...
    assert [(n["missing"], n["modified"], n["total"]) for n in ctx["needs"]] == [
        (1, 1, 2), (0, 1, 1), (0, 1, 1), (4, 0, 4),
    ]
    # ...and each with a link to its own review view, in that order, in the markup.
    section = _section(body, "needs-you")
    assert _needs_rows(body) == [f"/collection/{i}/review" for i in (1, 4, 3, 2)]
    assert section.count('class="needs__cta">Review') == 4
    first = section.split('<a class="needs__row"')[1]
    assert "1 changed" in first and "1 missing" in first and "newest found" in first
    assert "docs/" in first  # the folder holding its issues
    assert "4 missing" in section.split('<a class="needs__row"')[4]
    # More than one collection is affected, so the section also links to the fleet-wide page.
    assert section.count('href="/review"') == 1
    # The unaffected collection is not listed.
    assert "/collection/5/review" not in body
    # The section's total is the badge's number.
    assert _needs_total(body) == 8 == _badge_number(body)
    # It carries links only — nothing that could act.
    for control in ("<form", "<button", "<input", "hx-post", "population_fp"):
        assert control not in section, control


def test_needs_you_with_one_affected_collection_links_only_to_its_review(cairn_env):
    """web-panel "One affected collection": `/review` is linked only when more than one is."""
    a, b = cairn_env / "affected", cairn_env / "clean"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_issue(s, ca, "gone", "missing"))
        await _with_session(lambda s: _mk_files(s, cb, [("fine", "ok", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    assert [n["id"] for n in ctx["needs"]] == [1]
    assert _needs_rows(body) == ["/collection/1/review"]
    assert 'href="/review"' not in body
    assert _needs_total(body) == 1 == _badge_number(body)


def test_the_needs_you_total_is_the_sidebar_badges_number_on_every_page(cairn_env):
    """web-panel "Every count of outstanding work agrees".

    One shared definition: missing + modified, across the user's collections. New and healthy
    files are in neither number, and the badge says the same thing on every page it rides.
    """
    a, b = cairn_env / "big", cairn_env / "small"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b, mode="churn")
        await _with_session(lambda s: _mk_files(
            s, ca,
            [(f"vol/gone-{i}", "missing", "complete") for i in range(1000)]
            + [(f"fresh-{i}", "new", "complete") for i in range(7)]
            + [(f"fine-{i}", "ok", "complete") for i in range(11)],
        ))
        await _with_session(lambda s: _mk_files(s, cb, [
            ("edited-a", "modified", "none"), ("edited-b", "modified", "none"),
            ("edited-c", "modified", "none"), ("gone", "missing", "none"),
            ("fresh", "new", "none"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)
        other_pages = {
            url: client.get(url).text
            for url in ("/collections", "/collection/1", "/collection/2/review", "/review",
                        "/verify", "/settings")
        }
        with _capture_context() as captured:
            client.get("/review")
        fleet = [c for name, c in captured if name == "review_fleet.html"][0]

    assert _needs_total(body) == 1004 == _badge_number(body)
    assert ctx["needs_total"] == ctx["alert_count"] == 1004
    assert ctx["needs_total"] == sum(n["total"] for n in ctx["needs"])
    assert ctx["needs_total"] == ctx["needs_missing"] + ctx["needs_modified"]
    assert (ctx["needs_missing"], ctx["needs_modified"]) == (1001, 3)
    assert "1,001 missing · 3 changed" in re.sub(r"\s+", " ", _section(body, "needs-you"))
    assert 'aria-label="1004 files missing or changed — open dashboard"' in body
    for url, page in other_pages.items():
        assert _badge_number(page) == 1004, url
    assert fleet["total_issues"] == 1004


def test_resolving_an_issue_is_what_lowers_the_count(cairn_env):
    """web-panel "Resolving an issue is what lowers the count" — and viewing one does not."""
    root = cairn_env / "lowers"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_issue(s, cid, "edited-a", "modified", id=SUBJECT_ID))
        await _with_session(lambda s: _mk_issue(s, cid, "edited-b", "modified"))
        await _with_session(lambda s: _mk_issue(s, cid, "gone", "missing"))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        before = _aside(_snapshot)
        # Viewing changes nothing: every surface, twice.
        for _ in range(2):
            for url in ("/", "/collection/1", "/collection/1/review", "/review"):
                assert _badge_number(client.get(url).text) == 3, url
        assert _aside(_snapshot) == before
        assert "1 missing · 2 changed" in client.get("/collection/1/op-status").text

        fp = _fingerprint(1, "accept-file", file_id=SUBJECT_ID)
        _assert_performed(_post(client, _target("accept-file"), fp, token), "accept-file")

        body = client.get("/").text
        assert _badge_number(body) == 2 == _needs_total(body)
        # The collection's status reflects the remaining issues only.
        assert "1 missing · 1 changed" in client.get("/collection/1/op-status").text


@pytest.mark.parametrize("state", ["ok-only", "new-only", "ok-and-new"])
def test_the_all_clear_line_appears_when_files_are_indexed_and_none_are_issues(cairn_env, state):
    """web-panel "Nothing needs the operator": one line, saying what was established, no link."""
    root = cairn_env / "clear"
    root.mkdir()
    files = {
        "ok-only": [("fine", "ok", "complete")],
        "new-only": [("fresh", "new", "pending")],
        "ok-and-new": [("fine", "ok", "complete"), ("fresh", "new", "complete")],
    }[state]

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, files))

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    section = _section(body, "needs-you")
    assert "No files are changed or missing" in section
    assert body.count("No files are changed or missing") == 1
    assert "<a" not in section and "href" not in section  # a single line, with no link
    assert ctx["needs"] == [] and ctx["needs_total"] == 0 and ctx["any_files"] is True
    assert _needs_total(body) is None and _badge_number(body) is None
    assert 'href="/review"' not in body


@pytest.mark.parametrize("state", ["no-collections", "empty-collection", "two-empty-collections"])
def test_nothing_indexed_is_not_all_clear_on_the_dashboard(cairn_env, state):
    """web-panel "Nothing indexed is not all clear": nothing checked is not nothing wrong."""
    roots = {
        "no-collections": [],
        "empty-collection": ["empty"],
        "two-empty-collections": ["empty-a", "empty-b"],
    }[state]

    async def seed():
        from src.database import ensure_implicit_user, get_sessionmaker

        async with get_sessionmaker()() as s:
            await ensure_implicit_user(s)
        for name in roots:
            root = cairn_env / name
            root.mkdir()
            await seed_collection(root)

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    assert ctx["any_files"] is False and ctx["needs"] == []
    for claim in ("No files are changed or missing", "Nothing needs you", "All clear",
                  "Nothing changed or missing"):
        assert claim not in body, claim
    section = _section(body, "needs-you")
    if roots:
        assert "No files indexed yet" in section
    else:
        assert section == ""  # no collections: the section makes no statement at all


def test_an_issue_anywhere_removes_the_all_clear_line(cairn_env):
    a, b = cairn_env / "fine", cairn_env / "hurt"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("fine", "ok", "complete")]))
        await _with_session(lambda s: _mk_files(s, cb, [("edited", "modified", "complete")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text

    assert "No files are changed or missing" not in body
    assert "Nothing needs you" not in body
    assert _needs_rows(body) == ["/collection/2/review"]


# --- recent activity: a read-only summary of the event log ---------------------------------------


def test_recent_activity_is_a_read_only_summary_by_day_and_collection(cairn_env):
    """web-panel "Recent activity is a read-only summary by day and collection"."""
    a, b = cairn_env / "photos", cairn_env / "docs"
    a.mkdir()
    b.mkdir()
    # The summary covers the last 30 days of real time, so these are relative to now.
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    older = recent - timedelta(days=3)

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)

        async def events(s):
            for _ in range(3):  # a scan added files to one collection...
                await _mk_event(s, ca, None, "added", acked=True, detected_at=recent)
            # ...and found changes in another.
            await _mk_issue(s, cb, "edited-a", "modified", detected_at=recent)
            await _mk_issue(s, cb, "edited-b", "modified", detected_at=recent)
            await _mk_issue(s, cb, "gone", "missing", detected_at=recent)
            await _mk_event(s, ca, None, "added", acked=True, detected_at=older)
            await _mk_event(s, ca, None, "moved", acked=True, detected_at=older, detail="x → y")
            # Outside the window: not part of the summary.
            await _mk_event(s, cb, None, "added", acked=True,
                            detected_at=recent - timedelta(days=45))

        await _with_session(events)

    with _make_client(cairn_env, seed) as client:
        before = _aside(_snapshot)
        body, ctx = _dashboard(client)

    lines = ctx["activity"]
    # One line per day and collection.
    assert len(lines) == 3
    assert len({(line["day"], line["collection_id"]) for line in lines}) == 3
    by_key = {(line["collection"], tuple(p["text"] for p in line["parts"])): line for line in lines}
    assert set(by_key) == {
        ("photos", ("3 added",)),
        ("docs", ("1 missing", "2 changed")),
        ("photos", ("1 added", "1 moved")),
    }
    # Most recent first: both of the recent day's lines precede the older day's.
    assert lines[0]["day"] == lines[1]["day"] != lines[2]["day"]
    assert tuple(p["text"] for p in lines[2]["parts"]) == ("1 added", "1 moved")
    assert str(older.astimezone().day) in lines[2]["day"]
    # What needs attention is marked; what is informational is not.
    alarming = by_key[("docs", ("1 missing", "2 changed"))]["parts"]
    assert [p["alarm"] for p in alarming] == [True, True]
    assert [p["alarm"] for p in by_key[("photos", ("3 added",))]["parts"]] == [False]

    markup = _activity_markup(body)
    for text in ("3 added", "1 missing", "2 changed", "1 moved", "photos", "docs"):
        assert text in markup, text
    assert markup.count('class="activity__row"') == 3
    # Read-only: no control of any kind.
    for control in ("<button", "<form", "<input", "<select", "hx-post", "hx-get", "hx-delete",
                    "onclick", "population_fp"):
        assert control not in markup, control
    assert _aside(_snapshot) == before  # ...and rendering it changed nothing


@pytest.mark.parametrize("informational", [True, False])
def test_a_healthy_systems_activity_is_not_empty(cairn_env, informational):
    """web-panel "A healthy system's activity is not empty".

    Informational events are born acknowledged; a summary that listed only open ones would render
    its empty state on exactly the days everything went well. The second parameter is the control:
    with no events at all the empty state IS what renders, so the first assertion is not vacuous.
    """
    root = cairn_env / "healthy"
    root.mkdir()
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("fine", "ok", "complete")]))
        if informational:
            async def events(s):
                for kind, n in (("added", 4), ("restored", 2), ("moved", 1)):
                    for _ in range(n):
                        await _mk_event(s, cid, None, kind, acked=True, detected_at=recent)

            await _with_session(events)

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    markup = _activity_markup(body)
    if informational:
        assert len(ctx["activity"]) == 1
        assert [p["text"] for p in ctx["activity"][0]["parts"]] == [
            "4 added", "1 moved", "2 restored",
        ]
        assert not any(p["alarm"] for p in ctx["activity"][0]["parts"])
        assert "Nothing recorded" not in markup and "empty-state" not in markup
        assert "activity__part--alarm" not in markup
        for text in ("4 added", "1 moved", "2 restored"):
            assert text in markup, text
    else:
        assert ctx["activity"] == []
        assert "Nothing recorded in the last 30 days." in markup


@pytest.mark.parametrize("came_back", [1, 2])
def test_recent_activity_names_a_changed_reappearance_and_never_folds_it_into_restored(
    cairn_env, came_back
):
    """web-panel "Recent activity does not count it as a restore".

    A file that came back with different bytes is the opposite of a restore. On a day with both,
    the two are stated separately, the changed reappearance first and marked as an alarm.
    """
    root = cairn_env / "cameback"
    root.mkdir()
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)

    async def seed():
        cid = await seed_collection(root)

        async def events(s):
            from src.models.db import FileEntry

            for i in range(came_back):
                await _mk_files(s, cid, [(f"deed-{i}.pdf", "modified", "complete")])
                fid = await s.scalar(
                    select(FileEntry.id).where(FileEntry.relpath == f"deed-{i}.pdf")
                )
                await _mk_event(s, cid, fid, "restored_changed", detected_at=recent,
                                detail="recorded aaaa → found bbbb")
            for _ in range(2):
                await _mk_event(s, cid, None, "restored", acked=True, detected_at=recent)

        await _with_session(events)

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    assert len(ctx["activity"]) == 1
    assert ctx["activity"][0]["parts"] == [
        {"text": f"{came_back} came back different", "alarm": True},
        {"text": "2 restored", "alarm": False},
    ]
    markup = _activity_markup(body)
    assert f"{came_back} came back different" in markup
    assert "2 restored" in markup
    assert f"{came_back + 2} restored" not in markup
    assert (
        f'<span class="activity__part activity__part--alarm">{came_back} came back different</span>'
        in markup
    )


# --- the last-activity tile ----------------------------------------------------------------------
#
# The exhaustive matrix for this tile (moved suffix, newest-finished selection) lives in
# tests/test_fleet_review.py; these are the three scenarios the dashboard requirement names.


@pytest.mark.parametrize(
    ("kind", "result", "expected"),
    [
        # "names a non-scan run for what it is"
        ("stamp", "ok", "act stamp"),
        ("upgrade", "ok", "act upgrade"),
        # "names a partial or failed run"
        ("scan", "partial", "act scan · partial"),
        ("scan", "error", "act scan · failed"),
        ("upgrade", "partial", "act upgrade · partial"),
        # "treats an interrupted run neutrally"
        ("scan", "interrupted", "act scan · interrupted"),
        # the control: a clean scan carries no result suffix
        ("scan", "ok", "act scan"),
    ],
)
def test_the_last_activity_tile_names_the_run_it_found(cairn_env, kind, result, expected):
    root = cairn_env / "act"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_run(s, cid, kind=kind, result=result))

    with _make_client(cairn_env, seed) as client:
        body, ctx = _dashboard(client)

    sub = ctx["tiles"]["last_activity_sub"]
    assert sub == expected
    tile = body.split("<span>Last activity</span>", 1)[1].split("\n  </div>", 1)[0]
    assert f'<div class="tile__sub">{expected}</div>' in tile
    if kind != "scan":
        assert "scan" not in sub  # a stamp or upgrade pass is never described as a scan
    if result == "interrupted":
        # Neutral: named for what it is, never as a failure.
        for alarming in ("failed", "error", "fault"):
            assert alarming not in sub
        assert "var(--danger)" not in tile


# --- no surface offers to acknowledge, or counts what is unreviewed ------------------------------

FORBIDDEN_VOCABULARY = [
    "Mark reviewed", "Mark all reviewed", "unreviewed", "Unreviewed", "Acknowledge",
    "acknowledge", "unacknowledged", "not yet baselined", "Baseline ", "need action",
    "needs action",
]
# An address that only ever acknowledged: `/events/…`, or anything ending in `/ack` / `ack-all`.
_ACK_ADDRESS = re.compile(
    r'(?:hx-post|hx-get|hx-put|hx-patch|hx-delete|action|formaction|href)='
    r'"[^"]*(?:/events/|/ack(?:-all)?(?:[?"#]|$))'
)


def _assert_no_acknowledge_surface(body: str, where: str) -> None:
    for phrase in FORBIDDEN_VOCABULARY:
        assert phrase not in body, (where, phrase)
    assert not _ACK_ADDRESS.search(body), (where, _ACK_ADDRESS.search(body))
    assert "/events/" not in body, where


def test_the_ack_address_pattern_recognises_the_retired_controls():
    """The pattern above is only worth asserting on if it would have caught the old markup."""
    for retired in (
        '<button hx-post="/events/7/ack?view=review">',
        '<form hx-post="/events/ack-all">',
        '<form method="post" action="/collection/1/review/ack-all">',
        '<a href="/events/7/ack">',
    ):
        assert _ACK_ADDRESS.search(retired), retired
    for surviving in (
        '<form hx-post="/collection/1/scan">',
        '<form method="post" action="/collection/1/review/stop-tracking">',
        '<form method="post" action="/collection/1/file/5/accept">',
        '<a href="/collection/1/review?dir=back">',
    ):
        assert not _ACK_ADDRESS.search(surviving), surviving


def test_no_page_offers_to_acknowledge_or_counts_unreviewed_events(cairn_env):
    """web-panel "No surface offers to acknowledge".

    Rendered at their busiest: two collections under both policies, missing / changed / new /
    healthy files spread over folders, open alerts of every alarming kind (a changed reappearance
    included), a detached open alert, and the refusal notice.
    """
    a, b = cairn_env / "locked", cairn_env / "working"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b, mode="churn", ots_mode="none")

        async def busy(s):
            from src.models.db import FileEntry

            for cid in (ca, cb):
                await _mk_issues(
                    s, cid, "missing", ["a/gone-1", "a/gone-2", "b/sub/gone-3", "top-gone"]
                )
                await _mk_issues(s, cid, "modified", ["a/edited-1", "b/edited-2"])
                await _mk_files(s, cid, [
                    ("a/fresh", "new", "pending"), ("fresh-top", "new", "complete"),
                    ("a/kept", "ok", "complete"),
                    ("b/came-back", "modified", "complete"),
                ])
                fid = await s.scalar(select(FileEntry.id).where(
                    FileEntry.collection_id == cid, FileEntry.relpath == "b/came-back"
                ))
                await _mk_event(s, cid, fid, "restored_changed",
                                detail="recorded " + "a" * 64 + " → found " + "b" * 64)
                await _mk_event(s, cid, None, "missing")  # detached, still open
                await _mk_event(s, cid, None, "added", acked=True,
                                detected_at=datetime.now(timezone.utc))

        await _with_session(busy)

    with _make_client(cairn_env, seed) as client:
        pages = {}
        for url in (
            "/", "/collections",
            "/collection/1", "/collection/2", "/collection/1?view=list&filter=issues",
            "/collection/1?view=list&filter=new",
            "/collection/1/files?filter=issues", "/collection/1/tree?prefix=a",
            "/collection/1/op-status", "/collection/2/op-status",
            "/collection/1/review", "/collection/2/review", "/collection/1/review?stale=1",
            "/collection/1/review?dir=a", "/collection/1/review?dir=b&deep=0",
            "/collection/1/review?dir=nowhere",
            "/review", "/review?stale=1",
        ):
            r = client.get(url)
            assert r.status_code == 200, (url, r.status_code)
            pages[url] = r.text

    # The pages really are in the state described — these are not empty shells.
    assert _needs_total(pages["/"]) == 14
    assert "Came back different" in pages["/collection/1/review"]
    assert "Your action was NOT applied" in pages["/review?stale=1"]
    assert pages["/review"].count("Accept deletion") >= 8
    for url, body in pages.items():
        _assert_no_acknowledge_surface(body, url)


# --- D5/D11: policies and status are named in plain words ----------------------------------------


@pytest.mark.parametrize(
    ("files", "pill", "kind"),
    [
        ([("a", "modified", "none"), ("b", "modified", "none"), ("c", "ok", "none")],
         "2 changed", "warn"),
        ([("a", "missing", "none"), ("c", "ok", "none")], "1 missing", "danger"),
        ([("a", "missing", "none"), ("b", "modified", "none"), ("c", "modified", "none")],
         "1 missing · 2 changed", "danger"),
        ([("c", "ok", "none"), ("d", "new", "none")], "All clear", "ok"),
        ([], "No files indexed", "muted"),
    ],
    ids=["changed", "missing", "both", "clear", "empty"],
)
def test_the_status_pill_states_the_fact_not_a_grade(cairn_env, files, pill, kind):
    """web-panel "The status states what is wrong" / "Both kinds are stated" / "An empty
    collection is not all clear" — on every surface that carries the pill."""
    root = cairn_env / "pill"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, files))

    with _make_client(cairn_env, seed) as client:
        surfaces = {
            url: client.get(url).text
            for url in ("/", "/collections", "/collection/1", "/collection/1/op-status")
        }

    for url, body in surfaces.items():
        pills = re.findall(
            r'<span class="pill pill--soft pill--(\w+)">(?:<svg.*?</svg>)?([^<]*)</span>',
            body, re.S,
        )
        assert (kind, pill) in pills, (url, pills)
        for grade in ("Attention", "Alert</span>", ">Alert<"):
            assert grade not in body, (url, grade)
        if pill != "All clear":
            assert "All clear" not in body, url


def test_changed_and_missing_collections_carry_visually_distinct_icons(cairn_env):
    """web-panel: "Status indicators SHALL use visually distinct icons for a collection with
    changed files and a collection with missing files" — the colour alone must not carry it."""
    a, b = cairn_env / "edited", cairn_env / "lost"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("x", "modified", "none")]))
        await _with_session(lambda s: _mk_files(s, cb, [("y", "missing", "none")]))

    with _make_client(cairn_env, seed) as client:
        changed = client.get("/collection/1/op-status").text
        missing = client.get("/collection/2/op-status").text

    icon = re.compile(r'<span class="pill pill--soft pill--\w+">(<svg.*?</svg>)', re.S)
    changed_icon, missing_icon = icon.search(changed).group(1), icon.search(missing).group(1)
    assert changed_icon != missing_icon
    assert "1 changed" in changed and "pill--warn" in changed
    assert "1 missing" in missing and "pill--danger" in missing


@pytest.mark.parametrize(("mode", "label", "other"), [("worm", "Locked", "Working"),
                                                      ("churn", "Working", "Locked")])
def test_the_policy_is_shown_in_plain_words(cairn_env, mode, label, other):
    """web-panel "The policy is shown in plain words" / "The Working policy is not described as
    deletions-only"."""
    root = cairn_env / "policy"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root, mode=mode)
        await _with_session(lambda s: _mk_issue(s, cid, "gone", "missing"))

    with _make_client(cairn_env, seed) as client:
        detail = client.get("/collection/1").text
        dash = client.get("/").text
        fleet = client.get("/review").text

    cell = detail.split('<div class="meta-cell__label">Policy</div>', 1)[1]
    cell = cell.split("</div></div>", 1)[0]
    assert f">{label}</div>" in cell
    assert other not in cell
    for page in (detail, dash, fleet):
        # The stored value is never the label.
        assert not re.search(r">\s*(worm|churn|WORM|Churn|Worm)\s*<", page)
        assert label in page
    if mode == "churn":
        # Wherever Working is described, the description includes the changed reappearance.
        for page in (detail, dash, fleet):
            described = re.findall(r'title="(Edits are normal[^"]*)"', page)
            assert described, "the Working policy is not described on this page"
            for text in described:
                assert "comes back different after going missing" in text
                assert "Deletions are" in text
    else:
        assert "Every change and every deletion is flagged." in detail
