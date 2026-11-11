"""unify-issue-model — the panel's one open item (the issue) and the words it is described in.

What these tests pin, requirement by requirement (`openspec/changes/unify-issue-model/`):

* **The panel has one kind of open item, the issue** — the four retired addresses (three
  acknowledge routes and the baseline route) answer 404/405 and mutate nothing; no surface offers
  to acknowledge or counts unreviewed events; accepting a file is what lowers the count; a file
  that is merely new is not presented as work and is not called notarized for being new.
* **Policies and collection status are named in plain words** — Locked / Working (never the stored
  `worm` / `churn`), an accurate statement of what Working flags, and a status pill that states
  the fact ("2 changed", "3 missing · 2 changed") instead of a grade ("Attention" / "Alert").
* **Add/edit corpus validates the root path** (the form half) — no auto-baseline control; a
  collection created from the form graduates new files automatically; editing leaves the stored
  setting alone.
* **corpus-management** — `cairn add-collection --mode locked|working`.

Cairn's panel must never say something reassuring that is not true, so the absence assertions
here are as load-bearing as the presence ones: a retired control that still rendered, or a retired
route that still acknowledged, would let an operator clear a counter without resolving anything.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

# --- helpers ---------------------------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    """Seed on a throwaway loop, drop the engine, return a TestClient (mirrors tests/test_panel)."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warning)
    return TestClient(app)


def _run_check(coro_factory):
    """Run a post-test DB read on a fresh loop, disposing the engine on that same loop."""
    from src import database

    database.reset_engine()

    async def _wrapped():
        try:
            return await coro_factory()
        finally:
            await database.get_engine().dispose()

    return asyncio.run(_wrapped())


def _csrf_token(client) -> str:
    page = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', page)
    assert m, "no csrf token in page"
    return m.group(1)


async def _collection(
    root: Path,
    *,
    mode: str = "worm",
    ots_mode: str = "perfile",
    auto_baseline_new: bool = False,
) -> int:
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    root.mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id))
        collection = await create_collection(
            s, user_id=uid, name=root.name, root=str(root), mode=mode, ots_mode=ots_mode,
            auto_baseline_new=auto_baseline_new,
        )
        return collection.id


# What the scanner writes for each status: an alarming kind is born unacknowledged, an `added`
# event is born acknowledged (it is informational), an `ok` file has no event.
_EVENT_FOR = {"modified": "modified", "missing": "missing", "new": "added"}


async def _files(collection_id: int, *, ots_state: str = "none", **counts: int) -> None:
    """Insert tracked files by status (`ok=`, `new=`, `modified=`, `missing=`), each with the
    event a real scan would have left behind."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        for status, n in counts.items():
            for i in range(n):
                fe = FileEntry(
                    collection_id=collection_id, relpath=f"{status}-dir/file_{i:03d}.txt",
                    size=10, sha256="a" * 64, status=status, ots_state=ots_state,
                    first_seen=now, last_checked=now,
                )
                s.add(fe)
                await s.flush()
                kind = _EVENT_FOR.get(status)
                if kind:
                    s.add(Event(
                        collection_id=collection_id, file_id=fe.id, kind=kind, detected_at=now,
                        acknowledged_at=now if kind == "added" else None,
                    ))
        await s.commit()


_OPERATOR_ATTRS = ("title", "aria-label", "placeholder", "onsubmit", "data-where")


def _operator_text(page: str, cairn_env) -> str:
    """Everything a page SAYS to an operator, lower-cased: its visible text plus the attribute
    values a person reads or hears (tooltips, accessible labels, placeholders, confirm strings).

    Markup that is not copy is dropped — tags, scripts, styles, icons, ids, classes, form actions
    (`review/adopt-changed` is an address, not a word on the screen) — and so is the temp
    directory, whose name pytest derives from the test's own.
    """
    page = page.replace(str(cairn_env), "")
    page = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", page, flags=re.S)
    attrs = re.findall(r'\b(?:%s)="([^"]*)"' % "|".join(_OPERATOR_ATTRS), page)
    text = re.sub(r"<[^>]+>", " ", page) + " \x1f " + " \x1f ".join(attrs)
    return re.sub(r"\s+", " ", html_mod.unescape(text)).lower()


def _status_pill(page: str, collection_id: int) -> tuple[str, str]:
    """(pill kind, pill text) of a collection's resting status indicator in ``page``."""
    m = re.search(
        r'<span id="op-status-%d" class="op-status"\s*>\s*'
        r'<span class="pill pill--soft pill--(\w+)">(.*?)</span>\s*</span>' % collection_id,
        page, flags=re.S,
    )
    assert m, f"no resting status pill for collection {collection_id}"
    return m.group(1), re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()


def _badge(page: str) -> int:
    """The sidebar badge's number (0 when the badge is not rendered)."""
    m = re.search(r'<span id="sidebar-alert-badge">(.*?)</a>', page, flags=re.S)
    assert m, "the sidebar badge container is missing"
    n = re.search(r'<span class="nav-badge"[^>]*>\s*([\d,]+)\s*</span>', m.group(1))
    return int(n.group(1).replace(",", "")) if n else 0


# === the retired addresses ===================================================================

RETIRED_ADDRESSES = [
    "/events/1/ack",                  # one event
    "/events/1/ack?view=review",      # … from a review row
    "/events/ack-all",                # all of a user's events
    "/collection/1/review/ack-all",   # all of a collection's events
    "/collection/1/accept",           # "Baseline N new files"
]


@pytest.mark.parametrize("address", RETIRED_ADDRESSES)
def test_a_retired_address_answers_not_found_and_mutates_nothing(cairn_env, address):
    """A stale tab's acknowledge or baseline POST must fail closed (design D3).

    Sent the way the old pages sent it — with a VALID CSRF token, so the refusal is the address
    being gone and not the request being unauthenticated. Nothing forwards, nothing is
    acknowledged, and no file record changes.
    """
    async def seed():
        cid = await _collection(cairn_env / "archive")
        await _files(cid, ok=1, new=1, modified=1, missing=1)

    async def state():
        from src.database import get_sessionmaker
        from src.models.db import Event, FileEntry

        async with get_sessionmaker()() as s:
            files = (await s.execute(
                select(FileEntry.id, FileEntry.relpath, FileEntry.status, FileEntry.sha256)
                .order_by(FileEntry.id)
            )).all()
            events = (await s.execute(
                select(Event.id, Event.kind, Event.file_id, Event.acknowledged_at,
                       Event.acknowledged_by)
                .order_by(Event.id)
            )).all()
            return [tuple(r) for r in files], [tuple(r) for r in events]

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        r = client.post(
            address,
            data={"csrf_token": token, "population_fp": "0" * 64},
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert r.status_code in (404, 405), (address, r.status_code, r.text[:200])
        assert "location" not in r.headers, "a retired address must not forward anywhere"
        # A browser-shaped request gets the same answer, not a redirect to a live route.
        r = client.post(
            address,
            data={"csrf_token": token},
            headers={"X-CSRF-Token": token, "Accept": "text/html"},
            follow_redirects=False,
        )
        assert r.status_code in (404, 405), (address, r.status_code)
        assert "location" not in r.headers

    files, events = _run_check(state)
    assert [(f[1], f[2]) for f in files] == [
        ("ok-dir/file_000.txt", "ok"),
        ("new-dir/file_000.txt", "new"),            # the baseline address promoted nothing
        ("modified-dir/file_000.txt", "modified"),
        ("missing-dir/file_000.txt", "missing"),    # … and removed nothing
    ]
    open_kinds = sorted(e[1] for e in events if e[3] is None)
    assert open_kinds == ["missing", "modified"], "an alert was acknowledged by a retired address"
    assert all(e[4] is None for e in events), "no event may carry an operator acknowledgement"
    assert len(events) == 3


def test_no_surface_offers_to_acknowledge_or_counts_unreviewed_events(cairn_env):
    """No control whose only effect is to mark an event seen, and no count of unseen events.

    Rendered for a user WITH open issues and open events on two collections, so every surface that
    used to carry the verb — the dashboard feed and its "Mark all N reviewed" pill, the collection
    page, the collection's review rows and the fleet page — has something it could offer it on.
    """
    async def seed():
        a = await _collection(cairn_env / "archive")
        await _files(a, ok=1, modified=2, missing=1)
        b = await _collection(cairn_env / "drafts", mode="churn", ots_mode="none")
        await _files(b, ok=1, missing=2)

    with _make_client(cairn_env, seed) as client:
        pages = {
            path: client.get(path)
            for path in (
                "/", "/collections", "/review", "/collection/1", "/collection/1/review",
                "/collection/2", "/collection/2/review", "/collection/1/files?filter=issues",
            )
        }

    for path, r in pages.items():
        assert r.status_code == 200, path
        said = _operator_text(r.text, cairn_env)
        for retired in ("mark reviewed", "reviewed", "acknowledg", "unreviewed", "need action",
                        "needs action"):
            assert retired not in said, (path, retired)
        # No form or htmx trigger points at an acknowledge address, and the containers the
        # out-of-band "unreviewed" refreshes targeted are gone with them.
        for marker in ("/events/", "ack-all", "open-events-pill", "review-open-pill",
                       "hx-post=\"/events", "view=review"):
            assert marker not in r.text, (path, marker)

    # The pages do still carry the issues themselves — the test is not passing on an empty render.
    assert "Accept change" in pages["/collection/1/review"].text
    assert "Accept deletion" in pages["/review"].text


def test_no_page_speaks_the_retired_vocabulary(cairn_env):
    """The words this change retired appear on no page: the grades ("Attention"), the old verbs
    ("Adopt", "Stop tracking"), the stored policy values, and "baseline" as review vocabulary."""
    async def seed():
        a = await _collection(cairn_env / "archive")
        await _files(a, ok=2, new=1, modified=2, missing=1)
        b = await _collection(cairn_env / "drafts", mode="churn", ots_mode="none")
        await _files(b, ok=1, new=2, modified=1)
        await _collection(cairn_env / "unscanned")

    with _make_client(cairn_env, seed) as client:
        pages = {
            path: client.get(path)
            for path in (
                "/", "/collections", "/review", "/learn", "/collection/new",
                "/collection/1", "/collection/1/review", "/collection/1/edit",
                "/collection/1/op-status", "/collection/1/files?filter=new", "/collection/1/tree",
                "/collection/2", "/collection/2/review", "/collection/2/edit",
                "/collection/2/op-status", "/collection/3", "/collection/3/review",
                "/collection/1/review?stale=1", "/review?stale=1",
            )
        }

    retired = re.compile(
        r"baselin|\battention\b|\badopt|stop tracking|\bworm\b|\bchurn\b|\bunreviewed\b"
    )
    # Positive control: the sweep reads what an operator reads — button labels, tooltips and
    # confirm strings — so a retired word in any of those would be caught.
    review_said = _operator_text(pages["/collection/1/review"].text, cairn_env)
    assert "accept change" in review_said and "accept 1 deletion" in review_said
    assert "this is the file now" in review_said                  # a tooltip
    assert "will not be reported again" in review_said            # a confirm string
    assert "locked" in _operator_text(pages["/"].text, cairn_env)  # the needs-you policy pill
    for path, r in pages.items():
        assert r.status_code == 200, path
        said = _operator_text(r.text, cairn_env)
        hit = retired.search(said)
        assert hit is None, (path, said[max(0, hit.start() - 60): hit.end() + 60])


# === Locked / Working ========================================================================


def _policy_cell(page: str) -> tuple[str, str, str]:
    """(label, note, tooltip) of the collection page's Policy cell."""
    m = re.search(
        r'<div class="meta-cell__label">Policy</div>'
        r'<div class="meta-cell__value" title="([^"]*)">([^<]*)</div>'
        r'<div class="meta-cell__note">([^<]*)</div>',
        page,
    )
    assert m, "the collection page has no Policy cell"
    return m.group(2).strip(), m.group(3).strip(), html_mod.unescape(m.group(1))


def test_the_collection_page_names_the_policy_in_plain_words(cairn_env):
    async def seed():
        await _collection(cairn_env / "archive", mode="worm")
        await _collection(cairn_env / "drafts", mode="churn")

    with _make_client(cairn_env, seed) as client:
        locked = client.get("/collection/1").text
        working = client.get("/collection/2").text

    label, note, tip = _policy_cell(locked)
    assert label == "Locked"
    assert "change" in (note + " " + tip).lower()
    # Under Locked every change AND every deletion is flagged.
    assert "every change" in tip.lower() and "deletion" in tip.lower()

    label, note, tip = _policy_cell(working)
    assert label == "Working"
    said = (note + " " + tip).lower()
    assert "edits are normal" in said
    # Accurate, not "deletions only": deletions are flagged, and so is a file that comes back
    # different after going missing.
    assert "deletions are" in said
    assert "comes back different" in said and "missing" in said
    assert "only" not in said

    # The stored value is never the label — nor anywhere else an operator reads.
    for page in (locked, working):
        said = _operator_text(page, cairn_env)
        assert not re.search(r"\bworm\b|\bchurn\b", said)


@pytest.mark.parametrize("path, selected", [("/collection/new", "worm"), ("/collection/1/edit", "churn")])
def test_the_form_offers_locked_and_working_with_what_each_flags(cairn_env, path, selected):
    async def seed():
        await _collection(cairn_env / "drafts", mode="churn")

    with _make_client(cairn_env, seed) as client:
        r = client.get(path)
    assert r.status_code == 200
    page = r.text

    # Two radios: the stored values are what is submitted, the plain names are what is shown.
    radios = re.findall(
        r'<input type="radio" name="mode" value="([^"]+)"([^>]*)/>\s*([^<]+?)\s*</label>', page
    )
    assert [(value, label) for value, _, label in radios] == [
        ("worm", "Locked"), ("churn", "Working"),
    ]
    assert [value for value, attrs, _ in radios if "checked" in attrs] == [selected]

    # Each choice comes with a visible description of what it flags.
    described = dict(re.findall(r"<div><strong>(Locked|Working)</strong> — ([^<]+)</div>", page))
    assert set(described) == {"Locked", "Working"}
    locked = html_mod.unescape(described["Locked"]).lower()
    working = html_mod.unescape(described["Working"]).lower()
    assert "every change" in locked and "deletion" in locked
    assert "edits" in working and "not flagged" in working
    assert "deletions are" in working
    # Working is NOT "deletions only": a file that comes back different is flagged too.
    assert "comes back different" in working and "after going missing" in working
    assert "only" not in working

    assert not re.search(r"\bworm\b|\bchurn\b", _operator_text(page, cairn_env))


# === the form: no auto-baseline control ======================================================


@pytest.mark.parametrize("path", ["/collection/new", "/collection/1/edit"])
def test_the_form_offers_no_auto_baseline_control(cairn_env, path):
    async def seed():
        await _collection(cairn_env / "archive", auto_baseline_new=True)

    with _make_client(cairn_env, seed) as client:
        r = client.get(path)
    assert r.status_code == 200
    names = set(re.findall(r'\bname="([^"]+)"', r.text))
    assert not [n for n in names if "baseline" in n.lower()], names
    assert "baselin" not in _operator_text(r.text, cairn_env)
    # The controls the form does offer are still there — this is the real form.
    assert {"name", "root", "mode", "ots", "cadence", "verify_cadence", "excludes"} <= names


def _form_fields(root: Path, **overrides: str) -> dict[str, str]:
    data = {
        "name": root.name, "root": str(root), "mode": "worm", "ots": "perfile",
        "cadence": "900", "verify_cadence": "604800", "excludes": "", "email_to": "",
    }
    data.update(overrides)
    return data


async def _collections_by_name() -> dict[str, tuple[str, bool]]:
    from src.database import get_sessionmaker
    from src.models.db import Collection

    async with get_sessionmaker()() as s:
        rows = (await s.execute(
            select(Collection.name, Collection.mode, Collection.auto_baseline_new)
        )).all()
        return {name: (mode, bool(flag)) for name, mode, flag in rows}


@pytest.mark.parametrize("stale_field", [None, "off"])
def test_a_collection_created_from_the_form_graduates_new_files_automatically(
    cairn_env, stale_field
):
    """The form has no control for it, so the create route decides: on. A stale tab that still
    posts the old select's `auto_baseline=off` gets the same answer — the field is not read."""
    newroot = cairn_env / "store" / "fresh"
    newroot.mkdir(parents=True)

    async def seed():
        await _collection(cairn_env / "archive")

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        data = _form_fields(newroot, name="Fresh", mode="churn", csrf_token=token)
        if stale_field is not None:
            data["auto_baseline"] = stale_field
        r = client.post("/collection", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text

    rows = _run_check(_collections_by_name)
    assert rows["Fresh"] == ("churn", True)
    # The collection that already existed (created with the flag off) was not touched.
    assert rows["archive"] == ("worm", False)


@pytest.mark.parametrize("stored", [False, True])
@pytest.mark.parametrize("stale_field", [None, "opposite"])
def test_editing_a_collection_leaves_its_stored_auto_baseline_setting_alone(
    cairn_env, stored, stale_field
):
    """Saving the edit form must not flip a setting the form no longer shows — in either
    direction, and whether or not a stale tab still submits the old select."""
    root = cairn_env / "archive"

    async def seed():
        await _collection(root, auto_baseline_new=stored)

    with _make_client(cairn_env, seed) as client:
        token = _csrf_token(client)
        data = _form_fields(root, name="renamed", mode="churn", csrf_token=token)
        if stale_field is not None:
            data["auto_baseline"] = "off" if stored else "on"
        r = client.post("/collection/1", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text

    # The save itself went through (name and policy changed) and the flag is exactly as stored.
    assert _run_check(_collections_by_name) == {"renamed": ("churn", stored)}


# === the status pill states the fact =========================================================


def test_the_status_pill_states_what_is_wrong_on_every_surface(cairn_env):
    expected = {
        1: ("warn", "2 changed"),
        2: ("danger", "3 missing · 2 changed"),
        3: ("danger", "1 missing"),
        4: ("ok", "All clear"),           # its only non-ok files are `new`
        5: ("muted", "No files indexed"),  # nothing indexed is not all clear
    }

    async def seed():
        c = await _collection(cairn_env / "two-changed")
        await _files(c, ok=1, modified=2)
        c = await _collection(cairn_env / "both-kinds")
        await _files(c, ok=1, modified=2, missing=3)
        c = await _collection(cairn_env / "one-gone", mode="churn")
        await _files(c, ok=1, missing=1)
        c = await _collection(cairn_env / "only-new")
        await _files(c, new=4)
        await _collection(cairn_env / "unscanned")

    with _make_client(cairn_env, seed) as client:
        dashboard = client.get("/").text
        listing = client.get("/collections").text
        for cid, want in expected.items():
            detail = client.get(f"/collection/{cid}").text
            fragment = client.get(f"/collection/{cid}/op-status")
            assert fragment.status_code == 200
            # The detail page, the polled fragment that replaces its pill, and both card
            # surfaces say the same thing in the same colour.
            assert _status_pill(detail, cid) == want, cid
            assert _status_pill(fragment.text, cid) == want, cid
            assert _status_pill(dashboard, cid) == want, cid
            assert _status_pill(listing, cid) == want, cid
            # A grade leaves the operator to guess which kind of problem it is.
            for page in (detail, fragment.text):
                assert "Attention" not in page, cid
                assert ">Alert<" not in page and "Alert</span>" not in page, cid
        for page in (dashboard, listing):
            assert "Attention" not in page
            assert "Alert</span>" not in page


# === resolving is what lowers the count ======================================================


def _row_forms(page: str) -> dict[int, tuple[str, str]]:
    """file id → (button label, population_fp) for every per-row accept form on a review page."""
    out: dict[int, tuple[str, str]] = {}
    for m in re.finditer(
        r'<form method="post" action="/collection/\d+/file/(\d+)/accept[^"]*".*?</form>',
        page, flags=re.S,
    ):
        form = m.group(0)
        fp = re.search(r'name="population_fp" value="([0-9a-f]+)"', form)
        label = re.search(r"<button[^>]*>\s*(.*?)\s*</button>", form, flags=re.S)
        assert fp and label, form[:200]
        out[int(m.group(1))] = (label.group(1), fp.group(1))
    return out


def test_accepting_one_changed_file_lowers_the_sidebar_badge_by_one(cairn_env):
    """An issue leaves the count only by being resolved; looking at it changes nothing."""
    async def seed():
        cid = await _collection(cairn_env / "archive")
        await _files(cid, ok=1, modified=3, missing=1)

    async def state():
        from src.database import get_sessionmaker
        from src.models.db import Event, FileEntry

        async with get_sessionmaker()() as s:
            files = dict((await s.execute(select(FileEntry.id, FileEntry.status))).all())
            open_events = sorted((await s.execute(
                select(Event.file_id).where(Event.acknowledged_at.is_(None))
            )).scalars())
            return files, open_events

    with _make_client(cairn_env, seed) as client:
        # Viewing: the review page twice, the dashboard and the collection page — the count holds.
        review = client.get("/collection/1/review").text
        assert _badge(review) == 4
        assert _badge(client.get("/collection/1/review").text) == 4
        dashboard = client.get("/").text
        assert _badge(dashboard) == 4
        # The dashboard's needs-you total is the same number, from the same definition, and the
        # badge says in words what it counts.
        assert re.search(r'<strong id="needs-total">4</strong>', dashboard)
        assert 'aria-label="4 files missing or changed' in dashboard
        assert _status_pill(client.get("/collection/1").text, 1) == (
            "danger", "1 missing · 3 changed"
        )

        forms = _row_forms(review)
        assert sorted(label for label, _ in forms.values()) == [
            "Accept change", "Accept change", "Accept change", "Accept deletion",
        ]
        target = min(fid for fid, (label, _) in forms.items() if label == "Accept change")
        token = _csrf_token(client)
        r = client.post(
            f"/collection/1/file/{target}/accept",
            # The fingerprint the page published for THIS row, exactly as its hidden field holds it.
            data={"csrf_token": token, "population_fp": forms[target][1],
                  "return_dir": "", "return_deep": "1"},
            follow_redirects=False,
        )
        assert r.status_code == 303, r.text
        assert r.headers["location"] == "/collection/1/review", "the accept was refused as stale"

        after = client.get(r.headers["location"]).text
        assert _badge(after) == 3
        assert target not in _row_forms(after)
        assert len(_row_forms(after)) == 3
        after_dashboard = client.get("/").text
        assert _badge(after_dashboard) == 3
        assert re.search(r'<strong id="needs-total">3</strong>', after_dashboard)
        # The collection's status reflects the remaining issues only.
        assert _status_pill(client.get("/collection/1").text, 1) == (
            "danger", "1 missing · 2 changed"
        )

    files, open_events = _run_check(state)
    assert files[target] == "ok"
    assert sorted(files.values()) == ["missing", "modified", "modified", "ok", "ok"]
    # Resolving the issue closed the event that belonged to it, and only that one.
    assert target not in open_events
    assert len(open_events) == 3


# === new files are not work ==================================================================


def _card(page: str, collection_id: int) -> str:
    """One collection card's markup, cut out of a dashboard / collections-list page."""
    start = page.index(f'collection-card" href="/collection/{collection_id}"')
    end = page.find('class="card card--flush card--hover collection-card"', start)
    return page[start: end if end != -1 else len(page)]


def test_new_files_are_not_presented_as_work(cairn_env):
    """A file that is merely new is watched from the scan that found it. No page says it is
    waiting for a baseline, no page offers to baseline it, and no page calls it notarized for
    being new — collection 2 is a tripwire (nothing in it is ever notarized) and collection 1's
    new files are still only queued to stamp."""
    async def seed():
        c = await _collection(cairn_env / "perfile-new", ots_mode="perfile")
        await _files(c, new=4, ots_state="pending")
        c = await _collection(cairn_env / "tripwire-new", ots_mode="none", mode="churn")
        await _files(c, new=3)
        c = await _collection(cairn_env / "mixed", ots_mode="perfile")
        await _files(c, ok=2, new=2, modified=1, ots_state="pending")

    with _make_client(cairn_env, seed) as client:
        pages = {
            path: client.get(path)
            for path in ["/", "/collections", "/review"] + [
                f"/collection/{cid}{suffix}"
                for cid in (1, 2, 3)
                for suffix in ("", "/review", "/files?filter=new", "/tree", "/op-status")
            ]
        }

    claims_new_is_notarized = re.compile(
        r"(watched|recorded|stamped|tracked|monitored) and notari[sz]ed"
        r"|\bnew\b[^.\x1f]{0,60}\b(is|are|was|were|already|been|get|gets)\b[^.\x1f]{0,30}"
        r"(notari[sz]ed|anchored|timestamped)"
    )
    # The copy this replaced, which the pattern exists to keep out.
    assert claims_new_is_notarized.search("they're already watched and notarized — this only")
    assert claims_new_is_notarized.search("new files are watched and are notarized")
    for path, r in pages.items():
        assert r.status_code == 200, path
        said = _operator_text(r.text, cairn_env)
        assert "not yet baselined" not in said, path
        assert "baselin" not in said, path
        # The baseline action's address is rendered nowhere.
        assert not re.search(r'/collection/\d+/accept"', r.text), path
        hit = claims_new_is_notarized.search(said)
        assert hit is None, (path, said[max(0, hit.start() - 60): hit.end() + 40])

    # The new-only collections read all clear on every surface that states a status …
    for cid in (1, 2):
        for path in ("/", "/collections", f"/collection/{cid}", f"/collection/{cid}/op-status"):
            assert _status_pill(pages[path].text, cid) == ("ok", "All clear"), (cid, path)
        # … offer nothing to do about those files (no accept form of any kind, no review CTA) …
        for suffix in ("", "/review", "/files?filter=new", "/tree"):
            page = pages[f"/collection/{cid}{suffix}"].text
            assert "/accept" not in page, (cid, suffix)
            assert "/review/" not in page, (cid, suffix)
        assert f"/collection/{cid}/review" not in pages[f"/collection/{cid}"].text
        assert "All clear" in pages[f"/collection/{cid}/review"].text
        # … and still describe them: the card keeps its "N new" legend, the browser lists them.
        n = 4 if cid == 1 else 3
        assert f"{n} new" in re.sub(r"<[^>]+>", "", _card(pages["/"].text, cid))
        assert "new-dir/file_000.txt" in pages[f"/collection/{cid}/files?filter=new"].text
    assert _badge(pages["/collection/1"].text) == 1  # only the mixed collection's changed file

    # In the collection WITH an issue, the review page lists the changed file and not the new ones.
    mixed_review = pages["/collection/3/review"].text
    assert "modified-dir" in mixed_review and "new-dir" not in mixed_review
    assert _status_pill(pages["/collection/3"].text, 3) == ("warn", "1 changed")

    # A tripwire collection's pages never use the notary's word at all; its card says so plainly.
    for suffix in ("", "/review", "/files?filter=new", "/tree"):
        said = _operator_text(pages[f"/collection/2{suffix}"].text, cairn_env)
        assert "notarized" not in said and "anchored" not in said, suffix
    tripwire_card = _operator_text(_card(pages["/"].text, 2), cairn_env)
    assert "no notarization" in tripwire_card
    assert "notarized" not in tripwire_card and "anchored" not in tripwire_card

    # Where new files ARE in a notarized collection, their proof state is the file's own: these
    # are queued, so the rows say "Queued to stamp" and the collection claims nothing confirmed.
    rows = pages["/collection/1/files?filter=new"].text
    assert rows.count("Queued to stamp") == 4
    assert "Anchored" not in re.sub(r"<[^>]+>", " ", rows.split('id="file-table"', 1)[-1])
    card = _operator_text(_card(pages["/"].text, 1), cairn_env)
    assert "0 / 4 present files anchored" in card and "4 queued" in card


# === the learn page ==========================================================================


def test_the_learn_page_teaches_the_two_outcome_named_actions(cairn_env):
    async def seed():
        await _collection(cairn_env / "archive")

    with _make_client(cairn_env, seed) as client:
        r = client.get("/learn")
    assert r.status_code == 200
    assert "<strong>Accept change</strong>" in r.text
    assert "<strong>Accept deletion</strong>" in r.text

    said = _operator_text(r.text, cairn_env)
    for retired in ("mark reviewed", "acknowledg", "unreviewed", "adopt", "stop tracking"):
        assert retired not in said, retired
    assert not re.search(r"\bworm\b|\bchurn\b", said)
    assert "working" in said  # the policy is named the way the form names it

    # Accepting is never described as creating (or rewriting, or becoming) a baseline: every
    # sentence that mentions accepting is free of the word, and so is the page's phrasing of it.
    sentences = [s for s in re.split(r"(?<=[.?!])\s+", said) if "accept" in s]
    assert sentences, "the learn page no longer explains accepting at all"
    for sentence in sentences:
        assert "baselin" not in sentence, sentence
    for phrase in ("new baseline", "as the baseline", "re-baseline", "rebaseline"):
        assert phrase not in said, phrase

    # The two honest-claims rules hold here too: accepting a deletion deletes neither a file nor
    # a proof, and nothing promises that a rescan undoes an accepted change.
    assert "nothing on disk and no proof is deleted" in said
    assert "rescan" not in said and "undo" not in said
    # The self-closing promise that IS true is made: an identical file coming back.
    assert "a missing file that comes back identical closes its own issue" in said


# === CLI: the plain policy names =============================================================


def _dispose_engine() -> None:
    """Close the aiosqlite connections a finished ``asyncio.run`` leaves on a dead loop (the
    discipline `tests/test_proof_serialization.py` established for `cli.main` tests)."""
    from src import database

    engine = database._engine
    database.reset_engine()
    if engine is not None:
        asyncio.run(engine.dispose())


def _cli(argv: list[str]) -> int:
    import src.cli as cli

    _dispose_engine()
    try:
        return cli.main(argv)
    finally:
        _dispose_engine()


def _stored_collections() -> dict[str, tuple[str, bool]]:
    rows = _run_check(_collections_by_name)
    _dispose_engine()
    return rows


@pytest.mark.parametrize(
    "given, stored",
    [
        ("locked", "worm"),
        ("working", "churn"),
        # The stored names still work, exactly as before.
        ("worm", "worm"),
        ("churn", "churn"),
    ],
)
def test_cli_add_collection_accepts_the_plain_policy_names(cairn_env, capsys, given, stored):
    root = cairn_env / "cli-root"
    root.mkdir()

    rc = _cli(["add-collection", "--name", "from-cli", "--root", str(root), "--mode", given])
    out = capsys.readouterr().out
    assert rc == 0, out

    # Stored as the canonical value — and, the CLI being scripted use, with auto-baseline off
    # unless it is asked for (only the panel's create form turns it on).
    assert _stored_collections() == {"from-cli": (stored, False)}
    assert f"mode={stored}" in out


def test_cli_add_collection_rejects_an_unknown_policy(cairn_env, capsys):
    root = cairn_env / "cli-root"
    root.mkdir()

    with pytest.raises(SystemExit) as exc:
        _cli(["add-collection", "--name", "from-cli", "--root", str(root), "--mode", "frozen"])
    assert exc.value.code not in (0, None)
    err = capsys.readouterr().err
    assert "--mode" in err and "frozen" in err
    # The rejection names the choices an operator can use, plain names first.
    assert "locked" in err and "working" in err
    assert _stored_collections() == {}


def test_cli_help_names_the_two_policies_in_plain_words(capsys):
    import src.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.main(["add-collection", "--help"])
    assert exc.value.code == 0
    out = re.sub(r"\s+", " ", capsys.readouterr().out)
    assert "'locked'" in out and "'working'" in out
    assert "nothing should change" in out and "edits are normal" in out


# --- an accept never runs under an operation (review round 2) --------------------------------


async def _seed_running_scan(cairn_env, name: str) -> tuple[int, int]:
    """A collection with one changed, unaccepted file and a LIVE running scan claimed on it."""
    from datetime import datetime, timezone

    from sqlalchemy import select

    from src.database import get_sessionmaker
    from src.models.db import Collection, FileEntry, Run
    from src.services import scanner
    from tests.conftest import seed_collection

    root = cairn_env / name
    root.mkdir()
    (root / "a.txt").write_text("one")
    cid = await seed_collection(root, ots_mode="none", mode="worm")
    async with get_sessionmaker()() as s:
        collection = await s.get(Collection, cid)
        await scanner.scan_collection(s, collection)
        (root / "a.txt").write_text("two, longer")
        await scanner.scan_collection(s, collection)
        fid = await s.scalar(select(FileEntry.id).where(FileEntry.collection_id == cid))
        now = datetime.now(timezone.utc)
        s.add(Run(collection_id=cid, kind="scan", result="running", started=now, heartbeat_at=now))
        await s.commit()
    return cid, fid


def test_cli_accept_refuses_while_an_operation_is_running(cairn_env, capsys):
    import asyncio

    from sqlalchemy import func, select

    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    cid, fid = asyncio.run(_seed_running_scan(cairn_env, "busy"))

    assert _cli(["accept", "--collection", "busy"]) == 1
    assert "refused" in capsys.readouterr().err

    async def state():
        async with get_sessionmaker()() as s:
            status = await s.scalar(select(FileEntry.status).where(FileEntry.id == fid))
            open_events = await s.scalar(
                select(func.count()).select_from(Event).where(
                    Event.collection_id == cid, Event.acknowledged_at.is_(None)
                )
            )
            return status, open_events

    assert asyncio.run(state()) == ("modified", 1)


def test_the_panel_accept_rechecks_for_an_operation_under_the_write_lock(cairn_env, monkeypatch):
    """The advisory pre-check runs before the writer lock is taken, so a scan can claim the
    collection in between. With that pre-check blinded, the under-lock re-check must still refuse."""
    import asyncio

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from src.control_panel import routes
    from src.database import get_sessionmaker
    from src.main import app
    from src.models.db import FileEntry

    cid, fid = asyncio.run(_seed_running_scan(cairn_env, "window"))

    async def no_run(session, collection_id):
        return None

    with TestClient(app) as client:
        page = client.get(f"/collection/{cid}/review").text
        fp = page.split('name="population_fp" value="')[1].split('"')[0]
        token = page.split('name="csrf_token" value="')[1].split('"')[0]
        monkeypatch.setattr(routes.collections_svc, "blocking_run", no_run)
        r = client.post(
            f"/collection/{cid}/review/adopt-changed",
            data={"csrf_token": token, "population_fp": fp, "dir": "", "deep": "1"},
            follow_redirects=False,
        )
    assert r.status_code == 303 and "stale=1" in r.headers["location"]

    async def status():
        async with get_sessionmaker()() as s:
            return await s.scalar(select(FileEntry.status).where(FileEntry.id == fid))

    assert asyncio.run(status()) == "modified"
