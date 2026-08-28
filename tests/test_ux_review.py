"""UX audit sprint 1, slice C: the review page, the acknowledgement vocabulary, the accept guard's
rendered half.

Covers #17 (Acknowledge → Mark reviewed, scoped/counted/confirmed bulk actions, un-inverted button
styles), #22's UI half (design D9's restored-only branch) and the two context keys the accept
population guard needs rendered (design D14/D12).

Two levels are used deliberately:

* **Route level** (TestClient) for everything the shipped `collection_review` route already
  publishes — `total_issues`, `review_open`, the rows.
* **Template level** (render the Jinja template against a hand-built context) for `population_fp`
  and `stale`, which the route publishes on every render. The template must render them when they
  are there and degrade to a fail-closed empty field when they are not, which a hand-built context
  is the only way to exercise.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_ux_review.py``
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

from tests.conftest import seed_collection  # noqa: F401  (cairn_env comes from conftest)

HINT = (
    "Notes that you've seen this. The file stays on record as missing or changed, keeps any "
    "existence proof, and the collection keeps its Alert status until you restore or retire it."
)
# The refusal banner leads with the CONSEQUENCE (live-pass M2): the operator clicked a destructive
# button and the first thing they need is that it did not run. Asserted as the lead phrase, not the
# whole paragraph — the second half branches on whether anything is left to review.
STALE_LEAD = "<strong>Your action was NOT applied</strong>"
STALE_NOTHING_MUTATED = "Nothing was deleted or acknowledged."
STALE_COPY = "this collection changed since the page loaded"


# --- helpers ---------------------------------------------------------------------------------


def _make_client(cairn_env, seed_coro):
    """Run an async seed on a throwaway loop, drop the engine, return a TestClient."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warnings)
    return TestClient(app)


async def _seed_missing_file_with_event(cid: int, relpath: str = "2019/IMG_4421.jpg") -> None:
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        fe = FileEntry(
            collection_id=cid, relpath=relpath, size=4321, sha256="d" * 64,
            status="missing", ots_state="complete", first_seen=now, last_checked=now,
        )
        s.add(fe)
        await s.commit()
        s.add(Event(collection_id=cid, file_id=fe.id, kind="missing", detected_at=now))
        await s.commit()


async def _seed_ok_file(cid: int) -> None:
    """One indexed, healthy file: the collection is genuinely "all clear", not merely empty."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="kept/steady.txt", size=10, sha256="b" * 64,
            status="ok", ots_state="complete", first_seen=now, last_checked=now,
        ))
        await s.commit()


async def _seed_ok_file_and_open_event(cid: int) -> None:
    """The restored-but-unreviewed state: nothing is missing or changed, one alert still open."""
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="back/again.txt", size=10, sha256="a" * 64,
            status="ok", ots_state="complete", first_seen=now, last_checked=now,
        ))
        s.add(Event(collection_id=cid, kind="missing", detected_at=now))
        await s.commit()


def _render_review(**overrides) -> str:
    """Render `collection_review.html` directly against a full, hand-built page context.

    The route publishes `population_fp` / `stale`; this renders the template's half of that
    contract independently, so the degraded (key-absent) render can be asserted too.
    """
    from starlette.requests import Request

    from src.control_panel.routes import templates
    from src.main import app

    request = Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": "/collection/1/review", "raw_path": b"/collection/1/review",
        "query_string": b"", "root_path": "", "headers": [(b"host", b"testserver")],
        "client": ("testclient", 50000), "server": ("testserver", 80),
        "app": app, "router": app.router,
    })
    ctx = {
        "request": request, "page": "collection", "mode": "dark", "auth_mode": "single",
        "username": "cairn", "is_admin": True, "user_email": "cairn@localhost",
        "sidebar_collections": [], "alert_count": 1, "csrf_token": "tok",
        "c": {
            "id": 1, "name": "Photos", "root": "/data/photos",
            "counts": {"missing": 1, "modified": 0},
        },
        "items": [], "total_issues": 1, "shown": 0, "truncated": False, "root": "/data/photos",
        "copy_relpaths": "a/b.jpg", "copy_count": 1, "copy_truncated": False, "review_open": 1,
    }
    ctx.update(overrides)
    return templates.get_template("collection_review.html").render(ctx)


# --- #17: the vocabulary, the counts, the confirm, the button styles -------------------------


def test_review_row_control_is_mark_reviewed_and_never_the_loud_button(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "Mark reviewed" in body
    assert "Acknowledge" not in body           # the old verb is gone from this surface
    assert HINT in body                        # #17's hint copy, verbatim
    # Un-inverted (design D8): the per-file control is subtle even for a *missing* file, because it
    # changes nothing about the file.
    row_button = re.search(
        r'<button class="btn btn--sm ([^"]*)"[^>]*hx-post="/events/\d+/ack\?view=review"', body
    )
    assert row_button, "the per-file ack control is not where the test expects it"
    assert "btn--subtle" in row_button.group(1)
    assert "btn--danger" not in row_button.group(1)


def test_review_bulk_ack_is_counted_and_says_what_it_does_not_do(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "Mark all 1 reviewed" in body
    assert "Clears 1 alerts in this collection. Nothing about the files changes." in body
    assert "/collection/1/review/ack-all" in body


def test_dashboard_bulk_ack_carries_its_count_and_a_confirm(cairn_env):
    """It reaches events the 20-row feed never showed, so it needs both the count and the confirm."""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    assert "Mark all 1 reviewed (all collections)" in html
    assert "hx-confirm=" in html
    assert (
        "Marks 1 alerts across all your collections as seen. The files stay missing or changed "
        "and the red counts stay — this only clears the notification."
    ) in html


def test_event_feed_row_control_is_subtle_and_hinted(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        html = client.get("/").text

    feed_button = re.search(r'<button class="btn btn--sm ([^"]*)"[^>]*hx-post="/events/\d+/ack"', html)
    assert feed_button and "btn--subtle" in feed_button.group(1)
    assert "btn--danger" not in feed_button.group(1)
    assert HINT in html


# --- #22 / design D9: the restored-only branch -----------------------------------------------


def test_restored_only_review_offers_mark_all_reviewed_and_no_accept(cairn_env):
    """`total_issues == 0` with `review_open > 0`: clearable alerts, but Accept is not offered.

    From an otherwise-empty page Accept would baseline every pending `new` file in the collection.
    """
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_ok_file_and_open_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "All clear" in body
    assert "from files that have since been restored" in body
    assert "Mark all 1 reviewed" in body
    assert "/collection/1/review/ack-all" in body
    assert "/collection/1/review/accept" not in body
    assert "Accept all changes" not in body


def test_all_clear_with_nothing_open_offers_no_controls(cairn_env):
    """A collection with files, all healthy. (A collection with NO files is a different card —
    see `test_a_zero_file_collections_review_page_never_reads_all_clear`.)"""
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_ok_file(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    assert "All clear" in body
    assert "Mark all" not in body
    assert "from files that have since been restored" not in body


def test_a_zero_file_collections_review_page_never_reads_all_clear(cairn_env):
    """#31 on the review surface: watching nothing establishes nothing, so no green "All clear"."""
    root = cairn_env / "empty"
    root.mkdir()

    with _make_client(cairn_env, lambda: seed_collection(root)) as client:
        body = client.get("/collection/1/review").text

    assert "All clear" not in body
    assert "No files indexed yet" in body
    assert "Nothing has been checked in this collection" in body


def test_a_concurrent_accept_between_the_two_reads_never_reads_all_clear(cairn_env, monkeypatch):
    """The review page's zero-file state must come from the population snapshot, not an earlier read.

    `_collection_view` counts the collection's files in its own statement; the protected population
    is read seconds later in another. Python's sqlite3 runs in legacy transaction mode, so those
    are two independent snapshots: an accept committed from another tab in between (adopting the
    last missing file and leaving the collection empty) left `is_empty` false while the population
    read returned nothing — and the page rendered a green "All clear" for a collection that has
    established nothing. The interleaving is forced here by mutating inside `_collection_view`.
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
            # The concurrent accept, on its own connection: the last missing row is adopted and
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


# --- #32: the truncation deep-link and the recovery copy -------------------------------------


def test_truncation_notice_deep_links_to_the_filtered_list(cairn_env):
    """Slice B adds `view`/`filter` support; the link lands regardless (it degrades to today's page)."""
    html = _render_review(truncated=True, total_issues=900, shown=500)
    assert "/collection/1?view=list&amp;filter=issues" in html


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


# --- design D14: the two keys this template renders for the accept guard ----------------------


def test_accept_form_is_the_loud_button_and_carries_the_population_field(cairn_env):
    root = cairn_env / "photos"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_missing_file_with_event(cid)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/collection/1/review").text

    # Un-inverted (design D8): the destructive control is the loud one.
    assert 'class="btn btn--danger btn--full"' in body
    # The route publishes a real fingerprint, so the rendered form carries one (design D14). The
    # POST fails closed on an absent or empty field, so an empty value here would refuse accepts.
    field = re.search(r'name="population_fp" value="([^"]*)"', body)
    assert field, "the accept form does not carry a population_fp field"
    assert re.fullmatch(r"[0-9a-f]{64}", field.group(1))


def test_population_fp_is_rendered_when_the_route_publishes_it(cairn_env):
    html = _render_review(population_fp="a" * 64)
    assert f'name="population_fp" value="{"a" * 64}"' in html
    # Absent from the context, the field is still there and still fails closed at the route.
    assert 'name="population_fp" value=""' in _render_review()


def test_stale_banner_renders_only_when_the_route_says_stale(cairn_env):
    banner = _render_review(stale=True)
    assert STALE_COPY in banner
    assert "id=\"stale-banner\"" in banner
    assert STALE_COPY not in _render_review()          # no key -> no banner
    assert STALE_COPY not in _render_review(stale="")  # falsy -> no banner


def test_stale_banner_leads_with_the_action_not_having_been_applied(cairn_env):
    """M2: the cause is not the headline — the consequence is.

    "This collection changed since the page loaded" is a true statement *about the collection* that
    never says the operator's Accept did nothing. Read at speed after clicking a destructive button
    it parses as a status note on a page that has apparently been accepted.
    """
    banner = _render_review(stale=True, total_issues=1)
    assert STALE_LEAD in banner
    assert STALE_NOTHING_MUTATED in banner
    # Order: consequence, then cause. Not merely "both strings are present somewhere".
    assert banner.index(STALE_LEAD) < banner.index(STALE_COPY)
    # With a list to look at, the operator is told to look at it.
    assert "The list below is current" in banner


def test_stale_banner_refused_onto_an_empty_review_still_says_it_was_not_applied(cairn_env):
    """M2: the refusal redirects unconditionally, so it can land on an all-clear page.

    "The list below is current" over an empty page reads as if the accept had emptied it — the
    exact false impression the banner exists to prevent.
    """
    banner = _render_review(stale=True, total_issues=0, review_open=0)
    assert STALE_LEAD in banner
    assert STALE_NOTHING_MUTATED in banner
    assert "The list below is current" not in banner
    assert "may already have been handled" in banner
    # The all-clear card still carries the way out; no "try again" is invented for it.
    assert "Back to Photos" in banner


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


# --- live UX pass: review-page copy (#11 / #14 / #18) -----------------------------------------


def test_the_truncation_notice_points_at_the_filtered_browser_not_an_unbuilt_command(cairn_env):
    """#11: `cairn status` is listed as *planned* — the notice sent the operator to a command that
    does not exist, when the page already links the place the full set actually lives."""
    html = _render_review(copy_truncated=True, copy_count=2000, total_issues=9000)

    assert "cairn status" not in html
    assert 'href="/collection/1?view=list&amp;filter=issues"' in html
    assert "The buttons copy the first 2,000 of 9,000 paths" in html


def test_the_recovery_copy_refers_to_the_button_not_to_a_position(cairn_env):
    """#14: "the list above" names nothing — the paths are on the clipboard, not on the page."""
    html = _render_review()

    assert "Paste the list above" not in html
    assert "Paste the copied list" in html


def test_the_review_intro_is_withheld_when_there_is_nothing_to_review(cairn_env):
    """#18: on an all-clear page the intro described missing/changed files and offered recovery
    instructions for a set that is empty — the page then contradicts it two lines down."""
    intro = "that went missing or changed"

    assert intro in _render_review(total_issues=1, review_open=1)
    # Restored-but-unread alerts are still something to act on, so the intro stays.
    assert intro in _render_review(total_issues=0, review_open=2)
    # Genuinely nothing to do: no intro.
    assert intro not in _render_review(total_issues=0, review_open=0)


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
