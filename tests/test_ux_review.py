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
STALE_COPY = "This collection changed since the page loaded — the list below is current."


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
    assert STALE_COPY in _render_review(stale=True)
    assert "id=\"stale-banner\"" in _render_review(stale=True)
    assert STALE_COPY not in _render_review()          # no key -> no banner
    assert STALE_COPY not in _render_review(stale="")  # falsy -> no banner


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
