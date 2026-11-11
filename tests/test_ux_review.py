"""The collection review page (unify-issue-model): one kind of open item, resolved by actions named
for their outcome, listed by folder with the facts needed to decide.

Covers the web-panel requirements "The review view lists issues by folder with the facts needed to
decide", "Issues are resolved by actions named for their outcome", "A file that came back different
is rendered as an alarm, not as an arrival", and the phone-width scenario "The files come before
the instructions" — plus the rendered half of the accept guard (the fingerprints a page publishes,
the stale notice, where a scoped accept returns to).

Everything is exercised at **route level** (TestClient against a seeded database): the page is a
pure function of ONE `_read_population` snapshot, so the honest way to assert its claims is to
seed a population, render it, and — where the claim is about an action — submit the very form the
page rendered and look at what the database then holds.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ux_review.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests.conftest import seed_collection  # noqa: F401  (cairn_env comes from conftest)

# The refusal banner leads with the CONSEQUENCE (live-pass M2): the operator clicked an irreversible
# button and the first thing they need is that it did not run. Asserted as the lead phrase, not the
# whole paragraph — the second half branches on whether anything is left to review.
STALE_LEAD = "<strong>Your action was NOT applied</strong>"
STALE_NOTHING_MUTATED = "Nothing was removed or accepted."
STALE_COPY = "this collection changed since the page loaded"

# Both claims sound reasonable and both are false, so they are asserted absent rather than left to
# a reviewer's eye: a rescan matches the digest the scan already recorded, so an accepted change is
# never re-raised; and no path in the product deletes a proof — what is lost is the *link* to it.
# (The page legitimately says "no proof file is deleted"; none of these match that.)
FORBIDDEN = [
    "reversible by a rescan",
    "undone by a rescan",
    "undo it with a rescan",
    "proofs will be deleted",
    "proofs are deleted",
    "proof is deleted",
    "deletes the proof",
    "deletes its proof",
]

BULK_ACTION = re.compile(r"^/collection/(\d+)/review/(adopt-changed|stop-tracking)(\?from=fleet)?$")
ROW_ACTION = re.compile(r"^/collection/(\d+)/file/(\d+)/accept(\?from=fleet)?$")
HEX64 = re.compile(r"[0-9a-f]{64}")


# --- harness ---------------------------------------------------------------------------------


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

    Deliberately not the app's cached engine: these calls interleave with a live ``TestClient`` —
    they read what an accept left behind, or play a scan committing between a render and a submit —
    so they must not disturb the engine the client's loop is using.
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


def _review(client, url: str = "/collection/1/review"):
    """GET a review page; return ``(body, r)`` — the HTML and the review model it was built from."""
    with _capture_context() as captured:
        resp = client.get(url)
    assert resp.status_code == 200, resp.status_code
    ctxs = [c for name, c in captured if name == "collection_review.html"]
    assert len(ctxs) == 1, f"expected one review render, got {len(ctxs)}"
    return resp.text, ctxs[0]["r"]


# --- seeding ---------------------------------------------------------------------------------


def _local_noon(days_ago: int) -> datetime:
    """Noon server-local time ``days_ago`` days back, as UTC — a finding whose local calendar day is
    unambiguous whatever hour the suite runs at."""
    local = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).astimezone(timezone.utc)


def _ago(**delta) -> datetime:
    """A moment comfortably past a whole number of units ago, so "N days ago" is unambiguous."""
    return datetime.now(timezone.utc) - timedelta(**delta) - timedelta(minutes=5)


def _day_words(days_ago: int) -> str:
    """The words the panel uses for a local day — derived here independently of the route helper."""
    today = datetime.now().astimezone().date()
    if days_ago == 0:
        return "today"
    if days_ago == 1:
        return "yesterday"
    day = today - timedelta(days=days_ago)
    label = f"{day.day} {day.strftime('%b')}"
    return label if day.year == today.year else f"{label} {day.year}"


async def _seed_files(cid: int, specs) -> list[int]:
    """Insert files (and their events) from ``(relpath, status[, opts])``; return ids in spec order.

    A ``missing``/``modified`` file gets one open event of its own kind, detected now, unless
    ``opts`` says otherwise:

    * ``found`` — the open event's ``detected_at``; ``None`` writes no event at all (legacy data).
    * ``kind`` / ``detail`` — the open event's kind and detail (e.g. ``restored_changed``).
    * ``history`` — extra events, ``(kind, detected_at, acknowledged)``, written BEFORE the open one.
    * ``mtime`` / ``size`` / ``sha256`` / ``ots_state`` — the file row's own columns.
    """
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        rows = []
        for spec in specs:
            relpath, status = spec[0], spec[1]
            opts = spec[2] if len(spec) > 2 else {}
            fe = FileEntry(
                collection_id=cid, relpath=relpath, status=status,
                size=opts.get("size", 4321), sha256=opts.get("sha256", "d" * 64),
                ots_state=opts.get("ots_state", "complete"), mtime=opts.get("mtime"),
                first_seen=now, last_checked=now, last_changed=now,
            )
            s.add(fe)
            rows.append((fe, status, opts))
        await s.flush()
        for fe, status, opts in rows:
            for kind, at, acked in opts.get("history", ()):
                s.add(Event(
                    collection_id=cid, file_id=fe.id, kind=kind, detected_at=at,
                    acknowledged_at=now if acked else None,
                ))
            if status in ("missing", "modified") and opts.get("found", now) is not None:
                s.add(Event(
                    collection_id=cid, file_id=fe.id,
                    kind=opts.get("kind", status), detected_at=opts.get("found", now),
                    detail=opts.get("detail"),
                ))
        await s.commit()
        return [fe.id for fe, _status, _opts in rows]


async def _seed_missing_file_with_event(cid: int, relpath: str = "2019/IMG_4421.jpg") -> None:
    await _seed_files(cid, [(relpath, "missing")])


async def _seed_missing_and_modified(cid: int, *, missing: int, modified: int) -> None:
    """Populate both kinds of issue (in ``gone/`` and ``edited/``), so both actions are applicable."""
    await _seed_files(
        cid,
        [(f"gone/{i}.jpg", "missing") for i in range(missing)]
        + [(f"edited/{i}.txt", "modified", {"size": 99, "sha256": "e" * 64}) for i in range(modified)],
    )


async def _seed_ok_file(cid: int) -> None:
    """One indexed, healthy file: the collection is genuinely "all clear", not merely empty."""
    await _seed_files(cid, [("kept/steady.txt", "ok", {"size": 10, "sha256": "b" * 64})])


def _photos(cairn_env, specs, *, mode: str = "worm", ots_mode: str = "perfile"):
    """A seed coroutine factory: one collection named ``photos`` holding ``specs``."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root, mode=mode, ots_mode=ots_mode)
        await _seed_files(cid, specs)

    return seed


# --- reading the page ------------------------------------------------------------------------


class _FormParser(HTMLParser):
    """Collect every ``<form>`` with its attributes, hidden fields, button and visible text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._cur: dict | None = None
        self._in_button = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._cur = {
                "action": a.get("action", ""), "method": a.get("method", "get").lower(),
                "attrs": a, "fields": {}, "button": "", "button_class": "", "button_title": "",
                "text": "",
            }
            self.forms.append(self._cur)
        elif self._cur is not None and tag == "input" and a.get("name"):
            self._cur["fields"][a["name"]] = a.get("value", "")
        elif self._cur is not None and tag == "button":
            self._cur["button_class"] = a.get("class", "")
            self._cur["button_title"] = a.get("title", "")
            self._in_button = True

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None
        elif tag == "button":
            self._in_button = False

    def handle_data(self, data):
        if self._cur is not None:
            self._cur["text"] += data
            if self._in_button:
                self._cur["button"] += data


def _forms(fragment: str) -> list[dict]:
    """Every POSTing form in ``fragment`` (the chrome's top-bar search is a GET navigation)."""
    p = _FormParser()
    p.feed(fragment)
    out = []
    for f in p.forms:
        if f["method"] != "post":
            continue
        f["button"] = " ".join(f["button"].split())
        f["text"] = " ".join(f["text"].split())
        out.append(f)
    return out


def _bulk_forms(fragment: str) -> list[dict]:
    return [f for f in _forms(fragment) if BULK_ACTION.match(f["action"])]


def _row_forms(fragment: str) -> list[dict]:
    return [f for f in _forms(fragment) if ROW_ACTION.match(f["action"])]


def _submit(client, form: dict, **overrides):
    """POST a rendered form exactly as a browser would: its own action and its own hidden fields."""
    return client.post(
        form["action"], data={**form["fields"], **overrides}, follow_redirects=False
    )


_INLINE_TAG = re.compile(r"</?(?:strong|em|b|i|span|a|code)\b[^>]*>")


def _flat(fragment: str) -> str:
    """Markup with its whitespace collapsed — the template wraps prose across source lines."""
    return " ".join(fragment.split())


def _text(fragment: str) -> str:
    """Visible text of an HTML fragment, whitespace-collapsed.

    Inline tags vanish (so ``<strong>photos</strong>, under`` reads "photos, under", as it renders);
    every other tag is a word break.
    """
    without_inline = _INLINE_TAG.sub("", re.sub(r"<svg.*?</svg>", "", fragment, flags=re.S))
    return " ".join(html_lib.unescape(re.sub(r"<[^>]+>", " ", without_inline)).split())


def _summary(body: str) -> str:
    m = re.search(r'id="review-summary">(.*?)</div>', body, re.S)
    assert m, "the page has no #review-summary"
    return _text(m.group(1))


def _toolbar(body: str) -> str:
    start = body.index('id="review-toolbar"')
    return body[start : body.index('class="card card--flush issues"', start)]


def _help(body: str) -> str:
    start = body.index('id="review-help"')
    return body[start : body.index("<script", start)]


def _row_html(body: str, file_id: int) -> str:
    """One row's markup, from its opening tag to the end of its accept form."""
    start = body.index(f'id="review-row-{file_id}"')
    return body[start : body.index("</form>", start)]


def _group_sections(body: str) -> list[str]:
    return re.findall(r'<section class="issue-group">(.*?)</section>', body, re.S)


def _class_tokens(fragment: str) -> set[str]:
    return {tok for cls in re.findall(r'class="([^"]*)"', fragment) for tok in cls.split()}


def _files(cid: int = 1) -> dict[str, str]:
    """``{relpath: status}`` for every record the collection still holds."""

    async def _go(s):
        from src.models.db import FileEntry

        rows = await s.execute(
            select(FileEntry.relpath, FileEntry.status).where(FileEntry.collection_id == cid)
        )
        return dict(rows.all())

    return _aside(_go)


def _open_event_files() -> list[int | None]:
    """The ``file_id`` of every still-open event, sorted (``None`` = detached)."""

    async def _go(s):
        from src.models.db import Event

        rows = await s.execute(
            select(Event.file_id).where(Event.acknowledged_at.is_(None)).order_by(Event.id)
        )
        return [r[0] for r in rows.all()]

    return _aside(_go)


# --- the actions: one per kind, counted, confirmed, styled by consequence ---------------------


def test_the_review_page_offers_one_action_per_kind_with_its_live_count(cairn_env):
    """Scenario "Each action names its own population and count"."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=2, modified=3)

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    page_wide = _bulk_forms(_toolbar(body))
    assert [f["action"] for f in page_wide] == [
        "/collection/1/review/adopt-changed",
        "/collection/1/review/stop-tracking",
    ]
    changes, deletions = page_wide
    assert changes["button"] == "Accept 3 changes"
    assert deletions["button"] == "Accept 2 deletions"
    assert (r["accept_changes"]["count"], r["accept_deletions"]["count"]) == (3, 2)
    # Page-wide means the page's own scope: here, the whole collection.
    for f in page_wide:
        assert (f["fields"]["dir"], f["fields"]["deep"]) == ("", "1")
        assert f["attrs"]["data-where"] == "photos"
    # Each carries its own confirm naming its own count and consequence...
    assert "'Accept 3 changed files in ' + this.dataset.where + ' as they are now?" in (
        changes["attrs"]["onsubmit"]
    )
    assert "These changes will not be reported again." in changes["attrs"]["onsubmit"]
    assert "'Accept the deletion of 2 files in ' + this.dataset.where + '?" in (
        deletions["attrs"]["onsubmit"]
    )
    # ...and is distinguishable by consequence without shouting: wording AND colour differ, and
    # neither is a filled button.
    assert "btn--warn-outline" in changes["button_class"].split()
    assert "btn--danger-outline" in deletions["button_class"].split()
    assert "btn--danger" not in _class_tokens(body)
    # Each states what is kept and what is lost, for the several files it acts on.
    assert changes["button_title"].startswith("These are the files now. Cairn recorded the new contents")
    assert deletions["button_title"].startswith("Cairn forgets these files: their records,")
    assert "Nothing on disk is deleted and the proof files stay in the proof store." in (
        deletions["button_title"]
    )
    # No control acts on both kinds: every bulk form on the page is one of the two verbs.
    every_bulk = _bulk_forms(body)
    assert len(every_bulk) == 4, "page-wide pair + one per folder group"
    assert "Accept all changes" not in body
    assert "/collection/1/review/accept" not in body
    # Every form carries its own fingerprint, minted for its own verb and folder.
    fps = [f["fields"]["population_fp"] for f in _forms(body)]
    assert all(HEX64.fullmatch(fp) for fp in fps)
    assert len(fps) == len(set(fps)) == 4 + 5
    for bad in FORBIDDEN:
        assert bad not in body


def test_an_empty_population_is_offered_no_action(cairn_env):
    """A button whose count is zero is an invitation to act on nothing."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=0)

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert r["accept_changes"] is None
    assert [f["button"] for f in _bulk_forms(body)] == ["Accept 1 deletion"]
    assert "/collection/1/review/adopt-changed" not in body
    assert not [f for f in _forms(body) if "change" in f["button"].lower()]


def test_the_button_counts_are_singular_where_the_population_is_one(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    changes, deletions = _bulk_forms(_toolbar(body))
    assert changes["button"] == "Accept 1 change"
    assert deletions["button"] == "Accept 1 deletion"
    assert "'Accept 1 changed file in ' + this.dataset.where + ' as it is now?" in (
        changes["attrs"]["onsubmit"]
    )
    assert "This change will not be reported again." in changes["attrs"]["onsubmit"]
    assert "'Accept the deletion of 1 file in ' + this.dataset.where + '?" in (
        deletions["attrs"]["onsubmit"]
    )
    assert "Its record is removed from Cairn" in deletions["attrs"]["onsubmit"]
    # The hint is about one file too: a population of one is not "these files".
    assert changes["button_title"].startswith("This is the file now. Cairn recorded the new contents")
    assert changes["button_title"].endswith("this change is not reported again.")
    assert deletions["button_title"].startswith("Cairn forgets this file: its record,")
    assert "the proof file stays in the proof store." in deletions["button_title"]
    for f in _bulk_forms(body):
        assert "these" not in f["button_title"].lower() and "their" not in f["button_title"].lower()
    for wrong in ("1 changes", "1 deletions", "1 changed files", "1 files"):
        assert wrong not in body, wrong


def test_each_row_carries_its_own_accept_control(cairn_env):
    """One legitimate edit and one suspicious deletion resolve separately (#30)."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    rows = {int(ROW_ACTION.match(f["action"]).group(2)): f for f in _row_forms(body)}
    assert sorted(rows) == [1, 2] == sorted(it["id"] for it in r["rows"])
    gone, edited = rows[1], rows[2]
    assert gone["action"] == "/collection/1/file/1/accept"
    assert gone["button"] == "Accept deletion"
    assert "btn--danger-outline" in gone["button_class"].split()
    assert edited["button"] == "Accept change"
    assert "btn--warn-outline" in edited["button_class"].split()
    # The row's form is bound to the row: its own fingerprint, the one its model row carries.
    by_id = {it["id"]: it for it in r["rows"]}
    for fid, form in rows.items():
        assert form["fields"]["population_fp"] == by_id[fid]["fp"]
        assert HEX64.fullmatch(form["fields"]["population_fp"])
    assert gone["fields"]["population_fp"] != edited["fields"]["population_fp"]
    # The collection page's forms come back to the collection page, never the fleet one.
    assert "from=fleet" not in body
    for bad in FORBIDDEN:
        assert bad not in body


def test_rows_do_not_carry_a_filled_destructive_button(cairn_env):
    """Scenario "Rows do not carry a filled destructive button".

    A column of several hundred loud buttons trains the operator to stop reading them, and puts the
    irreversible control where a tap lands by accident.
    """
    seed = _photos(cairn_env, [(f"gone/{i}.jpg", "missing") for i in range(3)])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert len(r["rows"]) == 3
    for it in r["rows"]:
        row = _row_html(body, it["id"])
        assert "btn--danger-outline" in _class_tokens(row)
        assert "btn--danger" not in _class_tokens(row), "a filled destructive button in a row"
        (form,) = _row_forms(row + "</form>")
        assert form["button"] == "Accept deletion"
        assert form["button_class"].split() == ["btn", "btn--sm", "btn--danger-outline"]


def test_a_deletion_is_confirmed_with_its_consequence(cairn_env):
    """Scenario "A deletion is confirmed with its consequence" — per file and in bulk.

    The file's name rides a data attribute rather than the confirm string, so a quote in a
    filename can neither break the confirmation nor silently skip it.
    """
    awkward = "it's \"quoted\" & <odd>.jpg"
    seed = _photos(cairn_env, [(f"gone/{awkward}", "missing"), ("gone/b.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    row = _row_forms(_row_html(body, 1) + "</form>")[0]
    assert row["attrs"]["data-relpath"] == f"gone/{awkward}"  # round-trips through the markup
    confirm = row["attrs"]["onsubmit"]
    assert confirm.startswith("return confirm('Accept the deletion of ' + this.dataset.relpath")
    assert "Its record is removed from Cairn" in confirm
    assert "Nothing on disk is deleted and no proof file is deleted." in confirm
    assert "quoted" not in confirm

    (bulk,) = _bulk_forms(_toolbar(body))
    assert "Accept the deletion of 2 files in " in bulk["attrs"]["onsubmit"]
    assert "Their records are removed from Cairn" in bulk["attrs"]["onsubmit"]
    assert "Nothing on disk is deleted and no proof file is deleted." in bulk["attrs"]["onsubmit"]


def test_the_accept_change_hint_does_not_say_notarized(cairn_env):
    """Stamping is queued only in a notarized collection and may still be pending; the row's proof
    marker reports that. The hint claims what accepting does and nothing about proofs."""
    seed = _photos(cairn_env, [
        ("edited/a.txt", "modified"), ("edited/b.txt", "modified"), ("gone/c.jpg", "missing"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    change_controls = [f for f in _forms(body) if "change" in f["button"].lower()]
    assert len(change_controls) == 2 + 2, "two rows, the page-wide action and the group's"
    for f in change_controls:
        for claim in (f["button_title"], f["attrs"]["onsubmit"]):
            assert "notariz" not in claim.lower(), claim
            assert "stamp" not in claim.lower(), claim
    row = _row_forms(_row_html(body, 1) + "</form>")[0]
    assert row["button_title"] == (
        "This is the file now. Cairn recorded the new contents when it found the change; "
        "accepting clears the issue and this change is not reported again."
    )
    # The guidance's own entry for the action says the same, and no more.
    entry = _text(_help(body).split("<dt>Accept change</dt>")[1].split("<dt>")[0])
    assert entry.startswith("This is the file now. Cairn recorded the new contents")
    assert "notariz" not in entry.lower() and "stamp" not in entry.lower()


def test_no_acknowledge_only_control_survives_on_the_review_page(cairn_env):
    """The acknowledge-only verb and its vocabulary are gone: resolving is the only way out."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    for gone in ("Mark reviewed", "Mark all", "Acknowledge", "unreviewed", "Stop tracking",
                 "Adopt ", "/ack", "hx-post"):
        assert gone not in body, gone
    # Every POST on the page is one of the two resolving verbs (bulk) or a row's own accept.
    for f in _forms(body):
        assert BULK_ACTION.match(f["action"]) or ROW_ACTION.match(f["action"]), f["action"]


# --- the fingerprints a page publishes -------------------------------------------------------


def test_every_fingerprint_on_the_page_is_the_models_and_reproduces_at_the_route(cairn_env):
    """Each form carries the fingerprint the model minted for exactly that verb, folder or row —
    and the production recount (same read, same slice, same encoder) reproduces it, so an unchanged
    page validates and nothing else does."""
    seed = _photos(cairn_env, [
        ("a/x.txt", "modified"), ("a/y.jpg", "missing"), ("b/z.jpg", "missing"), ("top.txt", "modified"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

        expected = {r["accept_changes"]["fp"], r["accept_deletions"]["fp"]}
        for g in r["groups"]:
            expected |= {a["fp"] for a in (g["accept_changes"], g["accept_deletions"]) if a}
        expected |= {it["fp"] for it in r["rows"]}
        rendered = [f["fields"]["population_fp"] for f in _forms(body)]
        assert sorted(rendered) == sorted(expected)
        assert len(rendered) == len(set(rendered)), "two forms share a fingerprint"

        async def recount(s):
            from src.control_panel.routes import (
                _narrow, _parse_scope, _population_fingerprint, _read_population,
            )
            from src.models.db import Collection

            collection = await s.get(Collection, 1)
            pop = await _read_population(s, collection)
            out = {}
            for f in _bulk_forms(body):
                verb = BULK_ACTION.match(f["action"]).group(2)
                folder = _parse_scope(f["fields"]["dir"], f["fields"]["deep"])
                out[f["fields"]["population_fp"]] = _population_fingerprint(
                    collection, _narrow(pop, verb, folder=folder)
                )
            for f in _row_forms(body):
                fid = int(ROW_ACTION.match(f["action"]).group(2))
                out[f["fields"]["population_fp"]] = _population_fingerprint(
                    collection, _narrow(pop, "accept-file", file_id=fid)
                )
            return out

        for published, recomputed in _aside(recount).items():
            assert published == recomputed


def test_the_page_is_one_snapshot_counts_rows_and_copy_list_alike(cairn_env, monkeypatch):
    """Scenario "Button counts agree with the list".

    A scan committing a new missing file after the population read (forced here inside the
    display-only history read that follows it) must not reach the summary, a button's count, the
    rows or the copy list individually: they are slices of one read, so they describe one state.
    """
    seed = _photos(cairn_env, [("gone/a.jpg", "missing"), ("gone/b.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        from src.control_panel import routes

        reads: list[int] = []
        original_read = routes._read_population
        original_facts = routes._attach_facts

        async def counting_read(session, collection, file_id=None):
            reads.append(collection.id)
            return await original_read(session, collection, file_id)

        async def racing_facts(session, collection_id, rows):
            from src.database import get_sessionmaker
            from src.models.db import FileEntry

            now = datetime.now(timezone.utc)
            async with get_sessionmaker()() as other:
                other.add(FileEntry(
                    collection_id=collection_id, relpath="gone/late.jpg", size=1,
                    sha256="f" * 64, status="missing", ots_state="none",
                    first_seen=now, last_checked=now, last_changed=now,
                ))
                await other.commit()
            return await original_facts(session, collection_id, rows)

        monkeypatch.setattr(routes, "_read_population", counting_read)
        monkeypatch.setattr(routes, "_attach_facts", racing_facts)
        body, r = _review(client)

    assert reads == [1], "the review page reads its population exactly once"
    assert _files() == {"gone/a.jpg": "missing", "gone/b.jpg": "missing", "gone/late.jpg": "missing"}
    assert r["missing"] == 2 and len(r["rows"]) == 2
    assert _summary(body).startswith("2 missing files in photos")
    (bulk,) = _bulk_forms(body)
    assert bulk["button"] == "Accept 2 deletions"
    assert len(_row_forms(body)) == 2
    assert "late.jpg" not in body.split('id="review-toolbar"')[1]


# --- nothing to review: all clear, nothing indexed, and the stale notice ---------------------


def test_all_clear_with_nothing_open_offers_no_controls(cairn_env):
    """Scenario "Nothing to review": a collection with files, all healthy. (A collection with NO
    files is a different card — see `test_a_zero_file_collections_review_page_never_reads_all_clear`.)"""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_ok_file(cid)

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert r["total"] == 0
    assert "All clear" in body
    assert "Nothing is changed or missing in this collection right now." in body
    # The summary, the actions and the guidance would describe a set that is empty.
    for absent in ('id="review-summary"', 'id="review-toolbar"', 'id="review-help"',
                   'id="review-scope"', "Copy paths"):
        assert absent not in body, absent
    assert _forms(body) == [], "an all-clear page offers no accept action"


def test_the_summary_actions_and_guidance_render_only_with_something_to_review(cairn_env):
    """#18: on an all-clear page a summary and recovery instructions described a set that is empty,
    and the page then contradicted them two lines down. With issues, all three are there."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    for present in ('id="review-summary"', 'id="review-toolbar"', 'id="review-help"'):
        assert present in body, present
    assert "All clear" not in body
    assert "Nothing is changed or missing" not in body


def test_a_zero_file_collections_review_page_never_reads_all_clear(cairn_env):
    """#31 on the review surface: watching nothing establishes nothing, so no green "All clear"."""
    root = cairn_env / "empty"
    root.mkdir()

    with _make_client(cairn_env, lambda: seed_collection(root)) as client:
        body = client.get("/collection/1/review").text

    assert "All clear" not in body
    assert "Nothing is changed or missing" not in body
    assert "No files indexed yet" in body
    assert "Nothing has been checked in this collection" in body
    assert _forms(body) == []


def test_a_concurrent_accept_between_the_two_reads_never_reads_all_clear(cairn_env, monkeypatch):
    """The review page's zero-file state must come from the population snapshot, not an earlier read.

    `_collection_view` counts the collection's files in its own statement; the protected population
    is read seconds later in another. Python's sqlite3 runs in legacy transaction mode, so those
    are two independent snapshots: an accept committed from another tab in between (accepting the
    last missing file's deletion and leaving the collection empty) left `is_empty` false while the
    population read returned nothing — and the page rendered a green "All clear" for a collection
    that has established nothing. The interleaving is forced here by mutating inside
    `_collection_view`.
    """
    from sqlalchemy import delete, update

    root = cairn_env / "raced"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        from src.control_panel import routes

        original = routes._collection_view

        async def racing_view(session, collection):
            view = await original(session, collection)
            # The concurrent accept, on its own connection: the last missing row is removed and
            # its event acknowledged AFTER the counts were read, BEFORE `_read_population` runs.
            from src.database import get_sessionmaker
            from src.models.db import Event, FileEntry

            async with get_sessionmaker()() as other:
                await other.execute(
                    delete(FileEntry).where(FileEntry.collection_id == collection.id)
                )
                await other.execute(
                    update(Event)
                    .where(Event.collection_id == collection.id)
                    .values(acknowledged_at=datetime.now(timezone.utc))
                )
                await other.commit()
            return view

        monkeypatch.setattr(routes, "_collection_view", racing_view)
        body = client.get("/collection/1/review").text

    assert "All clear" not in body
    assert "No files indexed yet" in body
    assert "Nothing has been checked in this collection" in body


def test_stale_banner_renders_only_when_the_route_says_stale(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        for falsy in ("", "?stale=", "?stale=0", "?stale=true", "?dir=gone", "?dir=gone&stale=yes"):
            body = client.get(f"/collection/1/review{falsy}").text
            assert STALE_COPY not in body and 'id="stale-banner"' not in body, falsy
        # The refusal of a scoped accept lands on the scoped view, and says so there too.
        for stale in ("?stale=1", "?dir=gone&stale=1"):
            body = client.get(f"/collection/1/review{stale}").text
            assert STALE_COPY in body and 'id="stale-banner"' in body, stale


def test_stale_banner_leads_with_the_action_not_having_been_applied(cairn_env):
    """M2: the cause is not the headline — the consequence is.

    "This collection changed since the page loaded" is a true statement *about the collection* that
    never says the operator's accept did nothing. Read at speed after clicking an irreversible
    button it parses as a status note on a page that has apparently been accepted.
    """
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        banner = client.get("/collection/1/review?stale=1").text

    assert STALE_LEAD in banner
    assert STALE_NOTHING_MUTATED in banner
    # Order: consequence, then cause. Not merely "both strings are present somewhere".
    assert banner.index(STALE_LEAD) < banner.index(STALE_COPY)
    # ...and the notice comes before the files it tells the operator to look at again.
    assert banner.index('id="stale-banner"') < banner.index('class="review-row"')
    assert "The list below is current" in banner


def test_stale_banner_refused_onto_an_empty_review_still_says_it_was_not_applied(cairn_env):
    """M2: the refusal redirects unconditionally, so it can land on an all-clear page.

    "The list below is current" over an empty page reads as if the accept had emptied it — the
    exact false impression the banner exists to prevent.
    """
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_ok_file(cid)

    with _make_client(cairn_env, seed) as client:
        banner = client.get("/collection/1/review?stale=1").text

    assert STALE_LEAD in banner
    assert STALE_NOTHING_MUTATED in banner
    assert "The list below is current" not in banner
    assert "may already have been handled" in _text(banner)
    # The all-clear card still carries the way out; no "try again" is invented for it.
    assert "Back to photos" in banner
    assert _forms(banner) == []


def test_plain_review_get_has_no_stale_banner(cairn_env):
    """Only the whitelisted `?stale=1` sets the banner; nothing else may claim a refusal."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        assert STALE_COPY not in client.get("/collection/1/review").text
        # An unrecognized value is not a refusal and must never claim one.
        assert STALE_COPY not in client.get("/collection/1/review?stale=maybe").text
        # Only the whitelisted `1` renders the refusal banner (design D14, D11's whitelist shape).
        refused = client.get("/collection/1/review?stale=1").text
        assert STALE_COPY in refused
        assert 'id="stale-banner"' in refused


# --- the facts on a row ----------------------------------------------------------------------


def test_review_lists_what_happened_to_each_file(cairn_env):
    """Scenario "Review view lists what happened to each file": what, when found, size, and whether
    a proof of prior existence is kept."""
    found = _ago(days=3)
    seed = _photos(cairn_env, [
        ("docs/gone-notarized.pdf", "missing", {"found": found, "size": 4321, "ots_state": "complete"}),
        ("docs/gone-pending.pdf", "missing", {"size": 10, "ots_state": "pending"}),
        ("docs/edited.txt", "modified", {"size": 2048, "ots_state": "incomplete"}),
        ("docs/edited-plain.txt", "modified", {"ots_state": "none"}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    gone, pending, edited, plain = (_text(_row_html(body, i).split("<form")[0]) for i in (1, 2, 3, 4))
    assert "gone-notarized.pdf" in gone
    assert "Gone from disk" in gone and "found missing 3 days ago" in gone
    assert "4.2 KiB" in gone
    assert "proof of prior existence kept" in gone
    # The absolute time of the finding rides the hover title.
    assert f'title="{found.strftime("%Y-%m-%d %H:%M UTC")}"' in _row_html(body, 1)
    # A file that was never anchored has no proof to keep, and the row does not claim one.
    assert "Gone from disk" in pending and "proof of prior existence kept" not in pending
    assert "Contents changed" in edited and "found just now" in edited and "2.0 KiB" in edited
    assert "proof of prior existence kept" in edited
    assert "Contents changed" in plain and "proof of prior existence kept" not in plain
    assert [it["notarized"] for it in sorted(r["rows"], key=lambda it: it["id"])] == [
        True, False, True, False,
    ]
    # Missing first, then changed, each by path.
    assert [it["relpath"] for it in r["rows"]] == [
        "docs/gone-notarized.pdf", "docs/gone-pending.pdf",
        "docs/edited-plain.txt", "docs/edited.txt",
    ]


def test_a_changed_row_says_when_the_file_was_last_written(cairn_env):
    """Scenario "A changed file's row says when the file was last written" — the fact that answers
    "was that me?", alongside when Cairn found the change."""
    written = datetime.now(timezone.utc) - timedelta(hours=5, minutes=2)
    seed = _photos(cairn_env, [
        ("docs/edited.txt", "modified", {"mtime": written.timestamp(), "found": _ago(days=2)}),
        ("docs/no-mtime.txt", "modified", {"mtime": None}),
        ("docs/gone.txt", "missing", {"mtime": written.timestamp()}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    edited = _row_html(body, 1)
    story = _text(edited.split("<form")[0])
    assert "found 2 days ago" in story
    assert "file last written 5 hours ago" in story
    assert story.index("found 2 days ago") < story.index("file last written 5 hours ago")
    assert f'title="{written.strftime("%Y-%m-%d %H:%M UTC")}"' in edited
    # No modification time on record -> the row does not invent one.
    assert "file last written" not in _row_html(body, 2)
    # A missing file has no current contents to have been written.
    assert "file last written" not in _row_html(body, 3)
    assert {it["id"]: it["written"] for it in r["rows"]}[2] is None


def test_a_file_that_keeps_changing_says_so(cairn_env):
    """Scenario "A file that keeps changing says so": how many changes are on record, since when."""
    first, second = _local_noon(20), _local_noon(9)
    seed = _photos(cairn_env, [
        ("docs/churner.txt", "modified", {
            "history": [("modified", first, True), ("modified", second, True)],
        }),
        ("docs/once.txt", "modified"),
        ("docs/gone.txt", "missing", {"history": [("modified", first, True), ("modified", second, True)]}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    churner = _text(_row_html(body, 1).split("<form")[0])
    assert f"changed 3 times since {_day_words(20)}" in churner
    # One change on record is just "a change": no repeat claim, and never "changed 1 times".
    assert "times since" not in _row_html(body, 2)
    # The repeat count is a fact about changed files; a missing row does not carry it.
    assert "times since" not in _row_html(body, 3)
    by_id = {it["id"]: it for it in r["rows"]}
    assert (by_id[1]["changes"], by_id[2]["changes"]) == (3, 0)


# --- a file that came back different ---------------------------------------------------------


async def _scan_a_changed_restore(root, *, mode: str = "worm") -> tuple[int, str, str]:
    """Drive the REAL scanner through present → absent → back-with-different-bytes.

    Seeding the event by hand would prove the template renders a string a test wrote; this proves
    the panel renders what the scanner actually records, `detail` digests included.

    Returns ``(collection_id, recorded_digest, found_digest)``.
    """
    import hashlib

    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    original, impostor = "the real deed", "a different document entirely"
    root.mkdir(parents=True, exist_ok=True)
    (root / "deed.pdf").write_text(original)
    # A second file keeps the folder from reading as empty (a scan refuses an empty root while
    # files are tracked — add-scan-safety-nets); this test is about one file.
    (root / "bystander.txt").write_text("always here")
    # `ots_mode="none"`: this is about the rendered row, and a `perfile` collection would send the
    # post-scan stamp pass at a calendar.
    cid = await seed_collection(root, ots_mode="none", mode=mode)

    async def scan():
        async with get_sessionmaker()() as s:
            return await scan_collection(s, await s.get(Collection, cid))

    await scan()
    (root / "deed.pdf").unlink()
    await scan()
    (root / "deed.pdf").write_text(impostor)
    summary = await scan()
    assert summary.restored_changed == 1, "the fixture did not produce a changed restore"
    return (
        cid,
        hashlib.sha256(original.encode()).hexdigest(),
        hashlib.sha256(impostor.encode()).hexdigest(),
    )


@pytest.mark.parametrize("mode", ["worm", "churn"])
def test_the_review_row_draws_a_changed_restore_as_an_alarm_with_both_digests(cairn_env, mode):
    """Scenario "The review row names a changed reappearance as such" — in BOTH policies.

    `restored_changed` is the one event that tells "your file came back" from "something else came
    back in its place". Drawn as an ordinary change — or, in a Working collection where edits are
    not flagged, not drawn at all — it would restore the false reassurance the scanner fix removed.
    Driven through the real scanner, so the digests are the ones it actually recorded.
    """
    root = cairn_env / "photos"
    captured: dict[str, str] = {}

    async def seed():
        _cid, recorded, found = await _scan_a_changed_restore(root, mode=mode)
        captured.update(recorded=recorded, found=found)

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        with _capture_context() as renders:
            dashboard = client.get("/").text
            collection_page = client.get("/collection/1").text
        dash_ctx = [c for name, c in renders if name == "dashboard.html"][0]

    # It is an issue like any other changed file: listed, counted, and not "all clear" — on the
    # review page, on the collection's own page, or on the dashboard.
    assert (r["modified"], r["missing"]) == (1, 0)
    assert "All clear" not in body
    assert "All clear" not in collection_page
    assert 'href="/collection/1/review"' in collection_page
    assert dash_ctx["needs_total"] == 1 and [n["id"] for n in dash_ctx["needs"]] == [1]
    assert "Nothing needs you" not in dashboard and "All clear" not in dashboard
    assert _summary(body).startswith("1 changed file in photos")
    (item,) = r["rows"]
    assert item["kind"] == "came_back"

    row = _row_html(body, item["id"])
    assert "deed.pdf" in row, "an alarm is useless without the file it names"
    # Its own label, with the weight of an alarm...
    assert '<span class="review-row__alarm">Came back different</span>' in row
    assert "review-row__icon--danger" in row and "review-row__icon--warn" not in row
    # ...and never the wording of an ordinary change or of a benign reappearance.
    assert "Contents changed" not in row
    assert "Restored" not in row and "restored" not in _text(row.split("<form")[0])

    detail = f"recorded {captured['recorded']} → found {captured['found']}"
    assert f'<div class="review-row__detail mono">{detail}</div>' in row, (
        "both digests in full, on their own line: truncating either costs the operator the only "
        "surviving record of what the file was before it came back"
    )
    # The digest pair is never inside the path line (which may clip) or a hover title.
    path_line = row[row.index('class="review-row__path"') : row.index('class="review-row__story"')]
    assert "recorded " not in path_line
    # The file still resolves like any changed file.
    (form,) = _row_forms(row + "</form>")
    assert form["button"] == "Accept change"


def test_the_digest_line_wraps_instead_of_truncating():
    """ "In full and at any viewport width": the detail line's own CSS must let 146 characters of
    hex wrap, and must not clip, ellipsize or forbid wrapping."""
    css = Path("src/control_panel/static/css/panel.css").read_text()
    rules = re.findall(r"\.review-row__detail\s*\{([^}]*)\}", css)
    assert rules, "no .review-row__detail rule"
    for rule in rules:
        assert "ellipsis" not in rule and "nowrap" not in rule and "overflow: hidden" not in rule
    assert any("overflow-wrap: anywhere" in rule or "word-break: break-all" in rule for rule in rules)


def test_a_changed_restore_that_changed_again_is_still_drawn_as_an_alarm(cairn_env):
    """A file that came back different stays that alarm for as long as that event is open.

    In a Locked collection a further write opens a newer `modified` event on the same file. If the
    row were labelled from the newest event alone it would read "Contents changed" and the digest
    pair — the only record of what the file was before it came back — would never be shown, while
    accepting the row closes that alarm too. It must never be accepted without having been shown.
    """
    detail = "recorded " + "1" * 64 + " → found " + "2" * 64
    seed = _photos(cairn_env, [
        ("deed.pdf", "modified", {
            "history": [("restored_changed", _local_noon(2), False)],
            "found": _local_noon(1), "kind": "modified",
        }),
    ])

    async def with_detail():
        from sqlalchemy import update

        from src.database import get_sessionmaker
        from src.models.db import Event

        await seed()
        async with get_sessionmaker()() as s:
            await s.execute(
                update(Event).where(Event.kind == "restored_changed").values(detail=detail)
            )
            await s.commit()

    with _make_client(cairn_env, with_detail) as client:
        body, r = _review(client)
        assert _open_event_files() == [1, 1], "both alarms are open when the page renders"

        (item,) = r["rows"]
        assert (r["modified"], item["kind"]) == (1, "came_back")
        row = _row_html(body, 1)
        assert '<span class="review-row__alarm">Came back different</span>' in row
        assert "review-row__icon--danger" in row and "review-row__icon--warn" not in row
        assert "Contents changed" not in row
        assert f'<div class="review-row__detail mono">{detail}</div>' in row
        # The later write is still on the row: it is counted among the changes on record.
        assert "changed 2 times since" in _text(row.split("<form")[0])

        # Accepting the row — now that the alarm has been shown — closes both events.
        (form,) = _row_forms(row + "</form>")
        resp = _submit(client, form)
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review"

    assert _files() == {"deed.pdf": "ok"}
    assert _open_event_files() == []


# --- grouping by folder ----------------------------------------------------------------------


def test_files_in_several_folders_are_grouped(cairn_env):
    """Scenario "Files in several folders are grouped": one group per folder below the common one,
    each naming its folder, its counts and when its files were found, each with its own action per
    kind present — and ordered newest finding first, not by path."""
    seed = _photos(cairn_env, [
        ("trips/zebra/a.jpg", "missing"),
        ("trips/zebra/b.jpg", "missing"),
        ("trips/alpha/c.jpg", "missing", {"found": _local_noon(3)}),
        ("trips/alpha/sub/d.txt", "modified", {"found": _local_noon(5)}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert r["flat"] is False
    # Newest finding first: zebra (today) before alpha (3 days ago), against path order.
    assert [(g["label"], g["dir"], g["deep"]) for g in r["groups"]] == [
        ("trips/zebra/", "trips/zebra", True),
        ("trips/alpha/", "trips/alpha", True),
    ]
    assert [(g["missing"], g["modified"], g["total"]) for g in r["groups"]] == [(2, 0, 2), (1, 1, 2)]

    zebra, alpha = _group_sections(body)
    assert "trips/zebra/" in _text(zebra) and "trips/alpha/" in _text(alpha)
    # Counts per kind, only the kinds present.
    assert "2 missing" in _text(zebra) and "changed" not in _text(zebra.split("<form")[0])
    assert "1 missing" in _text(alpha) and "1 changed" in _text(alpha)
    # When they were found: one day, or a range.
    assert "found today" in _text(zebra)
    assert f"found {_day_words(5)} – {_day_words(3)}" in _text(alpha)
    # One bulk action per kind present, acting on that group only (a folder and its subtree).
    (z_del,) = _bulk_forms(zebra)
    assert z_del["action"] == "/collection/1/review/stop-tracking"
    assert z_del["button"] == "Accept 2 deletions"
    assert (z_del["fields"]["dir"], z_del["fields"]["deep"]) == ("trips/zebra", "1")
    assert z_del["attrs"]["data-where"] == "trips/zebra/"
    a_chg, a_del = _bulk_forms(alpha)
    assert (a_chg["action"], a_chg["button"]) == ("/collection/1/review/adopt-changed", "Accept 1 change")
    assert (a_del["action"], a_del["button"]) == ("/collection/1/review/stop-tracking", "Accept 1 deletion")
    for f in (a_chg, a_del):
        assert (f["fields"]["dir"], f["fields"]["deep"]) == ("trips/alpha", "1")
    # Each group's rows are its own; inside a group the row shows only what lies below its folder.
    assert len(_row_forms(zebra)) == 2 and len(_row_forms(alpha)) == 2
    assert 'review-row__dir mono">sub/</span>' in alpha
    assert 'review-row__dir' not in zebra
    # Each group's heading opens that folder as the page's scope.
    assert 'href="/collection/1/review?dir=trips%2Fzebra"' in zebra
    assert 'href="/collection/1/review?dir=trips%2Falpha"' in alpha


def test_files_directly_in_the_common_folder_get_their_own_group(cairn_env):
    """Scenario "Files directly in the common folder get their own group" — and its bulk action
    leaves the subfolders' files alone, which is proved by submitting it."""
    seed = _photos(cairn_env, [
        ("docs/top-a.txt", "missing"),
        ("docs/top-b.txt", "missing"),
        ("docs/sub/inner.txt", "missing"),
        ("docs/sub/deeper/x.txt", "missing"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

        by_label = {g["label"]: g for g in r["groups"]}
        assert set(by_label) == {"docs/ (this folder only)", "docs/sub/"}
        direct = by_label["docs/ (this folder only)"]
        assert (direct["dir"], direct["deep"], direct["total"]) == ("docs", False, 2)
        assert direct["query"] == "dir=docs&deep=0"

        section = next(s for s in _group_sections(body) if "this folder only" in s)
        # Its link opens exactly that set: the folder WITHOUT its subfolders.
        assert 'href="/collection/1/review?dir=docs&amp;deep=0"' in section
        (form,) = _bulk_forms(section)
        assert form["button"] == "Accept 2 deletions"
        assert (form["fields"]["dir"], form["fields"]["deep"]) == ("docs", "0")

        resp = _submit(client, form)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/collection/1/review"

    assert _files() == {"docs/sub/inner.txt": "missing", "docs/sub/deeper/x.txt": "missing"}


def test_opening_a_direct_files_group_does_not_widen_it(cairn_env):
    """Scenario "Opening a direct-files group does not widen it": `deep=0` excludes subfolders."""
    seed = _photos(cairn_env, [
        ("docs/top.txt", "missing"),
        ("docs/sub/inner.txt", "missing"),
        ("docs/sub/edited.txt", "modified"),
    ])

    with _make_client(cairn_env, seed) as client:
        narrow_body, narrow = _review(client, "/collection/1/review?dir=docs&deep=0")
        _wide_body, wide = _review(client, "/collection/1/review?dir=docs")

    assert [it["relpath"] for it in narrow["rows"]] == ["docs/top.txt"]
    assert (narrow["total"], narrow["missing"], narrow["modified"]) == (1, 1, 0)
    assert narrow["scope_deep"] is False and narrow["flat"] is True
    assert "inner.txt" not in narrow_body and "edited.txt" not in narrow_body
    # Its actions are bound to the same narrow scope — and there is nothing to accept as a change.
    (form,) = _bulk_forms(narrow_body)
    assert form["button"] == "Accept 1 deletion"
    assert (form["fields"]["dir"], form["fields"]["deep"]) == ("docs", "0")
    assert _summary(narrow_body).startswith("1 missing file in photos, under docs/ (this folder only)")
    # The default (whole subtree) holds all three.
    assert wide["total"] == 3 and wide["scope_deep"] is True


def test_files_in_one_folder_are_a_single_list(cairn_env):
    """Scenario "Files in one folder are a single list" — not a group of one."""
    seed = _photos(cairn_env, [
        ("one/a.jpg", "missing"), ("one/b.jpg", "missing"), ("one/c.txt", "modified"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert r["flat"] is True and len(r["groups"]) == 1
    assert _group_sections(body) == [], "a single folder must not be drawn as a group"
    # No group header forms: the page-wide actions in the toolbar ARE this list's bulk actions.
    assert r["groups"][0]["accept_changes"] is None and r["groups"][0]["accept_deletions"] is None
    assert [f["button"] for f in _bulk_forms(body)] == ["Accept 1 change", "Accept 2 deletions"]
    assert len(_bulk_forms(_toolbar(body))) == 2
    # The one folder is named once, above the list, and the rows are all there.
    assert '<span class="mono">one/</span>' in body
    assert len(_row_forms(body)) == 3
    assert "review-row__dir" not in body


def test_files_at_the_collection_root_are_a_single_unlabelled_list(cairn_env):
    seed = _photos(cairn_env, [("a.jpg", "missing"), ("b.txt", "modified")])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert r["flat"] is True
    assert _group_sections(body) == [] and "issues__where" not in body
    assert len(_row_forms(body)) == 2


def test_a_group_larger_than_the_open_limit_is_collapsed(cairn_env):
    """A group of up to REVIEW_GROUP_OPEN_MAX files is open; a larger one is a collapsed
    `<details>` — closed by default — that says how many files it holds."""
    from src.control_panel.routes import REVIEW_GROUP_OPEN_MAX as OPEN_MAX

    seed = _photos(cairn_env, (
        [(f"big/{i:02d}.jpg", "missing") for i in range(OPEN_MAX + 1)]
        + [(f"small/{i:02d}.jpg", "missing") for i in range(OPEN_MAX)]
    ))

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    by_dir = {g["dir"]: g for g in r["groups"]}
    assert (by_dir["big"]["open"], by_dir["small"]["open"]) == (False, True)
    big = next(s for s in _group_sections(body) if 'value="big"' in s)
    small = next(s for s in _group_sections(body) if 'value="small"' in s)
    details = re.search(r"<details([^>]*)>\s*<summary>(.*?)</summary>", big, re.S)
    assert details, "the large group is not a <details>"
    assert "open" not in details.group(1), "a large group must be collapsed by default"
    assert _text(details.group(2)) == f"Show {OPEN_MAX + 1} files"
    # Its rows are all inside the collapsed element — none is left outside it.
    assert big.index("<details") < big.index('class="review-row"')
    assert big.count('class="review-row"') == OPEN_MAX + 1
    assert "<details" not in small and small.count('class="review-row"') == OPEN_MAX
    # Collapsing hides rows, never counts: the header and the action still state the whole group.
    assert f"{OPEN_MAX + 1} missing" in _text(big)
    assert _bulk_forms(big)[0]["button"] == f"Accept {OPEN_MAX + 1} deletions"


def test_a_group_past_its_row_limit_says_how_many_more_and_links_to_the_scoped_view(cairn_env):
    """Scenario "A large group is collapsed and can be opened as its own scope"."""
    from src.control_panel.routes import REVIEW_GROUP_ROW_LIMIT as LIMIT

    seed = _photos(cairn_env, (
        [(f"huge/{i:04d}.jpg", "missing") for i in range(LIMIT + 3)]
        + [("other/kept-apart.jpg", "missing")]
    ))

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        huge_model = next(g for g in r["groups"] if g["dir"] == "huge")
        assert (huge_model["total"], huge_model["shown"], huge_model["more"]) == (LIMIT + 3, LIMIT, 3)

        huge = next(s for s in _group_sections(body) if 'value="huge"' in s)
        assert "<details" in huge and huge.count('class="review-row"') == LIMIT
        assert f"Show the first {LIMIT} files · 3 more not shown" in _text(huge)
        assert "3 more files in this folder are not shown." in _text(huge)
        # The way to see them: the same view scoped to exactly this folder, and the file browser.
        assert 'href="/collection/1/review?dir=huge"' in huge
        assert 'href="/collection/1?view=list&amp;filter=issues"' in huge
        # The count in the header is the group's, not the truncated list's.
        assert f"{LIMIT + 3:,} missing" in _text(huge) or f"{LIMIT + 3} missing" in _text(huge)

        # Following the link restricts the view to exactly the files the group held, and offers
        # the way back to the wider scope.
        scoped_body, scoped = _review(client, "/collection/1/review?dir=huge")

    assert scoped["total"] == LIMIT + 3 and scoped["collection_total"] == LIMIT + 4
    # ...where, as the page's own single list, it is no longer cut at the per-group limit: the
    # files the group could not show are all there.
    assert scoped["flat"] is True
    assert (scoped["shown"], scoped["truncated"]) == (LIMIT + 3, False)
    assert len(_row_forms(scoped_body)) == LIMIT + 3
    assert "kept-apart.jpg" not in scoped_body
    assert 'id="review-scope"' in scoped_body
    assert 'href="/collection/1/review"' in scoped_body


def test_an_action_over_files_that_are_not_shown_says_so(cairn_env):
    """Scenario "An action over files that are not shown says so" — in the hint AND the confirm.

    The action stays (an incident of thousands of files cannot be listed in full, and the guard
    hashes the whole population), but it never hides that it reaches past the page.
    """
    from src.control_panel.routes import REVIEW_GROUP_ROW_LIMIT as LIMIT

    seed = _photos(cairn_env, (
        [(f"huge/{i:04d}.jpg", "missing") for i in range(LIMIT + 1)]
        + [("huge/zz-edit-1.txt", "modified"), ("huge/zz-edit-2.txt", "modified")]
        + [("small/a.jpg", "missing")]
    ))

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    huge = next(s for s in _group_sections(body) if 'value="huge"' in s)
    small = next(s for s in _group_sections(body) if 'value="small"' in s)
    changes, deletions = _bulk_forms(huge)
    # Missing files are listed first, so the group's row limit cut one deletion and both changes.
    assert deletions["button"] == f"Accept {LIMIT + 1} deletions"
    assert "includes 1 not shown here" in deletions["text"]
    assert "1 of them is not shown on this page." in deletions["attrs"]["onsubmit"]
    assert changes["button"] == "Accept 2 changes"
    assert "includes 2 not shown here" in changes["text"]
    assert "2 of them are not shown on this page." in changes["attrs"]["onsubmit"]
    # The page-wide actions cover the same unseen files and say so too.
    page_changes, page_deletions = _bulk_forms(_toolbar(body))
    assert "includes 2 not shown here" in page_changes["text"]
    assert "2 of them are not shown on this page." in page_changes["attrs"]["onsubmit"]
    assert "includes 1 not shown here" in page_deletions["text"]
    assert "1 of them is not shown on this page." in page_deletions["attrs"]["onsubmit"]
    # An action whose every file is on the page makes no such statement.
    (small_del,) = _bulk_forms(small)
    assert "not shown" not in small_del["text"] and "not shown" not in small_del["attrs"]["onsubmit"]
    # The model's numbers are the population's, and "hidden" is exactly population minus rows.
    g = next(g for g in r["groups"] if g["dir"] == "huge")
    assert (g["accept_deletions"]["count"], g["accept_deletions"]["hidden"]) == (LIMIT + 1, 1)
    assert (g["accept_changes"]["count"], g["accept_changes"]["hidden"]) == (2, 2)
    # ...and the view links to where the full set can be listed.
    assert 'id="review-truncated"' in body
    assert 'href="/collection/1?view=list&amp;filter=issues"' in body


def test_a_scoped_view_acts_only_inside_its_scope_and_offers_the_way_back(cairn_env):
    """Scenario "A scoped view acts only inside its scope and offers the way back"."""
    seed = _photos(cairn_env, [
        ("a/x.txt", "modified"),
        ("a/sub/y.jpg", "missing"),
        ("b/z.jpg", "missing"),
        ("ab/near-miss.jpg", "missing"),  # `a` is a folder, not a prefix: this is NOT inside it
    ])

    with _make_client(cairn_env, seed) as client:
        root_body, _root = _review(client)
        assert 'id="review-scope"' not in root_body, "the whole collection is not 'a scope'"

        body, r = _review(client, "/collection/1/review?dir=a")
        # Lists and counts that folder's issues only...
        assert sorted(it["relpath"] for it in r["rows"]) == ["a/sub/y.jpg", "a/x.txt"]
        assert (r["total"], r["modified"], r["missing"], r["collection_total"]) == (2, 1, 1, 4)
        assert _summary(body).startswith("1 changed and 1 missing files in photos, under a/")
        assert "z.jpg" not in body and "near-miss" not in body  # rows AND the copy list
        # ...names the folder it is scoped to, and links back to the collection-wide view.
        scope_bar = body[body.index('id="review-scope"') : body.index('id="review-toolbar"')]
        assert "Showing only" in _text(scope_bar) and "a/" in _text(scope_bar)
        assert 'href="/collection/1/review"' in scope_bar
        assert "Show everything in photos (4)" in _text(scope_bar)
        # Its page-wide actions are that folder's.
        changes, deletions = _bulk_forms(_toolbar(body))
        assert (changes["button"], deletions["button"]) == ("Accept 1 change", "Accept 1 deletion")
        for f in (changes, deletions):
            assert (f["fields"]["dir"], f["fields"]["deep"]) == ("a", "1")
            assert f["attrs"]["data-where"] == "a/"
            assert (f["fields"]["return_dir"], f["fields"]["return_deep"]) == ("a", "1")

        resp = _submit(client, deletions)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/collection/1/review?dir=a"

    assert _files() == {
        "a/x.txt": "modified", "b/z.jpg": "missing", "ab/near-miss.jpg": "missing",
    }


def test_a_scope_with_nothing_in_it_is_not_an_all_clear(cairn_env):
    """A folder with no issues, opened as a scope while the collection has issues elsewhere, must
    not borrow the collection's "All clear" — it says where the open decisions still are."""
    seed = _photos(cairn_env, [("b/z.jpg", "missing"), ("b/w.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client, "/collection/1/review?dir=nothing-here")

    assert (r["total"], r["collection_total"]) == (0, 2)
    assert "All clear" not in body and "Nothing is changed or missing" not in body
    assert "Nothing to review in this folder" in body
    assert "2 files elsewhere in photos still need a decision." in body
    assert 'href="/collection/1/review"' in body
    assert _forms(body) == []


@pytest.mark.parametrize(
    "query",
    [
        "?dir=/abs",              # a leading slash is not a collection-relative folder
        "?dir=a/",                # nor is a trailing one
        "?dir=a&deep=2",          # an unknown depth flag
        "?dir=a&deep=",           # ...or an empty one
        "?dir=a%00b",             # a NUL
        "?dir=" + "a" * 5000,     # over the length bound
    ],
)
def test_a_malformed_scope_falls_back_to_the_whole_collection(cairn_env, query):
    """A scope that no page could have rendered is not interpreted: the view shows the whole
    collection (it selects only what is displayed — every action carries its own bound scope)."""
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("b/y.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client, f"/collection/1/review{query}")

    assert r["scope_is_root"] is True and (r["scope_dir"], r["scope_deep"]) == ("", True)
    assert r["total"] == 2 and len(r["rows"]) == 2
    assert 'id="review-scope"' not in body
    for f in _bulk_forms(_toolbar(body)):
        assert (f["fields"]["dir"], f["fields"]["deep"]) == ("", "1")
        assert (f["fields"]["return_dir"], f["fields"]["return_deep"]) == ("", "1")


def test_the_summary_says_when_the_issues_were_found(cairn_env):
    """Scenario "The summary says when the issues were found": each day, with how many it found."""
    seed = _photos(cairn_env, [
        ("a/1.jpg", "missing"),
        ("a/2.jpg", "missing"),
        ("a/3.txt", "modified", {"found": _local_noon(1)}),
        ("b/4.jpg", "missing", {"found": _local_noon(9)}),
        ("b/5.jpg", "missing", {"found": _local_noon(9)}),
        ("b/6.jpg", "missing", {"found": _local_noon(9)}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert _summary(body) == (
        "1 changed and 5 missing files in photos "
        f"— found today (2), yesterday (1), {_day_words(9)} (3)."
    )
    assert r["found_days"] == [
        {"label": "today", "count": 2},
        {"label": "yesterday", "count": 1},
        {"label": _day_words(9), "count": 3},
    ]
    # The days' counts account for every listed file.
    assert sum(d["count"] for d in r["found_days"]) + r["found_earlier"] == r["total"] == 6


def test_a_single_day_of_findings_is_named_without_a_redundant_count(cairn_env):
    seed = _photos(cairn_env, [("a.jpg", "missing"), ("b.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    assert _summary(body) == "2 missing files in photos — found today."


def test_findings_on_more_days_than_the_summary_names_are_still_all_counted(cairn_env):
    """The summary names the newest days and lumps the rest as "earlier" — never drops them."""
    seed = _photos(cairn_env, [
        (f"f{d}.jpg", "missing", {"found": _local_noon(d)}) for d in (0, 1, 2, 3, 4, 5)
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

    assert len(r["found_days"]) == 4 and r["found_earlier"] == 2
    assert _summary(body).endswith(
        f"found today (1), yesterday (1), {_day_words(2)} (1), {_day_words(3)} (1), earlier (2)."
    )


def test_a_group_past_the_page_row_budget_keeps_its_header_and_says_its_files_are_not_listed(
    cairn_env, monkeypatch
):
    """Rows are budgeted per page; counts and groups are not. A group the budget never reached
    still names its folder and its counts, says its files are not listed, and links to itself as
    the page's scope — but it offers NO bulk action: that would be the one place a click removes
    what the operator cannot see at all. The action is offered where its rows are."""
    from src.control_panel import routes

    monkeypatch.setattr(routes, "REVIEW_ROW_LIMIT", 2)
    seed = _photos(cairn_env, [
        ("new/a.jpg", "missing"), ("new/b.jpg", "missing"),
        ("old/c.jpg", "missing", {"found": _local_noon(4)}),
        ("old/d.txt", "modified", {"found": _local_noon(4)}),
    ])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)

        assert (r["total"], r["shown"], r["truncated"]) == (4, 2, True)
        assert [(g["dir"], g["shown"], g["total"]) for g in r["groups"]] == [("new", 2, 2), ("old", 0, 2)]
        new = next(s for s in _group_sections(body) if 'value="new"' in s)
        old = next(s for s in _group_sections(body) if "old/" in _text(s))
        # The group with nothing listed: header, counts, the statement and the link — no form.
        assert 'class="review-row"' not in old
        assert "1 missing" in _text(old) and "1 changed" in _text(old)
        assert "These 2 files are not listed on this page — open old/ to see them." in _text(old)
        assert 'href="/collection/1/review?dir=old"' in old
        assert _forms(old) == [], "a group that lists none of its files must offer no action"
        assert "Accept" not in _text(old)
        # The group that IS listed keeps its action.
        assert [f["button"] for f in _bulk_forms(new)] == ["Accept 2 deletions"]
        # The page says how much of the whole it is showing, and where the rest can be listed.
        notice = body[body.index('id="review-truncated"') :].split("</div>")[0]
        assert "Showing 2 of 4 files on this page" in _text(notice)
        assert 'href="/collection/1?view=list&amp;filter=issues"' in notice
        # The summary and the page-wide actions still describe all four — and those say what they
        # reach beyond the page.
        assert _summary(body).startswith("1 changed and 3 missing files in photos")
        page_changes, page_deletions = _bulk_forms(_toolbar(body))
        assert (page_changes["button"], page_deletions["button"]) == ("Accept 1 change", "Accept 3 deletions")
        assert "includes 1 not shown here" in page_changes["text"]
        assert "includes 1 not shown here" in page_deletions["text"]

        # The page the group links to DOES offer the action — beside the rows it acts on.
        scoped_body, scoped = _review(client, "/collection/1/review?dir=old")

    assert sorted(it["relpath"] for it in scoped["rows"]) == ["old/c.jpg", "old/d.txt"]
    assert len(_row_forms(scoped_body)) == 2
    changes, deletions = _bulk_forms(_toolbar(scoped_body))
    assert (changes["button"], deletions["button"]) == ("Accept 1 change", "Accept 1 deletion")
    for f in (changes, deletions):
        assert (f["fields"]["dir"], f["fields"]["deep"]) == ("old", "1")
        assert "not shown" not in f["text"]


# --- the ways in -----------------------------------------------------------------------------


def test_the_issue_counts_are_links_into_the_review_view(cairn_env):
    """Scenarios "Dashboard issue count links to the review view" and "The keyboard-activated issue
    count responds to both activation keys"; plus the collection page's own statistic and action."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=2, modified=1)

    with _make_client(cairn_env, seed) as client:
        dashboard = client.get("/").text
        detail = client.get("/collection/1").text
        assert client.get("/collection/1/review").status_code == 200

    # The card's issue count: a focusable link (a span, since the card itself is the anchor) that
    # opens the review view on click, on Enter and on Space.
    tag = re.search(r'<span class="legend__review"[^>]*>', dashboard)
    assert tag, "the dashboard card has no issue-count link"
    link = tag.group(0)
    assert 'role="link"' in link and 'tabindex="0"' in link
    go = "window.location.href='/collection/1/review'"
    onclick = re.search(r'onclick="([^"]*)"', link).group(1)
    onkey = re.search(r'onkeydown="([^"]*)"', link).group(1)
    assert go in html_lib.unescape(onclick)
    assert go in html_lib.unescape(onkey)
    for key in ("event.key==='Enter'", "event.key===' '"):
        assert key in html_lib.unescape(onkey), key
    assert "event.preventDefault()" in onkey  # Space must not scroll the page instead
    card_counts = _text(dashboard[tag.end() :].split("legend__cta")[0])
    assert "2 missing" in card_counts and "1 changed" in card_counts
    # The collection page: its primary action and its changed-or-missing statistic both go there.
    assert detail.count('href="/collection/1/review"') >= 2
    assert "Review 3 issues" in _text(detail)


# --- files first -----------------------------------------------------------------------------


def test_the_first_file_comes_before_the_instructions(cairn_env):
    """Scenario "The files come before the instructions": an operator arriving from an alert on a
    phone reaches the first file without scrolling past what the actions do or how to recover."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    first_row = body.index('class="review-row"')
    assert first_row < body.index('id="review-help"')
    # Heading, one-line summary, page-wide actions — then the files. In that order.
    assert (
        body.index("<h1>What changed</h1>")
        < body.index('id="review-summary"')
        < body.index('id="review-toolbar"')
        < first_row
    )
    # Nothing explanatory sits between the heading and the first file.
    before = body[body.index("<h1>What changed</h1>") : first_row]
    for instruction in ("What the actions do", "To get files back instead", "How to recover",
                        "guide__", "recovery__steps", "backup tool"):
        assert instruction not in before, instruction
    # The explanation is still one tap away from the toolbar.
    assert 'href="#review-help"' in _toolbar(body)


# --- recovery guidance -----------------------------------------------------------------------


def _guidance(cairn_env) -> tuple[str, str]:
    """``(help_html, whole_body)`` for a collection with one changed and one missing file."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_and_modified(cid, missing=1, modified=1)

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
    return _help(body), body


def test_recovery_guidance_names_both_outcomes_of_a_rescan(cairn_env):
    """Scenario "Recovery guidance names both outcomes of a rescan".

    A restored missing file is compared against the digest recorded for it. Telling the operator a
    restore-and-rescan returns it to healthy unconditionally promises a verification the product
    does not perform — the failure class this view exists to close.
    """
    help_html, _body = _guidance(cairn_env)
    text = _text(help_html)

    # Healthy only where the bytes match what was recorded — and what closes is the MISSING
    # alert: a file that also had an unaccepted change is not cleared by coming back.
    assert (
        "A missing file that comes back is re-read and compared against the fingerprint recorded "
        "before it went missing. If it matches, the missing alert closes by itself (a file that "
        "also had an unaccepted change stays listed as changed)."
    ) in text
    assert "the issue closes by itself" not in text
    # ...and otherwise the obsolete missing alert is closed and replaced by a new issue.
    assert (
        "If it does not — the wrong snapshot, a truncated restore — the missing alert is closed "
        "and replaced by a new came back different issue for you to check."
    ) in text
    # The step-by-step version states the same two outcomes.
    assert (
        "A missing file that reappeared with the recorded contents returns to healthy; one whose "
        "contents differ is flagged came back different — check that one before you trust it."
    ) in text
    # Every "returns to healthy" in the guidance is the conditional one.
    assert text.count("returns to healthy") == text.count("with the recorded contents returns to healthy")


def test_recovery_guidance_states_the_exact_condition_for_a_changed_file(cairn_env):
    """Scenario "Recovery guidance states the exact condition for a changed file", and
    "The forbidden reassurances are not rendered"."""
    help_html, body = _guidance(cairn_env)
    text = _text(help_html)

    # What the scan actually does since it keeps the contents a change replaced: a changed file
    # put back EXACTLY as it was closes its own issue; any other version is one more change.
    assert (
        "A changed file closes its own issue too — but only if it is put back exactly as it was "
        "before the change (Cairn keeps that fingerprint until you accept). Any other version "
        "shows up as one more change."
    ) in text
    assert "No earlier version is kept on record to compare against." not in text
    # ...and it does not widen that into "an earlier version clears it".
    assert not re.search(
        r"(earlier|previous|original|older) version[^.]*"
        r"(clears|closes|resolves|returns? (it )?to (a )?healthy|healthy again)",
        text,
        re.I,
    )
    # Neither forbidden reassurance appears anywhere on the page: hints, confirms or guidance.
    for bad in FORBIDDEN:
        assert bad not in body and bad not in text, bad
    assert not re.search(r"rescan[^.]*(revers|undo|undoes|bring[s]? (it|them) back)", _text(body), re.I)
    # What accepting a deletion loses is the LINK to the proof; the proof file stays.
    assert "the proof file stays in the proof store" in text


def test_recovery_guidance_names_the_accept_deletion_action_by_its_label(cairn_env):
    """Scenario "Recovery guidance names the action for unwanted files" — the label the control
    actually carries, and no control the view no longer offers."""
    help_html, body = _guidance(cairn_env)

    assert "For files you don't want back, use <strong>Accept deletion</strong>." in help_html
    assert "For anything you don't want back, use <strong>Accept deletion</strong>" in help_html
    # ...and that label is on the page's controls.
    assert "Accept deletion" in {f["button"] for f in _row_forms(body)}
    for retired in ("Stop tracking", "Accept all changes", "Mark reviewed", "Baseline"):
        assert retired not in body, retired


def test_recovery_guidance_is_tool_neutral_and_refers_to_the_copy_button(cairn_env):
    """Scenario "Recovery guidance is offered without assuming a backup tool"; and #14: "the list
    above" names nothing — the paths are on the clipboard, not on the page."""
    help_html, body = _guidance(cairn_env)
    text = _text(help_html)

    assert "restore them with your own backup tool" in text
    assert "it never restores them itself" in text
    assert "How to recover (any backup tool)" in text
    # The copied list is named by the control that produced it, and that control is on the page.
    assert "Copy the affected paths with <strong>Copy paths</strong>" in _flat(help_html)
    toolbar = _toolbar(body)
    assert re.search(r'data-copy="rel">.*?Copy paths</button>', toolbar, re.S)
    assert re.search(r'data-copy="abs">.*?Copy full paths</button>', toolbar, re.S)
    assert "list above" not in text.lower() and "list below" not in text.lower()
    # The copyable list is the paths in the view's scope, relative; the root makes them absolute.
    assert 'data-relpaths="edited/0.txt\ngone/0.jpg"' in toolbar
    assert f'data-root="{cairn_env / "photos"}"' in toolbar


def test_recovery_step_three_names_where_scan_now_lives(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "<strong>Scan now</strong> on the collection page" in body
    # #32: a copy that fails has to say so, and has to work outside a secure context.
    assert "execCommand" in body and ".catch(" in body
    assert "Couldn't copy" in body


def test_the_recovery_copy_buttons_go_through_the_one_shared_clipboard_helper(cairn_env):
    """M4: the review page's working fallback is now the panel's only clipboard implementation,
    so the verify card cannot drift away from it."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "window.cairnCopy(paths(" in body
    # The implementation ships once, from base.html, with the fallback and the visible result.
    assert body.count("function legacyCopy") == 1
    assert "execCommand" in body and ".catch(" in body and "Couldn't copy" in body


# --- truncation: where the full set lives ----------------------------------------------------


def test_truncation_notice_deep_links_to_the_filtered_list(cairn_env):
    """Scenario "The truncation link lands filtered": the link to the full set opens the file
    browser already in its list view and already filtered to issues.

    A single list is the page, so it is cut only by the page's own row budget — and it states its
    truncation once, itself: there is no folder "above" to open.
    """
    from src.control_panel.routes import REVIEW_GROUP_ROW_LIMIT, REVIEW_ROW_LIMIT as LIMIT

    assert LIMIT > REVIEW_GROUP_ROW_LIMIT
    seed = _photos(cairn_env, [(f"only/{i:04d}.jpg", "missing", {"found": None}) for i in range(LIMIT + 1)])

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        assert (r["flat"], r["truncated"], r["shown"]) == (True, True, LIMIT)
        assert len(_row_forms(body)) == LIMIT

        assert body.count('id="review-truncated"') == 1
        notice = body[body.index('id="review-truncated"') :].split("</div>")[0]
        assert f"Showing {LIMIT} of {LIMIT + 1} files" in _text(notice)
        assert 'href="/collection/1?view=list&amp;filter=issues"' in notice
        assert "open a folder above" not in body
        assert body.count(f"Showing {LIMIT} of") == 1, "the truncation is stated once"
        # The page-wide action still covers the one file the list could not show, and says so.
        (bulk,) = _bulk_forms(_toolbar(body))
        assert bulk["button"] == f"Accept {LIMIT + 1} deletions"
        assert "includes 1 not shown here" in bulk["text"]

        with _capture_context() as captured:
            browser = client.get("/collection/1?view=list&filter=issues")
        ctx = [c for name, c in captured if name == "collection_detail.html"][0]

    assert browser.status_code == 200
    assert (ctx["view"], ctx["filter"]) == ("list", "issues")
    assert ctx["files_total"] == LIMIT + 1, "the filtered browser holds the full set"
    # ...and the filter control reflects it, rather than a checked "All" over a filtered list.
    assert re.search(r'name="filter" value="issues" checked', browser.text)


def test_a_truncated_copy_list_points_at_the_filtered_browser_not_an_unbuilt_command(cairn_env):
    """Scenario "A truncated copy list points at the filtered browser" (#11): `cairn status` is
    listed as *planned* — a notice naming it sends the operator to a command that does not exist."""
    from src.control_panel.routes import REVIEW_COPY_LIMIT as LIMIT

    seed = _photos(cairn_env, [(f"only/{i:05d}.jpg", "missing", {"found": None}) for i in range(LIMIT + 1)])

    with _make_client(cairn_env, seed) as client:
        with _capture_context() as captured:
            body = client.get("/collection/1/review").text
        ctx = [c for name, c in captured if name == "collection_review.html"][0]

    assert (ctx["copy_count"], ctx["copy_truncated"]) == (LIMIT, True)
    assert len(ctx["copy_relpaths"].split("\n")) == LIMIT
    help_html = _help(body)
    assert f"The copy buttons take the first {LIMIT:,} of {LIMIT + 1:,} paths" in _text(help_html)
    assert 'href="/collection/1?view=list&amp;filter=issues"' in help_html
    assert "cairn status" not in body


def test_an_untruncated_copy_list_carries_no_truncation_notice(cairn_env):
    seed = _photos(cairn_env, [("a.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)

    assert "The copy buttons take the first" not in body
    assert 'id="review-truncated"' not in body


def test_a_truncated_direct_files_group_does_not_promise_a_deeper_grouping(cairn_env):
    """A group of the files sitting directly in a folder has nothing deeper to group by, so its
    "more not shown" line must not promise a deeper grouping — while a folder group, which does
    have a level below it, still offers one."""
    from src.control_panel.routes import REVIEW_GROUP_ROW_LIMIT as LIMIT

    seed = _photos(cairn_env, (
        [(f"flat/{i:04d}.jpg", "missing", {"found": None}) for i in range(LIMIT + 2)]
        + [(f"flat/sub/{i:04d}.jpg", "missing", {"found": None}) for i in range(LIMIT + 1)]
        + [("flat/sub/deeper/one.jpg", "missing", {"found": None})]
    ))

    with _make_client(cairn_env, seed) as client:
        body, r = _review(client)
        direct = next(g for g in r["groups"] if not g["deep"])
        assert (direct["label"], direct["shown"], direct["more"]) == ("flat/ (this folder only)", LIMIT, 2)
        scoped_body, scoped = _review(client, f"/collection/1/review?{direct['query']}")

    section = next(s for s in _group_sections(body) if "this folder only" in s)
    tail = _text(section[section.rindex('class="review-list__more"') :])
    assert "2 more files in this folder are not shown." in tail
    assert "Use the file browser, filtered to issues, for every file." in tail
    assert "grouped one level deeper" not in _text(section)
    assert 'href="/collection/1?view=list&amp;filter=issues"' in section
    # Its heading still opens it as the page's scope — exactly its own files, as one list, which
    # the page budget (not the group limit) now bounds: all of them are there.
    assert 'href="/collection/1/review?dir=flat&amp;deep=0"' in section
    assert (scoped["flat"], scoped["total"], scoped["shown"]) == (True, LIMIT + 2, LIMIT + 2)
    assert "sub/" not in _text(scoped_body.split('id="review-toolbar"')[1].split('id="review-help"')[0])
    # A folder group past its limit has a level below it, and says so.
    sub = next(s for s in _group_sections(body) if 'name="dir" value="flat/sub"' in s)
    assert "Open flat/sub/ to see it grouped one level deeper" in _text(sub)


# --- accepting: one row, one group, and where it returns --------------------------------------


def test_accepting_from_a_row_acts_on_that_file_only(cairn_env):
    """Scenarios "A single row is resolved on its own" / "Accept one file from its own row": only
    that file is acted on, its own events are closed, every other row's record and alert is
    unchanged, and the view refreshes without the resolved row."""
    seed = _photos(cairn_env, [
        ("docs/edit-a.txt", "modified"),
        ("docs/edit-b.txt", "modified"),
        ("docs/gone-a.jpg", "missing"),
        ("docs/gone-b.jpg", "missing"),
        ("docs/fine.txt", "ok"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        forms = {int(ROW_ACTION.match(f["action"]).group(2)): f for f in _row_forms(body)}
        assert sorted(forms) == [1, 2, 3, 4]

        # Accept one change: the file is healthy, its alert closed; nothing else moved.
        resp = _submit(client, forms[1])
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review"
        assert _files() == {
            "docs/edit-a.txt": "ok", "docs/edit-b.txt": "modified",
            "docs/gone-a.jpg": "missing", "docs/gone-b.jpg": "missing", "docs/fine.txt": "ok",
        }
        assert _open_event_files() == [2, 3, 4]

        # The view reflects it; the remaining rows carry fresh forms.
        body, r = _review(client)
        assert sorted(it["id"] for it in r["rows"]) == [2, 3, 4]
        assert 'id="review-row-1"' not in body
        assert _summary(body).startswith("1 changed and 2 missing files")

        # Accept one deletion: that record is removed (its event kept, detached); the rest stays.
        forms = {int(ROW_ACTION.match(f["action"]).group(2)): f for f in _row_forms(body)}
        resp = _submit(client, forms[3])
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review"

    assert _files() == {
        "docs/edit-a.txt": "ok", "docs/edit-b.txt": "modified",
        "docs/gone-b.jpg": "missing", "docs/fine.txt": "ok",
    }
    assert _open_event_files() == [2, 4]


def test_a_row_form_does_not_validate_at_another_rows_address(cairn_env):
    """A row's fingerprint names its own record: replayed at a sibling's address it is refused."""
    seed = _photos(cairn_env, [("docs/gone-a.jpg", "missing"), ("docs/gone-b.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        first = _row_forms(_row_html(body, 1) + "</form>")[0]
        resp = client.post(
            "/collection/1/file/2/accept", data=first["fields"], follow_redirects=False
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/collection/1/review?stale=1"

    assert _files() == {"docs/gone-a.jpg": "missing", "docs/gone-b.jpg": "missing"}
    assert _open_event_files() == [1, 2]


def test_a_bulk_action_from_the_review_view_accepts_exactly_its_population(cairn_env):
    """Scenario "A bulk action from the review view": a group's action, then a page-wide one."""
    seed = _photos(cairn_env, [
        ("a/gone-1.jpg", "missing"),
        ("a/gone-2.jpg", "missing"),
        ("a/edit.txt", "modified"),
        ("b/gone-3.jpg", "missing"),
        ("b/edit.txt", "modified"),
        ("b/new.txt", "new"),
    ])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        group_a = next(s for s in _group_sections(body) if 'name="dir" value="a"' in s)
        deletions = next(f for f in _bulk_forms(group_a) if f["action"].endswith("stop-tracking"))
        assert deletions["button"] == "Accept 2 deletions"

        resp = _submit(client, deletions)
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review"
        # Exactly group a's missing files: its changed file, group b and the new file are untouched.
        assert _files() == {
            "a/edit.txt": "modified", "b/gone-3.jpg": "missing",
            "b/edit.txt": "modified", "b/new.txt": "new",
        }
        assert _open_event_files() == [3, 4, 5]

        # The view reflects it, and the page-wide "accept changes" now names both changed files.
        body, r = _review(client)
        assert (r["missing"], r["modified"]) == (1, 2)
        changes = next(f for f in _bulk_forms(_toolbar(body)) if f["action"].endswith("adopt-changed"))
        assert changes["button"] == "Accept 2 changes"
        resp = _submit(client, changes)
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review"

    # Changes accepted collection-wide; the missing file is not this verb's, the new file is not
    # an issue and is not promoted by it.
    assert _files() == {
        "a/edit.txt": "ok", "b/gone-3.jpg": "missing", "b/edit.txt": "ok", "b/new.txt": "new",
    }
    assert _open_event_files() == [4]


def test_a_scoped_accept_returns_to_the_scoped_view(cairn_env):
    """An operator working through one folder is returned to that folder — on success and on
    refusal alike — and the return address is built on a fixed path from percent-encoded values."""
    awkward = "100% real/a&b #1"
    seed = _photos(cairn_env, [
        ("a/x.txt", "modified"),
        ("a/y.jpg", "missing"),
        ("a/sub/z.jpg", "missing"),
        (f"{awkward}/w.jpg", "missing"),
        (f"{awkward}/v.jpg", "missing"),
    ])

    with _make_client(cairn_env, seed) as client:
        # A row on the folder's whole-subtree view returns there.
        body, _r = _review(client, "/collection/1/review?dir=a")
        row = _row_forms(_row_html(body, 1) + "</form>")[0]
        assert (row["fields"]["return_dir"], row["fields"]["return_deep"]) == ("a", "1")
        resp = _submit(client, row)
        assert resp.status_code == 303 and resp.headers["location"] == "/collection/1/review?dir=a"

        # A row on the files-directly-in-the-folder view returns to exactly that view...
        body, _r = _review(client, "/collection/1/review?dir=a&deep=0")
        row = _row_forms(_row_html(body, 2) + "</form>")[0]
        assert (row["fields"]["return_dir"], row["fields"]["return_deep"]) == ("a", "0")
        # ...on a refusal (a fingerprint that no longer reproduces) with the stale marker...
        refused = _submit(client, row, population_fp="0" * 64)
        assert refused.status_code == 303
        assert refused.headers["location"] == "/collection/1/review?dir=a&deep=0&stale=1"
        assert _files()["a/y.jpg"] == "missing"
        # ...and on success without it.
        resp = _submit(client, row)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/collection/1/review?dir=a&deep=0"

        # A folder whose name is hostile to URLs survives the round trip as a query VALUE.
        body, scoped = _review(client, "/collection/1/review?" + urlencode({"dir": awkward}))
        assert scoped["scope_dir"] == awkward and scoped["total"] == 2
        (bulk,) = _bulk_forms(_toolbar(body))
        resp = _submit(client, bulk)
        location = resp.headers["location"]
        assert location == "/collection/1/review?dir=100%25+real%2Fa%26b+%231"
        # Landing there renders that scope — now with nothing left in it.
        _body, after = _review(client, location)
        assert after["scope_dir"] == awkward and after["total"] == 0

    assert _files() == {"a/x.txt": "ok", "a/sub/z.jpg": "missing"}


@pytest.mark.parametrize(
    ("return_dir", "expected"),
    [
        ("//evil.example", "/collection/1/review"),                # not a scope: contributes nothing
        ("/abs/path", "/collection/1/review"),
        ("https://evil.example/x", "/collection/1/review?dir=https%3A%2F%2Fevil.example%2Fx"),
        ("a\r\nSet-Cookie: x=1", "/collection/1/review?dir=a%0D%0ASet-Cookie%3A+x%3D1"),
        ("a&stale=1", "/collection/1/review?dir=a%26stale%3D1"),   # cannot smuggle a parameter
    ],
)
def test_a_supplied_return_scope_never_becomes_the_redirects_host_or_path(
    cairn_env, return_dir, expected
):
    """`return_dir` is operator-controlled bytes. It may only ever become a percent-encoded query
    value on the collection's own review path — and it never changes what the accept does."""
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("a/y.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        row = _row_forms(_row_html(body, 1) + "</form>")[0]
        resp = _submit(client, row, return_dir=return_dir)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == expected
    parts = urlsplit(location)
    assert (parts.scheme, parts.netloc, parts.path) == ("", "", "/collection/1/review")
    assert _files() == {"a/y.jpg": "missing"}  # the same single-file accept, whatever was supplied


def test_review_actions_are_scoped_to_the_viewers_own_collections(cairn_env, monkeypatch):
    """"All review actions SHALL be scoped to the current user's own collections": another user's
    review page is not found (its existence is not disclosed) and its accept routes act on nothing."""
    from src.config import get_settings

    mine, theirs = cairn_env / "mine", cairn_env / "theirs"
    mine.mkdir()
    theirs.mkdir()

    async def seed():
        cid = await seed_collection(mine)
        await _seed_files(cid, [("my.txt", "missing")])

        async def other(s):
            from src.models.db import User
            from src.services.collections import create_collection

            u = User(username="bob", is_admin=False)
            s.add(u)
            await s.commit()
            c = await create_collection(
                s, user_id=u.id, name="Bobs Files", root=str(theirs), ots_mode="none"
            )
            return c.id

        bob_cid = await _with_session(other)
        await _seed_files(bob_cid, [("bobs/secret.txt", "missing"), ("bobs/edit.txt", "modified")])

    with _make_client(cairn_env, seed) as client:
        monkeypatch.setenv("CAIRN_AUTH_MODE", "multi")
        monkeypatch.setenv("CAIRN_SECRET_KEY", "0" * 64)
        get_settings.cache_clear()
        try:
            body, _r = _review(client)
            token = _bulk_forms(body)[0]["fields"]["csrf_token"]

            page = client.get("/collection/2/review")
            assert page.status_code == 404
            assert "secret.txt" not in page.text and "Bobs Files" not in page.text

            async def bobs_fps(s):
                from src.control_panel.routes import (
                    _ROOT_SCOPE, _narrow, _population_fingerprint, _read_population,
                )
                from src.models.db import Collection

                c = await s.get(Collection, 2)
                pop = await _read_population(s, c)
                return {
                    "stop-tracking": _population_fingerprint(
                        c, _narrow(pop, "stop-tracking", folder=_ROOT_SCOPE)),
                    "adopt-changed": _population_fingerprint(
                        c, _narrow(pop, "adopt-changed", folder=_ROOT_SCOPE)),
                    "row": _population_fingerprint(c, _narrow(pop, "accept-file", file_id=2)),
                }

            fps = _aside(bobs_fps)
            # Even with a fingerprint that WOULD validate, the route does not find the collection.
            for url, fp in (
                ("/collection/2/review/stop-tracking", fps["stop-tracking"]),
                ("/collection/2/review/adopt-changed", fps["adopt-changed"]),
                ("/collection/2/file/2/accept", fps["row"]),
                # ...and a row of Bob's addressed through the viewer's own collection is not found.
                ("/collection/1/file/2/accept", fps["row"]),
            ):
                resp = client.post(
                    url,
                    data={"csrf_token": token, "population_fp": fp, "dir": "", "deep": "1"},
                    follow_redirects=False,
                )
                assert resp.status_code == 404, url
        finally:
            get_settings.cache_clear()

    assert _files(2) == {"bobs/secret.txt": "missing", "bobs/edit.txt": "modified"}
    assert _files(1) == {"my.txt": "missing"}


# --- "it worked": the one-shot confirmation after an accept ----------------------------------

DONE_ONE_CHANGE = "Accepted 1 change."
DONE_ONE_DELETION = "Accepted 1 deletion — its record was removed; nothing on disk was touched."


def _done_banner(body: str) -> str | None:
    """The confirmation's visible text, or ``None`` when the page renders none."""
    m = re.search(r'<div[^>]*id="done-banner"[^>]*>(.*?)</div>\s*</div>', body, re.S)
    if m is None:
        assert "done-banner" not in body
        return None
    assert 'role="status"' in m.group(0)
    return _text(m.group(1))


def _row_form(body: str, file_id: int) -> dict:
    return _row_forms(_row_html(body, file_id) + "</form>")[0]


def test_an_accept_is_confirmed_once_on_the_page_it_returns_to(cairn_env):
    """After a successful accept the page it lands on says what was done — once.

    The only sign an accept worked used to be a smaller number somewhere on the page. Each verb
    states its own outcome, in the singular and the plural, and a reload does not say it again (it
    would read as a second accept).
    """
    seed = _photos(cairn_env, [
        ("e/1.txt", "modified"), ("e/2.txt", "modified"), ("e/3.txt", "modified"), ("e/4.txt", "modified"),
        ("g/5.jpg", "missing"), ("g/6.jpg", "missing"), ("g/7.jpg", "missing"),
        ("h/8.jpg", "missing"),
        ("i/9.txt", "modified"),
    ])

    def accept(client, form, expected):
        resp = _submit(client, form)
        assert resp.status_code == 303
        landed = client.get(resp.headers["location"]).text
        assert _done_banner(landed) == expected
        # Shown once: the same address again carries no confirmation.
        again = client.get(resp.headers["location"]).text
        assert _done_banner(again) is None
        return again

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        assert _done_banner(body) is None, "nothing was accepted yet"

        # One file, from its own row — a change, then a deletion.
        body = accept(client, _row_form(body, 1), DONE_ONE_CHANGE)
        body = accept(client, _row_form(body, 5), DONE_ONE_DELETION)
        # A group's bulk action over ONE file reads in the singular too.
        h = next(s for s in _group_sections(body) if 'name="dir" value="h"' in s)
        body = accept(client, _bulk_forms(h)[0], DONE_ONE_DELETION)
        i = next(s for s in _group_sections(body) if 'name="dir" value="i"' in s)
        body = accept(client, _bulk_forms(i)[0], DONE_ONE_CHANGE)
        # ...and over several, in the plural, with the count the service reported.
        e = next(s for s in _group_sections(body) if 'name="dir" value="e"' in s)
        body = accept(client, _bulk_forms(e)[0], "Accepted 3 changes.")
        # The last accept empties the page: the confirmation is what explains the all-clear.
        (last,) = _bulk_forms(_toolbar(body))
        assert last["button"] == "Accept 2 deletions"
        resp = _submit(client, last)
        landed = client.get(resp.headers["location"]).text
        assert _done_banner(landed) == (
            "Accepted 2 deletions — their records were removed; nothing on disk was touched."
        )
        assert "All clear" in landed
        assert landed.index('id="done-banner"') < landed.index("All clear")
        assert _done_banner(client.get(resp.headers["location"]).text) is None

    assert set(_files().values()) == {"ok"} and len(_files()) == 5


def test_the_confirmation_follows_a_scoped_accept_to_the_scoped_view(cairn_env):
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("a/y.jpg", "missing"), ("b/z.jpg", "missing")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client, "/collection/1/review?dir=a")
        resp = _submit(client, _row_form(body, 1))
        assert resp.headers["location"] == "/collection/1/review?dir=a"
        landed = client.get(resp.headers["location"]).text
        assert _done_banner(landed) == DONE_ONE_DELETION
        # It sits above the list it explains, not below the instructions.
        assert landed.index('id="done-banner"') < landed.index('class="review-row"')
        assert _done_banner(client.get("/collection/1/review").text) is None


def test_a_refused_accept_is_not_confirmed(cairn_env):
    """A refusal sets nothing: the page it lands on says the action was NOT applied, and nothing on
    it — then or on a later visit — says anything was accepted."""
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("a/y.txt", "modified")])

    with _make_client(cairn_env, seed) as client:
        body, _r = _review(client)
        attempts = [
            (_row_form(body, 1), {"population_fp": "0" * 64}),   # a fingerprint that does not reproduce
            (_row_form(body, 2), {"population_fp": ""}),          # no fingerprint at all
            (_bulk_forms(_toolbar(body))[0], {"population_fp": "f" * 64}),
            (_bulk_forms(_toolbar(body))[1], {"dir": "/not-a-scope"}),
        ]
        for form, tamper in attempts:
            resp = _submit(client, form, **tamper)
            assert resp.status_code == 303 and resp.headers["location"].endswith("stale=1")
            assert "cairn_done" not in resp.headers.get("set-cookie", "")
            landed = client.get(resp.headers["location"]).text
            assert STALE_LEAD in landed
            assert _done_banner(landed) is None
            assert "Accepted" not in _text(landed.split('id="review-toolbar"')[0])
        assert _done_banner(client.get("/collection/1/review").text) is None

    assert _files() == {"a/x.jpg": "missing", "a/y.txt": "modified"}


FORGED_DONE = [
    "<script>alert(1)</script>",
    "deletions:99999999",          # eight digits: over the bound
    "foo:3",                       # not a verb this panel has
    "changes:",
    "changes:-1",
    "changes:1e3",
    "CHANGES:3",
    "deletions:3:extra",
    "changes:3<b>",
    "%3Cscript%3E",
]


@pytest.mark.parametrize("forged", FORGED_DONE)
def test_a_forged_confirmation_cookie_renders_nothing(cairn_env, forged):
    """The cookie is client-held, so it is matched against a strict pattern: anything else renders
    no confirmation and is reflected nowhere in the page."""
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("a/y.txt", "modified")])

    with _make_client(cairn_env, seed) as client:
        clean = client.get("/collection/1/review").text
        for url in ("/collection/1/review", "/collection/1/review?dir=a", "/collection/1/review?stale=1"):
            body = client.get(url, headers={"Cookie": f"cairn_done={forged}"}).text
            assert _done_banner(body) is None, url
            assert "Accepted" not in _text(body.split('id="review-toolbar"')[0]), url
            for fragment in (forged, html_lib.escape(forged), "alert(1)", "99999999", "foo:3"):
                assert fragment not in body, (url, fragment)
        # ...and apart from the per-request CSRF token it changes nothing about the page.
        forged_body = client.get(
            "/collection/1/review", headers={"Cookie": f"cairn_done={forged}"}
        ).text

    def stable(page: str) -> str:
        page = re.sub(r'name="csrf_token" value="[^"]*"', "", page)
        return re.sub(r'name="csrf-token" content="[^"]*"', "", page)

    assert stable(forged_body) == stable(clean)


def test_a_well_formed_confirmation_cookie_only_ever_produces_the_sentence(cairn_env):
    """Even a cookie that matches the pattern selects nothing and authorises nothing: it yields the
    one sentence, with a number, and the collection is exactly as it was."""
    seed = _photos(cairn_env, [("a/x.jpg", "missing"), ("a/y.txt", "modified")])

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review", headers={"Cookie": "cairn_done=deletions:1234567"}).text
        assert _done_banner(body) == (
            "Accepted 1,234,567 deletions — their records were removed; nothing on disk was touched."
        )
        body, r = _review(client)
        assert _done_banner(body) is None
        assert (r["missing"], r["modified"]) == (1, 1)

    assert _files() == {"a/x.jpg": "missing", "a/y.txt": "modified"}
    assert _open_event_files() == [1, 2]

