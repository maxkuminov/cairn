"""The fleet-wide review page (`GET /review`) and the dashboard's way into it (unify-issue-model).

Covers the web-panel requirement "Fleet-wide review groups every collection's issues": one section
per affected collection the viewer owns, each built from ONE `_read_population` of that collection
and rendered by the same component as the collection's own review page — the same folder groups,
rows, per-file actions and per-group bulk actions. Sections are ordered by what most deserves
attention (Locked before Working, then the newest finding, then name), not by size.

**No control on the page acts on more than one collection**, and the tests assert its absence:
every accept is authorised over a single collection's population and is irreversible. An accept
submitted from here returns here — on success and on refusal — and that destination is chosen from
a fixed pair, never taken from the submission.

Plus the dashboard's last-activity tile (design D14 of `add-fleet-review-and-run-health`), which
described every run of every kind and every result as a clean scan.

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

# The markup readers are the review-page suite's: the two pages render one component, and reading
# them with one parser is what lets a test compare them.
from tests.test_ux_review import (  # noqa: F401
    BULK_ACTION,
    HEX64,
    ROW_ACTION,
    _bulk_forms,
    _forms,
    _row_forms,
    _submit,
    _text,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

STALE_LEAD = "<strong>Your action was NOT applied</strong>"


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


async def _mk_event(session, cid, file_id, kind, *, acked=False, at=None, detail=None):
    from src.models.db import Event

    e = Event(
        collection_id=cid,
        file_id=file_id,
        kind=kind,
        detected_at=at or NOW,
        acknowledged_at=NOW if acked else None,
        detail=detail,
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
        from src.control_panel.routes import _narrow, _population_fingerprint, _read_population
        from src.models.db import Collection

        collection = await s.get(Collection, collection_id)
        pop = await _read_population(s, collection)
        return _population_fingerprint(
            collection, _narrow(pop, "accept-file", file_id=file_id)
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


def _fleet_ctx(captured) -> dict:
    ctxs = [c for name, c in captured if name == "review_fleet.html"]
    assert len(ctxs) == 1, f"expected one fleet render, got {len(ctxs)}"
    return ctxs[0]


def _fleet(client, url: str = "/review"):
    """GET the fleet page; return ``(body, sections)`` — the HTML and the per-collection models."""
    with _capture_context() as captured:
        resp = client.get(url)
    assert resp.status_code == 200, resp.status_code
    return resp.text, _fleet_ctx(captured)["sections"]


def _section_html(body: str, collection_id: int) -> str:
    """One collection's ``<section class="fleet-section">``, up to the next one (or the guidance)."""
    start = body.index(f'id="fleet-collection-{collection_id}"')
    ends = [
        i
        for i in (
            body.find('<section class="fleet-section"', start),
            body.find('id="review-help"', start),
        )
        if i != -1
    ]
    return body[start : min(ends) if ends else len(body)]


def _section_head(body: str, collection_id: int) -> str:
    """A section's header line: its name, policy, both counts and its link."""
    section = _section_html(body, collection_id)
    return section[section.index(">") + 1 : section.index('<div class="card card--flush issues"')]


def _names(sections) -> list[str]:
    return [s["collection_name"] for s in sections]


# --- the page lists every affected collection, with both counts --------------------------------


def test_fleet_page_lists_every_affected_collection_with_both_counts(cairn_env):
    """Scenario "Issues in several collections are listed together" (#27's whole point): two
    collections in trouble, one page that lists both and can act."""
    a, b = cairn_env / "alpha", cairn_env / "beta"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        ids = await _with_session(lambda s: _mk_files(s, ca, [
            ("a/changed.txt", "modified"),
            ("a/gone.txt", "missing"),
            ("fine.txt", "ok"),
        ]))
        await _with_session(lambda s: _mk_event(s, ca, ids[1], "missing"))
        await _with_session(lambda s: _mk_files(s, cb, [("b/edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = _fleet_ctx(captured)
        sections = ctx["sections"]

    assert _names(sections) == ["alpha", "beta"]  # same policy, same newest finding -> by name
    assert (sections[0]["missing"], sections[0]["modified"]) == (1, 1)
    assert (sections[1]["missing"], sections[1]["modified"]) == (0, 1)
    assert (ctx["total_issues"], ctx["total_missing"], ctx["total_modified"]) == (3, 1, 2)
    assert "3 files across 2 collections — 1 missing · 2 changed." in _text(
        body[body.index('id="review-summary"') :].split("</div>")[0]
    )
    # Missing before changed within a list, regardless of path order.
    assert [it["relpath"] for it in sections[0]["rows"]] == ["a/gone.txt", "a/changed.txt"]
    # The `ok` file is not an issue and is not listed anywhere.
    assert "fine.txt" not in body
    # Every row carries a real, per-row fingerprint — the control fails closed without one.
    for s in sections:
        assert s["rows"], "every affected collection's rows are rendered"
        for it in s["rows"]:
            assert HEX64.fullmatch(it["fp"])
            assert f'name="population_fp" value="{it["fp"]}"' in body
    # Each section names its collection and its policy, states BOTH counts — including a zero,
    # since an unstated count cannot be told apart from an absent one — and links to that
    # collection's own review view.
    assert _text(_section_head(body, 1)) == "alpha Locked 1 missing 1 changed Open alpha"
    assert _text(_section_head(body, 2)) == "beta Locked 0 missing 1 changed Open beta"
    assert _section_head(body, 1).count('href="/collection/1/review"') == 2  # its name and its button
    assert 'href="/collection/2/review"' in _section_head(body, 2)
    # The accept forms post from the fleet surface, so a success or a refusal comes back here.
    assert 'action="/collection/1/file/2/accept?from=fleet"' in body
    assert 'action="/collection/2/file/4/accept?from=fleet"' in body


def test_each_section_states_both_counts_including_a_zero(cairn_env):
    """ "1 missing" alone leaves the reader to infer whether the other count is zero or unmentioned,
    and the two kinds carry different consequences."""
    a, b = cairn_env / "only-gone", cairn_env / "only-edited"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("x.jpg", "missing"), ("y.jpg", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("z.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    heads = {s["collection_name"]: _text(_section_head(body, s["collection_id"])) for s in sections}
    assert "2 missing 0 changed" in heads["only-gone"]
    assert "0 missing 1 changed" in heads["only-edited"]


def test_the_fleet_page_lights_no_other_nav_item(cairn_env):
    """Task 2.7: `/review` is reached from the dashboard, the badge and the cards — it has no nav
    entry, and it must not make another one claim to be the current page."""
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
        body, sections = _fleet(client)

    assert _names(sections) == ["sick"]
    assert 'id="fleet-collection-2"' not in body
    # A `new` file is watched, not work: it is not an issue and is not listed.
    assert "n.txt" not in body and "kept.txt" not in body


# --- ordering: what most deserves attention, not size ------------------------------------------


def test_locked_collections_lead_whatever_their_size(cairn_env):
    """Scenario "Locked collections lead": a single change where nothing should change is not
    listed beneath hundreds of routine deletions elsewhere — nor beneath a newer finding, nor
    beneath a name that sorts first."""
    working, locked = cairn_env / "aaa-working", cairn_env / "zzz-locked"
    working.mkdir()
    locked.mkdir()

    async def seed():
        cw = await seed_collection(working, mode="churn")
        cl = await seed_collection(locked)
        # 300 missing files, found NOW (the newest finding on the page)...
        await _with_session(lambda s: _mk_files(
            s, cw, [(f"gone/{i:04d}.jpg", "missing") for i in range(300)]
        ))
        # ...against ONE changed file, found a month earlier.
        ids = await _with_session(lambda s: _mk_files(s, cl, [("ledger.xlsx", "modified")]))
        await _with_session(lambda s: _mk_event(
            s, cl, ids[0], "modified", at=NOW - timedelta(days=30)
        ))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    assert _names(sections) == ["zzz-locked", "aaa-working"]
    assert [s["policy"]["label"] for s in sections] == ["Locked", "Working"]
    assert [(s["missing"], s["modified"]) for s in sections] == [(0, 1), (300, 0)]
    assert body.index('id="fleet-collection-2"') < body.index('id="fleet-collection-1"')
    # Each section names its policy in plain words, never the stored value.
    assert "zzz-locked Locked 0 missing 1 changed" in _text(_section_head(body, 2))
    assert "aaa-working Working 300 missing 0 changed" in _text(_section_head(body, 1))
    assert "worm" not in _text(body) and "churn" not in _text(body)


def test_within_a_policy_the_newest_finding_leads_then_name(cairn_env):
    """Scenario "Within a policy the newest finding leads" — and name only breaks a tie."""
    roots = {n: cairn_env / n for n in ("b-old", "z-new", "a-old")}
    for r in roots.values():
        r.mkdir()

    async def seed():
        for name, days in (("b-old", 10), ("z-new", 1), ("a-old", 10)):
            cid = await seed_collection(roots[name])
            # Many files in the OLD collections, one in the new: size must not decide either.
            n = 1 if name == "z-new" else 5
            ids = await _with_session(lambda s, c=cid, n=n: _mk_files(
                s, c, [(f"gone/{i}.jpg", "missing") for i in range(n)]
            ))
            for fid in ids:
                await _with_session(lambda s, c=cid, f=fid, d=days: _mk_event(
                    s, c, f, "missing", at=NOW - timedelta(days=d)
                ))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    assert _names(sections) == ["z-new", "a-old", "b-old"]
    positions = [body.index(f'id="fleet-collection-{s["collection_id"]}"') for s in sections]
    assert positions == sorted(positions)


def test_the_newest_finding_is_the_newest_open_alert_not_the_newest_file(cairn_env):
    """A collection's place is decided by when its most recent issue was FOUND. One recent finding
    among old ones leads a collection whose every finding is merely less old."""
    one, two = cairn_env / "one", cairn_env / "two"
    one.mkdir()
    two.mkdir()

    async def seed():
        c1 = await seed_collection(one)
        c2 = await seed_collection(two)
        ids = await _with_session(lambda s: _mk_files(s, c1, [("a.jpg", "missing"), ("b.jpg", "missing")]))
        for fid in ids:
            await _with_session(lambda s, f=fid: _mk_event(s, c1, f, "missing", at=NOW - timedelta(days=5)))
        ids2 = await _with_session(lambda s: _mk_files(s, c2, [("c.jpg", "missing"), ("d.jpg", "missing")]))
        await _with_session(lambda s: _mk_event(s, c2, ids2[0], "missing", at=NOW - timedelta(days=40)))
        await _with_session(lambda s: _mk_event(s, c2, ids2[1], "missing", at=NOW - timedelta(days=2)))

    with _make_client(cairn_env, seed) as client:
        _body, sections = _fleet(client)

    assert _names(sections) == ["two", "one"]


# --- one component: the fleet section IS the collection's review list --------------------------


def test_a_section_groups_by_folder_exactly_as_the_collections_own_review_view(cairn_env):
    """ "Grouped by folder exactly as on the collection's own review view, with the same rows, the
    same per-file actions and the same per-group bulk actions" — same groups, same counts, and the
    very same fingerprints (an action authorised here is the action authorised there)."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [
            ("2019/trip/a.jpg", "missing"),
            ("2019/trip/b.jpg", "missing"),
            ("2019/docs/x.txt", "modified"),
            ("2019/top.jpg", "missing"),
        ]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "missing", at=NOW - timedelta(days=1)))
        await _with_session(lambda s: _mk_event(s, cid, ids[2], "modified", at=NOW - timedelta(days=3)))

    def shape(model):
        return [
            (
                g["label"], g["dir"], g["deep"], g["missing"], g["modified"], g["found"],
                [(it["relpath"], it["kind"], it["fp"]) for it in g["items"]],
                g["accept_changes"] and (g["accept_changes"]["count"], g["accept_changes"]["fp"]),
                g["accept_deletions"] and (g["accept_deletions"]["count"], g["accept_deletions"]["fp"]),
            )
            for g in model["groups"]
        ]

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)
        with _capture_context() as captured:
            own = client.get("/collection/1/review").text
        own_model = [c for n, c in captured if n == "collection_review.html"][0]["r"]

    (section,) = sections
    assert shape(section) == shape(own_model)
    assert [g["label"] for g in section["groups"]] == [
        "2019/top.jpg".rsplit("/", 1)[0] + "/ (this folder only)", "2019/trip/", "2019/docs/",
    ]
    # The rendered forms differ in exactly one thing: where they return to.
    fleet_forms = _forms(_section_html(body, 1))
    own_forms = [f for f in _forms(own) if f["fields"].get("dir") != "" or ROW_ACTION.match(f["action"])]
    assert [f["action"] for f in fleet_forms] == [f["action"] + "?from=fleet" for f in own_forms]
    assert [f["fields"]["population_fp"] for f in fleet_forms] == [
        f["fields"]["population_fp"] for f in own_forms
    ]
    assert [f["button"] for f in fleet_forms] == [f["button"] for f in own_forms]


def test_a_changed_restore_is_an_alarm_on_the_fleet_page_too(cairn_env):
    """ "Wherever that file is listed for review, the row SHALL show that event's recorded detail":
    the fleet page lists it, so it carries the label and both digests in full there as well."""
    root = cairn_env / "deeds"
    root.mkdir()
    detail = "recorded " + "1" * 64 + " → found " + "2" * 64

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [("deed.pdf", "modified")]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "restored_changed", detail=detail))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    assert sections[0]["rows"][0]["kind"] == "came_back"
    section = _section_html(body, 1)
    assert '<span class="review-row__alarm">Came back different</span>' in section
    assert f'<div class="review-row__detail mono">{detail}</div>' in section
    assert "Contents changed" not in section


# --- no collection-spanning action -------------------------------------------------------------


def test_no_control_on_the_fleet_page_acts_on_more_than_one_collection(cairn_env):
    """Scenario "No collection-spanning action is offered".

    Every accept is authorised by a fingerprint minted over ONE collection's population and is
    irreversible. A fleet-wide accept would need a cross-collection fingerprint and a confirm
    reading "remove N records across M collections" — the unscoped verb rebuilt one level up.
    """
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [
            ("x/gone.txt", "missing"), ("y/gone.txt", "missing"), ("y/edit.txt", "modified"),
        ]))
        await _with_session(lambda s: _mk_files(s, cb, [("edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    # Only the POSTing forms are in scope: the chrome's top-bar search is a GET navigation.
    posts = _forms(body)
    assert len(posts) == 4 + 3 + 1, "one per row, plus the group and folder-line bulk actions"
    for f in posts:
        # Every form's address is under exactly one collection, and is one of the three verbs,
        # bound to the fleet surface.
        m = BULK_ACTION.match(f["action"]) or ROW_ACTION.match(f["action"])
        assert m, f"an unexpected verb on the fleet page: {f['action']}"
        assert f["action"].endswith("?from=fleet"), f["action"]
        assert len(re.findall(r"/collection/\d+", f["action"])) == 1
        # ...and it sits inside that collection's own section, nowhere else.
        cid = int(m.group(1))
        assert f'action="{f["action"]}"' in _section_html(body, cid)
        other = 2 if cid == 1 else 1
        assert f'action="{f["action"]}"' not in _section_html(body, other)
    # A bulk form names ONE folder of its one collection; its count never exceeds that collection's.
    by_cid = {s["collection_id"]: s for s in sections}
    for f in _bulk_forms(body):
        cid = int(BULK_ACTION.match(f["action"]).group(1))
        n = int(re.search(r"Accept ([\d,]+) ", f["button"]).group(1).replace(",", ""))
        assert n <= by_cid[cid]["total"]
    # Nothing outside the sections posts at all: no page-wide toolbar exists here.
    outside = body[: body.index('id="fleet-collection-')]
    assert _forms(outside) == []
    assert 'id="review-toolbar"' not in body
    # The retired acknowledge verbs are not borrowed onto this page either.
    for retired in ("/review/ack-all", "/events/ack-all", "/events/", "Mark reviewed", "Mark all"):
        assert retired not in body, retired


# --- the per-row accept, and where it lands ----------------------------------------------------


def _accept(client, url, fp, token):
    return client.post(
        url,
        data={"population_fp": fp},
        headers={"X-CSRF-Token": token},
        follow_redirects=False,
    )


def test_fleet_row_accept_lands_back_on_the_fleet_page(cairn_env):
    """Scenario "A row acts on one file without leaving the page's snapshot"."""
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing"), ("also.txt", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("edited.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)
        # The form the page rendered for that row, carrying the fingerprint minted from the same
        # population read the row came from — which is the one the production recount reproduces.
        form = next(f for f in _row_forms(body) if f["action"] == "/collection/1/file/1/accept?from=fleet")
        row = next(it for it in sections[0]["rows"] if it["id"] == 1)
        assert form["fields"]["population_fp"] == row["fp"] == _row_fp(1, 1)

        r = _submit(client, form)
        assert r.status_code == 303
        assert r.headers["location"] == "/review"

    # Exactly one file acted on: its record is gone, its sibling and the other collection untouched.
    assert _statuses(1) == [("also.txt", "missing")]
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
        # The notice is rendered on the fleet page, not only on the collection one — and leads
        # with the consequence.
        refused = client.get("/review?stale=1").text
        assert STALE_LEAD in refused
        assert "Nothing was removed or accepted." in refused
        assert "The list below is current" in refused
        assert 'id="stale-banner"' not in client.get("/review").text
        assert 'id="stale-banner"' not in client.get("/review?stale=maybe").text

    assert _statuses(1) == [("gone.txt", "missing")]  # refusals mutate nothing
    assert _open_events() == 1


# --- the return destination is a whitelist, never the supplied value ---------------------------

HOSTILE_FROM = [
    "?from=https://evil.example/x",
    "?from=//evil.example",
    "?from=/collection/99/review",
    "?from=",
    "?from=FLEET",  # the whitelist is exact, not case-folded
    "?from=fleet%20",
    "",  # no parameter at all
]


@pytest.mark.parametrize("hostile", HOSTILE_FROM)
def test_a_hostile_from_value_never_reaches_the_redirect(cairn_env, hostile):
    """`from` carries one bit and selects a destination from a fixed pair (design D9).

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


@pytest.mark.parametrize("hostile", HOSTILE_FROM)
def test_a_hostile_from_value_never_reaches_a_bulk_redirect(cairn_env, hostile):
    """The same whitelist on the folder-scoped verbs: the supplied value selects nothing but one
    of two fixed destinations, and the same folder's accept runs either way."""
    root = cairn_env / "hostilebulk"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [
            ("a/gone.txt", "missing"),
            ("b/stays.txt", "missing"),
        ]))

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)
        form = next(f for f in _bulk_forms(body) if f["fields"]["dir"] == "a")
        action = form["action"].split("?")[0]
        r = client.post(f"{action}{hostile}", data=form["fields"], follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/collection/1/review"

    assert _statuses(1) == [("b/stays.txt", "missing")]


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


# --- a group's bulk action: one collection's folder, and back to this page ---------------------


def _two_collections_with_the_same_folders(cairn_env):
    """Two collections holding the SAME folder names, so only the collection id tells them apart."""
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()
    files = [
        ("shared/a.jpg", "missing"),
        ("shared/b.jpg", "missing"),
        ("shared/edit.txt", "modified"),
        ("other/c.jpg", "missing"),
    ]

    async def seed():
        for root in (a, b):
            cid = await seed_collection(root)
            await _with_session(lambda s, c=cid: _mk_files(s, c, files))

    return seed, sorted(files)


def test_a_group_action_acts_on_one_collections_folder_and_returns_to_the_fleet_page(cairn_env):
    """Scenario "A group action acts on one collection's folder"."""
    seed, everything = _two_collections_with_the_same_folders(cairn_env)

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)
        form = next(
            f for f in _bulk_forms(_section_html(body, 1))
            if f["fields"]["dir"] == "shared" and "stop-tracking" in f["action"]
        )
        # The group's form posts from the fleet surface, naming its folder and its count.
        assert form["action"] == "/collection/1/review/stop-tracking?from=fleet"
        assert (form["fields"]["dir"], form["fields"]["deep"]) == ("shared", "1")
        assert form["button"] == "Accept 2 deletions"
        assert HEX64.fullmatch(form["fields"]["population_fp"])
        # Both collections have a `shared/`, so the confirmation names the collection as well.
        assert form["attrs"]["data-where"] == "shared/ — one"
        assert "2 files in ' + this.dataset.where + '?" in form["attrs"]["onsubmit"]
        # The second collection's same-named folder has its own form, with its own fingerprint.
        twin = next(
            f for f in _bulk_forms(_section_html(body, 2))
            if f["fields"]["dir"] == "shared" and "stop-tracking" in f["action"]
        )
        assert twin["action"] == "/collection/2/review/stop-tracking?from=fleet"
        assert twin["attrs"]["data-where"] == "shared/ — two"
        assert twin["fields"]["population_fp"] != form["fields"]["population_fp"]

        r = _submit(client, form)
        assert r.status_code == 303
        assert r.headers["location"] == "/review"

        # Only collection one's `shared/` missing files: its changed file there, its other folder
        # and the whole of collection two are untouched.
        assert _statuses(1) == [("other/c.jpg", "missing"), ("shared/edit.txt", "modified")]
        assert _statuses(2) == everything

        # ...and the page the operator lands on reflects it.
        _body, sections = _fleet(client)
        assert [(s["collection_name"], s["missing"], s["modified"]) for s in sections] == [
            ("one", 1, 1), ("two", 3, 1),
        ]


def test_a_group_form_does_not_validate_at_another_collections_address(cairn_env):
    """Same folder, same file names, same statuses — but a fingerprint is minted over one
    collection's records, so replayed at the twin collection's address it is refused."""
    seed, everything = _two_collections_with_the_same_folders(cairn_env)

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)
        form = next(
            f for f in _bulk_forms(_section_html(body, 1))
            if f["fields"]["dir"] == "shared" and "stop-tracking" in f["action"]
        )
        r = client.post(
            "/collection/2/review/stop-tracking?from=fleet",
            data=form["fields"],
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/review?stale=1"

    assert _statuses(1) == everything and _statuses(2) == everything


def test_a_stale_group_action_is_refused_and_returns_to_the_fleet_page(cairn_env):
    """Scenario "A stale fleet-wide submission is refused and returns to the fleet page"."""
    seed, everything = _two_collections_with_the_same_folders(cairn_env)

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)
        form = next(
            f for f in _bulk_forms(_section_html(body, 1))
            if f["fields"]["dir"] == "shared" and "stop-tracking" in f["action"]
        )
        # A scan commits after the page loaded: one more file of that folder is now missing. The
        # button said "2"; submitting it must not remove three.
        _aside(lambda s: _mk_files(s, 1, [("shared/late.jpg", "missing")]))

        r = _submit(client, form)
        assert r.status_code == 303
        assert r.headers["location"] == "/review?stale=1"

        landed = client.get(r.headers["location"]).text
        assert STALE_LEAD in landed
        assert "a collection changed since the page loaded" in landed
        assert "Nothing was removed or accepted." in landed
        # The list the operator is told to look at again is current: it now says three.
        assert "Accept 3 deletions" in _section_html(landed, 1)

    assert _statuses(1) == sorted(everything + [("shared/late.jpg", "missing")])
    assert _statuses(2) == everything


def test_a_single_folder_section_offers_its_bulk_action_on_the_folder_line(cairn_env):
    """Where every file of a collection lies in one folder the section is a single list, not a
    group of one — and since this page has no page-wide toolbar, that list's bulk actions sit on
    the folder line, acting on that one collection."""
    a, b = cairn_env / "single", cairn_env / "bystander"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [
            ("only/a.jpg", "missing"), ("only/b.jpg", "missing"), ("only/c.txt", "modified"),
        ]))
        await _with_session(lambda s: _mk_files(s, cb, [("only/z.jpg", "missing")]))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)
        single = next(s for s in sections if s["collection_name"] == "single")
        assert single["flat"] is True

        section = _section_html(body, 1)
        assert '<section class="issue-group">' not in section
        line = section[section.index('class="issues__where"') : section.index('class="review-row"')]
        assert '<span class="mono">only/</span>' in line
        changes, deletions = _bulk_forms(line)
        assert changes["action"] == "/collection/1/review/adopt-changed?from=fleet"
        assert deletions["action"] == "/collection/1/review/stop-tracking?from=fleet"
        assert (changes["button"], deletions["button"]) == ("Accept 1 change", "Accept 2 deletions")
        for f in (changes, deletions):
            # The single list IS the collection's whole issue set, so the action names the
            # collection — in its scope and in its confirmation.
            assert (f["fields"]["dir"], f["fields"]["deep"]) == ("", "1")
            assert f["attrs"]["data-where"] == "single"
        # They are the only bulk forms of the section, and the rows follow them.
        assert len(_bulk_forms(section)) == 2 and len(_row_forms(section)) == 3

        r = _submit(client, deletions)
        assert r.status_code == 303 and r.headers["location"] == "/review"

    assert _statuses(1) == [("only/c.txt", "modified")]
    assert _statuses(2) == [("only/z.jpg", "missing")]


# --- the dashboard's way in --------------------------------------------------------------------


def _needs_you(client) -> tuple[str, dict]:
    """The dashboard's needs-you section (markup) and the context it was rendered from."""
    with _capture_context() as captured:
        body = client.get("/").text
    ctx = [c for n, c in captured if n == "dashboard.html"][0]
    start = body.index('id="needs-you"')
    return body[start : body.index("</section>", start)], ctx


def test_the_dashboard_sends_a_fleet_wide_count_to_the_fleet_page(cairn_env):
    """ "So that a fleet-wide issue count has a destination that can act on it": one affected
    collection goes straight to its own review page; several also get the fleet page."""
    a, b = cairn_env / "one", cairn_env / "two"
    a.mkdir()
    b.mkdir()

    async def seed():
        ca = await seed_collection(a)
        cb = await seed_collection(b)
        await _with_session(lambda s: _mk_files(s, ca, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_files(s, cb, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        # One affected collection -> its own review page, and no detour through a fleet page that
        # would list the same single collection.
        needs, ctx = _needs_you(client)
        assert [n["id"] for n in ctx["needs"]] == [1] and ctx["needs_total"] == 1
        assert 'href="/collection/1/review"' in needs
        assert 'href="/review"' not in needs

        # A second affected collection -> the fleet page, which lists every file the count names.
        _aside(lambda s: _mk_files(s, 2, [("edited.txt", "modified"), ("gone2.txt", "missing")]))
        needs, ctx = _needs_you(client)
        assert sorted(n["id"] for n in ctx["needs"]) == [1, 2] and ctx["needs_total"] == 3
        assert 'href="/review"' in needs
        assert 'href="/collection/1/review"' in needs and 'href="/collection/2/review"' in needs

        # ...and that destination accounts for exactly the files the dashboard counted.
        with _capture_context() as captured:
            client.get("/review")
        assert _fleet_ctx(captured)["total_issues"] == ctx["needs_total"]


def test_the_dashboard_offers_no_review_link_when_nothing_is_affected(cairn_env):
    root = cairn_env / "clean"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        needs, ctx = _needs_you(client)

    assert ctx["needs"] == [] and ctx["needs_total"] == 0
    assert "<a " not in needs, "an all-clear line links nowhere"
    assert "No files are changed or missing." in needs


# --- scoping -----------------------------------------------------------------------------------


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
            ctx = _fleet_ctx(captured)
        finally:
            get_settings.cache_clear()

    assert _names(ctx["sections"]) == ["mine"]
    assert (ctx["total_issues"], ctx["total_missing"]) == (1, 1)
    assert "my-secret.txt" in body
    assert "Bobs Files" not in body and "bobs-secret.txt" not in body
    assert "/collection/2/" not in body


# --- the two render budgets --------------------------------------------------------------------


def test_one_very_large_collection_does_not_crowd_out_the_others(cairn_env):
    """Scenario "One very large collection does not hide the others": the per-collection cap, with
    a "N more" note whose N is the SNAPSHOT remainder."""
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
        body, sections = _fleet(client)

    assert _names(sections) == ["big", "small"]
    assert sections[0]["shown"] == CAP
    assert len(sections[0]["rows"]) == CAP
    # The counts and the remainder come from the population, never from the render list.
    assert sections[0]["missing"] == over and sections[0]["total"] == over
    assert sections[0]["truncated"] is True
    big_section = _section_html(body, 1)
    assert big_section.count('class="review-row"') == CAP
    assert f"{over} missing" in _text(_section_head(body, 1))
    note = _text(big_section[big_section.rindex('class="review-list__more"') :])
    assert f"Showing {CAP} of {over} — 7 more in big." in note
    assert big_section.count('href="/collection/1/review"') == 3  # name, button, and the "more" link
    # Its bulk action still covers the whole collection, and says how much of that is off-page.
    (bulk,) = _bulk_forms(big_section)
    assert bulk["button"] == f"Accept {over} deletions"
    assert "includes 7 not shown here" in bulk["text"]
    assert "7 of them are not shown on this page." in bulk["attrs"]["onsubmit"]
    # The other affected collection still renders its rows, which is the whole point of the cap.
    assert sections[1]["shown"] == 1
    assert "edited.txt" in _section_html(body, 2)


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
            # Two folders, so the section past the budget has group headers to keep.
            await _with_session(lambda s, c=cid: _mk_files(
                s, c,
                [(f"gone/{j:05d}.txt", "missing") for j in range(CAP - 1)]
                + [("edited/one.txt", "modified")],
            ))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    assert len(sections) == n_full + 1
    assert sum(s["shown"] for s in sections) == TOTAL
    last = sections[-1]
    assert last["collection_name"] == f"c{n_full}"
    assert last["shown"] == 0 and last["rows"] == []
    # ...but it is still on the page, with its header, both counts and its way in.
    assert (last["missing"], last["modified"], last["total"]) == (CAP - 1, 1, CAP)
    cid = last["collection_id"]
    section = _section_html(body, cid)
    assert _text(_section_head(body, cid)) == (
        f"c{n_full} Locked {CAP - 1} missing 1 changed Open c{n_full}"
    )
    assert f'href="/collection/{cid}/review"' in _section_head(body, cid)
    assert 'class="review-row"' not in section
    # Its groups' headers survive too: folder, counts, and the action with what it leaves unseen.
    assert [g["dir"] for g in last["groups"]] == ["edited", "gone"]
    assert section.count('<section class="issue-group">') == 2
    assert f'href="/collection/{cid}/review?dir=gone"' in section
    assert f"{CAP - 1} missing" in _text(section) and "1 changed" in _text(section)
    gone = next(f for f in _bulk_forms(section) if f["fields"]["dir"] == "gone")
    assert gone["button"] == f"Accept {CAP - 1} deletions"
    assert f"includes {CAP - 1} not shown here" in gone["text"]
    # A section with ZERO rows is not "showing the first 0" — that describes a listing, and this is
    # a listing that could not start. It says what happened and points at the page that can.
    text = _text(section)
    assert (
        "This page's row budget was already used by the collections above, so "
        f"c{n_full}'s {CAP} files are not listed here — open its review page."
    ) in text
    assert f"These {CAP - 1} files are not listed on this page — open gone/ to see them." in text
    assert "This file is not listed on this page — open edited/ to see it." in text
    assert "Showing 0 of" not in text and "Showing the first 0" not in text


def test_a_single_folder_section_past_the_budget_does_not_describe_a_listing_of_zero(
    cairn_env, monkeypatch
):
    """A single-list section with no rows left in the budget says that is what happened — once.
    "Showing 0 of N files" would describe a listing that does not exist."""
    from src.control_panel import routes

    monkeypatch.setattr(routes, "FLEET_ROW_LIMIT", 2)
    first, second = cairn_env / "first", cairn_env / "second"
    first.mkdir()
    second.mkdir()

    async def seed():
        c1 = await seed_collection(first)
        c2 = await seed_collection(second)
        await _with_session(lambda s: _mk_files(s, c1, [("only/a.jpg", "missing"), ("only/b.jpg", "missing")]))
        await _with_session(lambda s: _mk_files(s, c2, [(f"only/{i}.jpg", "missing") for i in range(3)]))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    assert _names(sections) == ["first", "second"]
    assert sections[1]["shown"] == 0 and sections[1]["flat"] is True
    section = _section_html(body, 2)
    text = _text(section)
    assert "second's 3 files are not listed here — open its review page." in text
    assert "Showing 0 of" not in text
    assert section.count('class="review-list__more"') == 1
    assert 'class="review-row"' not in section
    # Its header, counts, folder line and action are all still there.
    assert "second Locked 3 missing 0 changed" in _text(_section_head(body, 2))
    (bulk,) = _bulk_forms(section)
    assert bulk["button"] == "Accept 3 deletions" and "includes 3 not shown here" in bulk["text"]


def test_a_group_cut_short_by_the_budget_still_says_what_its_action_leaves_unseen(cairn_env, monkeypatch):
    """The budget can run out in the middle of a small, open group. The rows stop; the claims must
    not: the group's header still states its whole count, its action says how many of its files are
    not on the page, and the section says how many more the collection holds and where."""
    from src.control_panel import routes

    monkeypatch.setattr(routes, "FLEET_COLLECTION_ROW_LIMIT", 3)
    root = cairn_env / "cut"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [
            ("g1/a.jpg", "missing"), ("g1/b.jpg", "missing"),
            ("g2/c.jpg", "missing"), ("g2/d.jpg", "missing"),
        ]))
        # g1 found later than g2, so g1 leads and the cut lands inside g2.
        for fid in ids[:2]:
            await _with_session(lambda s, f=fid: _mk_event(s, cid, f, "missing", at=NOW))
        for fid in ids[2:]:
            await _with_session(lambda s, f=fid: _mk_event(s, cid, f, "missing", at=NOW - timedelta(days=2)))

    with _make_client(cairn_env, seed) as client:
        body, sections = _fleet(client)

    (section,) = sections
    assert [(g["dir"], g["shown"], g["more"]) for g in section["groups"]] == [("g1", 2, 0), ("g2", 1, 1)]
    html = _section_html(body, 1)
    g2 = next(s for s in re.findall(r'<section class="issue-group">(.*?)</section>', html, re.S) if 'value="g2"' in s)
    assert g2.count('class="review-row"') == 1
    assert "2 missing" in _text(g2)
    (bulk,) = _bulk_forms(g2)
    assert bulk["button"] == "Accept 2 deletions"
    assert "includes 1 not shown here" in bulk["text"]
    assert "1 of them is not shown on this page." in bulk["attrs"]["onsubmit"]
    assert 'href="/collection/1/review?dir=g2"' in g2
    assert "Showing 3 of 4 — 1 more in cut." in _text(html)
    # ...and the group says so itself, with the way to see the rest.
    own_line = _text(g2[g2.rindex('class="review-list__more"') :])
    assert "1 more file in this folder is not shown on this page — open g2/." in own_line
    # The group that was listed in full makes no such statement.
    g1 = next(s for s in re.findall(r'<section class="issue-group">(.*?)</section>', html, re.S) if 'value="g1"' in s)
    assert "review-list__more" not in g1 and "not shown" not in _text(g1)


# --- the two empty states ----------------------------------------------------------------------


def test_empty_state_with_collections_but_no_issues(cairn_env):
    """Scenario "No open issues" — distinct from the state shown to a user with no collections."""
    root = cairn_env / "allclear"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = _fleet_ctx(captured)

    assert ctx["has_collections"] is True and ctx["sections"] == []
    assert "No open issues" in body
    assert "Nothing is changed or missing in any of your collections right now." in body
    assert "No collections yet" not in body
    # Nothing needs adding — that is the *other* empty state, and offering it here misreads a
    # healthy fleet as an unconfigured one.
    assert 'href="/collection/new"' not in body.split('class="empty-state"')[1]
    # No section, no guidance and no accept form over an empty set.
    assert 'id="review-help"' not in body and "fleet-section" not in body
    assert _forms(body) == []


def test_empty_state_with_no_collections_at_all(cairn_env):
    async def seed():
        from src.database import get_sessionmaker, ensure_implicit_user

        async with get_sessionmaker()() as s:
            await ensure_implicit_user(s)

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = _fleet_ctx(captured)

    assert ctx["has_collections"] is False
    assert "No collections yet" in body
    assert "There is nothing to review because nothing is being watched." in body
    # Watching nothing establishes nothing: this state must not read as an all-clear.
    assert "No open issues" not in body and "Nothing is changed or missing" not in body
    assert 'href="/collection/new"' in body.split('class="empty-state"')[1]
    assert _forms(body) == []


def test_a_stale_notice_on_an_emptied_fleet_page_does_not_point_at_a_list(cairn_env):
    """A refusal redirects unconditionally, so it can land after everything was resolved elsewhere:
    "the list below is current" over an empty page would read as if the accept had emptied it."""
    root = cairn_env / "emptied"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("kept.txt", "ok")]))

    with _make_client(cairn_env, seed) as client:
        body = client.get("/review?stale=1").text

    assert STALE_LEAD in body and "Nothing was removed or accepted." in body
    assert "The list below is current" not in body
    assert "may already have been handled" in _text(body)


# --- one snapshot per collection: rows, counts and fingerprints alike --------------------------


def test_the_fleet_page_reads_each_collections_population_exactly_once(cairn_env, monkeypatch):
    """ "Every number a section shows and every action a row or group authorises SHALL be derived
    from one read of that collection's review population."

    With two reads, a scan committing between them lets a row's DISPLAYED state and the fingerprint
    its accept control AUTHORIZES describe two different states of the same file — the guard's own
    failure mode, reintroduced beside the guard.
    """
    from src.control_panel import routes

    roots = [cairn_env / n for n in ("one", "two", "healthy")]
    for r in roots:
        r.mkdir()

    async def seed():
        c1 = await seed_collection(roots[0])
        c2 = await seed_collection(roots[1])
        c3 = await seed_collection(roots[2])
        ids = await _with_session(lambda s: _mk_files(s, c1, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, c1, ids[0], "missing"))
        await _with_session(lambda s: _mk_files(s, c2, [("edited.txt", "modified")]))
        await _with_session(lambda s: _mk_files(s, c3, [("kept.txt", "ok")]))

    reads: list[int] = []
    original = routes._read_population

    async def counting(session, collection, file_id=None):
        reads.append(collection.id)
        return await original(session, collection, file_id)

    with _make_client(cairn_env, seed) as client:
        monkeypatch.setattr(routes, "_read_population", counting)
        _body, sections = _fleet(client)

    # Once per owned collection — the healthy one included (that read is how the page learns it has
    # nothing to list) — and never a second time for a section that is rendered.
    assert sorted(reads) == [1, 2, 3]
    # The row's open alert came out of that same read.
    item = sections[0]["rows"][0]
    assert item["event_id"] == 1 and item["kind"] == "missing"


def test_a_fleet_section_is_one_snapshot_of_its_collection(cairn_env, monkeypatch):
    """A scan committing right after a collection's population read (forced here inside the
    display-only history read that follows it) reaches none of that section's claims on its own:
    the header counts, the bulk count, the rows and the page total all describe the one read."""
    from src.control_panel import routes

    root = cairn_env / "snap"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone/a.jpg", "missing"), ("gone/b.jpg", "missing")]))

    original = routes._attach_facts

    async def racing(session, collection_id, rows):
        await _with_session(lambda s: _mk_files(s, collection_id, [("gone/late.jpg", "missing")]))
        return await original(session, collection_id, rows)

    with _make_client(cairn_env, seed) as client:
        monkeypatch.setattr(routes, "_attach_facts", racing)
        with _capture_context() as captured:
            body = client.get("/review").text
        ctx = _fleet_ctx(captured)

    assert len(_statuses(1)) == 3, "the concurrent scan did commit"
    (section,) = ctx["sections"]
    assert (section["missing"], section["total"], len(section["rows"])) == (2, 2, 2)
    assert ctx["total_issues"] == ctx["total_missing"] == 2
    html = _section_html(body, 1)
    assert "2 missing 0 changed" in _text(_section_head(body, 1))
    (bulk,) = _bulk_forms(html)
    assert bulk["button"] == "Accept 2 deletions"
    assert len(_row_forms(html)) == 2 and "late.jpg" not in html


def test_a_rows_displayed_state_and_its_fingerprint_move_together(cairn_env):
    """Both are slices of one `_read_population`, so they can never contradict each other.

    After a scan raises a fresh alert on the row, the row shows the new finding AND its fingerprint
    changes — and the fingerprint minted before is refused, which is what proves the two described
    the same snapshot.
    """
    root = cairn_env / "together"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        ids = await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))
        await _with_session(lambda s: _mk_event(s, cid, ids[0], "missing", at=NOW - timedelta(days=3)))

    with _make_client(cairn_env, seed) as client:
        token = _csrf(client)
        _body, sections = _fleet(client)
        before = sections[0]["rows"][0]
        assert before["event_id"] == 1
        assert before["found_full"] == (NOW - timedelta(days=3)).strftime("%Y-%m-%d %H:%M UTC")

        # Another session — a scan — raises a newer alert on that file.
        _aside(lambda s: _mk_event(s, 1, 1, "missing", at=NOW))

        _body, sections = _fleet(client)
        after = sections[0]["rows"][0]
        # The row shows the newer finding, and its authorization moved with it.
        assert after["event_id"] == 2
        assert after["found_full"] == NOW.strftime("%Y-%m-%d %H:%M UTC")
        assert after["fp"] != before["fp"]

        # The earlier fingerprint is refused — it described a population that no longer exists.
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", before["fp"], token)
        assert r.status_code == 303 and r.headers["location"] == "/review?stale=1"
        assert _statuses(1) == [("gone.txt", "missing")] and _open_events() == 2
        # ...and the current one is accepted.
        r = _accept(client, "/collection/1/file/1/accept?from=fleet", after["fp"], token)
        assert r.status_code == 303 and r.headers["location"] == "/review"

    assert _statuses(1) == []
    assert _open_events() == 0


# --- what the page explains --------------------------------------------------------------------


def test_the_fleet_page_explains_the_actions_after_the_files(cairn_env):
    """Files first here too: the sections come before what the actions do, and that explanation
    makes neither forbidden claim."""
    root = cairn_env / "explain"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing"), ("edit.txt", "modified")]))

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)

    assert body.index('class="review-row"') < body.index('id="review-help"')
    before = body[body.index("<h1>What changed</h1>") : body.index('class="review-row"')]
    assert "What the actions do" not in before and "guide__" not in before
    help_text = _text(body[body.index('id="review-help"') :].split("<script")[0])
    assert "Accept change" in help_text and "Accept deletion" in help_text
    assert "the proof file stays in the proof store" in help_text
    for bad in ("reversible by a rescan", "proofs will be deleted", "proof is deleted"):
        assert bad not in body


def test_the_fleet_guidance_names_only_controls_the_fleet_page_renders(cairn_env):
    """The guidance is shared with the collection page, which has a "Copy paths" control. This
    page has none — so it sends the operator to the page that does, instead of naming a button
    that is not here."""
    root = cairn_env / "controls"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _with_session(lambda s: _mk_files(s, cid, [("gone.txt", "missing")]))

    with _make_client(cairn_env, seed) as client:
        body, _sections = _fleet(client)
        own = client.get("/collection/1/review").text

    help_html = body[body.index('id="review-help"') :].split("<script")[0]
    assert "data-copy=" not in body, "the fleet page renders no copy control"
    assert "Copy paths" not in help_html and "Copy full paths" not in help_html
    assert "Open the collection's own review page to copy the affected paths" in _text(help_html)
    assert "restore them with your own backup tool" in _text(help_html)
    # ...and that page is linked from every section, and does carry the control.
    assert 'href="/collection/1/review"' in _section_head(body, 1)
    assert 'data-copy="rel"' in own and "Copy paths" in own


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
