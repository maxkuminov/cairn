"""Run health + the health pill + the #24 verify remainder (add-fleet-review-and-run-health, slice B).

Three families, one theme: **no surface may report a status the data does not support.**

* ``compute_health``'s two freshness legs (#28 / app-runtime). A completed scan inside its window is
  fresh; a scan that is in flight and *still heartbeating* is fresh regardless of the cadence (issue
  #5 — a long scan must not age out its own freshness); an **abandoned** ``running`` row is stale,
  because a process that died mid-scan is exactly what the dead-man's switch exists to report. The
  panel's health is owner-scoped, ``/healthz`` is not.
* ``runs.errors`` / ``runs.error_sample`` and the run-health line (#29). A ``partial`` scan must
  never render like a clean one, an ``interrupted`` run must never render like a failure, and the
  sample must stay inside its three bounds while still telling the truth about what it dropped.
* The verify card's retry affordance and typed transport reason (#24 remainder).

Run from the repo root: ``PYTHONPATH=. pytest tests/test_run_health.py``
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import seed_collection  # noqa: F401  (cairn_env fixture lives beside it)

# --- harness --------------------------------------------------------------------------------


def _make_client(seed_coro):
    """Seed on a throwaway loop, drop the engine, return a TestClient (see tests/test_panel.py)."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()
    return TestClient(app)


def _csrf_token(client) -> str:
    html = client.get("/").text
    m = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _new_collection(
    root: Path, *, cadence: int = 900, name: str | None = None, ots_mode: str = "none"
) -> int:
    """One collection owned by the implicit (single-mode) user. Threshold = max(2*cadence, floor)."""
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    root.mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)
        uid = await s.scalar(select(User.id).order_by(User.id))
        c = await create_collection(
            s,
            user_id=uid,
            name=name or root.name,
            root=str(root),
            mode="worm",
            ots_mode=ots_mode,
            hash_cadence_seconds=cadence,
        )
        return c.id


async def _second_user_collection(root: Path, *, username: str, name: str | None = None) -> int:
    """A collection owned by a DIFFERENT user (higher id, so `current_user` never picks them)."""
    from src.database import ensure_implicit_user, get_sessionmaker
    from src.models.db import User
    from src.services.collections import create_collection

    root.mkdir(parents=True, exist_ok=True)
    async with get_sessionmaker()() as s:
        await ensure_implicit_user(s)  # keep the implicit user at id 1
        u = User(username=username, is_admin=False, is_active=True, created_at=_utcnow())
        s.add(u)
        await s.flush()
        c = await create_collection(
            s,
            user_id=u.id,
            name=name or root.name,
            root=str(root),
            mode="worm",
            ots_mode="none",
        )
        return c.id


async def _add_run(
    cid: int,
    *,
    result: str,
    started: datetime,
    finished: datetime | None = None,
    heartbeat: datetime | None = None,
    kind: str = "scan",
    errors: int = 0,
    error_sample: str | None = None,
) -> int:
    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        run = Run(
            collection_id=cid,
            kind=kind,
            started=started,
            finished=finished,
            heartbeat_at=heartbeat,
            result=result,
            errors=errors,
            error_sample=error_sample,
        )
        s.add(run)
        await s.commit()
        return run.id


async def _age_collection(cid: int, *, seconds: float) -> None:
    """Push `created_at` back so the startup grace cannot mask what a test is isolating."""
    from src.database import get_sessionmaker
    from src.models.db import Collection

    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        c.created_at = _utcnow() - timedelta(seconds=seconds)
        await s.commit()


async def _health(user_id: int | None = None):
    from src.config import get_settings
    from src.database import get_sessionmaker
    from src.services.scheduler import compute_health

    async with get_sessionmaker()() as s:
        return await compute_health(s, get_settings(), user_id=user_id)


# --- 3.25 compute_health's two freshness legs -------------------------------------------------


def test_leg_a_completed_scan_inside_the_window_is_fresh(cairn_env):
    async def go():
        cid = await _new_collection(cairn_env / "a-fresh", cadence=900)  # threshold 1800s
        now = _utcnow()
        await _add_run(
            cid, result="ok", started=now - timedelta(seconds=120),
            finished=now - timedelta(seconds=60),
        )
        report = await _health()
        row = report.collections[0]
        assert report.status == "ok"
        assert row.state == "fresh"
        assert row.last_scan_age_seconds is not None
        assert row.last_scan_age_seconds < 1800

    asyncio.run(go())


def test_leg_a_completed_scan_outside_the_window_is_stale(cairn_env):
    async def go():
        cid = await _new_collection(cairn_env / "a-stale", cadence=900)
        now = _utcnow()
        await _add_run(
            cid, result="ok", started=now - timedelta(hours=3), finished=now - timedelta(hours=3)
        )
        report = await _health()
        assert report.status == "degraded"
        assert report.collections[0].state == "stale"

    asyncio.run(go())


def test_leg_b_a_live_long_running_scan_stays_fresh_with_no_completed_age(cairn_env):
    """Issue #5: a scan that outlives its own cadence must not trip the switch against itself.

    `started` is deliberately hours past the freshness window and there is NO completed scan — the
    only thing keeping the collection fresh is a heartbeat seconds old. And because nothing has
    finished, the reported age is `None` rather than the in-flight run's elapsed time: an unfinished
    run has no "last scan" age, and reporting one would state a completion that has not happened.
    """

    async def go():
        cid = await _new_collection(cairn_env / "b-live", cadence=900)
        await _age_collection(cid, seconds=6 * 3600)  # well past the startup grace
        now = _utcnow()
        await _add_run(
            cid, result="running", started=now - timedelta(hours=4),
            heartbeat=now - timedelta(seconds=5),
        )
        report = await _health()
        assert report.status == "ok"
        assert report.collections[0].state == "fresh"
        assert report.collections[0].last_scan_age_seconds is None

    asyncio.run(go())


def test_leg_b_an_abandoned_running_scan_confers_no_freshness(cairn_env):
    """`result='running'` is not evidence of life — a crashed scanner must read stale, not fresh."""

    async def go():
        from src.services.collections import RUN_HEARTBEAT_TIMEOUT_SECONDS

        cid = await _new_collection(cairn_env / "b-dead", cadence=900)
        await _age_collection(cid, seconds=6 * 3600)
        now = _utcnow()
        # Started RECENTLY (so the old `started`-dated leg would have called it fresh) but its last
        # reported progress is past the claim-abandonment interval.
        await _add_run(
            cid, result="running", started=now - timedelta(seconds=30),
            heartbeat=now - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60),
        )
        report = await _health()
        assert report.status == "degraded"
        assert report.collections[0].state == "stale"
        assert report.collections[0].last_scan_age_seconds is None

    asyncio.run(go())


def test_leg_b_falls_back_to_started_when_no_heartbeat_was_ever_written(cairn_env):
    """A pre-0011 row (or one that has not reported yet) is dated from `started`, like the reaper."""

    async def go():
        from src.services.collections import RUN_HEARTBEAT_TIMEOUT_SECONDS

        env = cairn_env
        live = await _new_collection(env / "b-nohb-live", cadence=900)
        dead = await _new_collection(env / "b-nohb-dead", cadence=900)
        for cid in (live, dead):
            await _age_collection(cid, seconds=6 * 3600)
        now = _utcnow()
        await _add_run(live, result="running", started=now - timedelta(seconds=10))
        await _add_run(
            dead, result="running",
            started=now - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS + 60),
        )
        by_id = {r.id: r.state for r in (await _health()).collections}
        assert by_id[live] == "fresh"
        assert by_id[dead] == "stale"

    asyncio.run(go())


def test_grace_covers_a_never_scanned_collection_only(cairn_env):
    """`pending` is for "nothing has ever run here", not for "something ran and did not finish"."""

    async def go():
        env = cairn_env
        virgin = await _new_collection(env / "d-virgin", cadence=900)
        old = await _new_collection(env / "d-old", cadence=900)
        errored = await _new_collection(env / "d-errored", cadence=900)
        interrupted = await _new_collection(env / "d-interrupted", cadence=900)

        await _age_collection(old, seconds=6 * 3600)  # past the grace, still no runs
        now = _utcnow()
        # These two are INSIDE the startup grace (created moments ago) but each has a terminal scan
        # run: the grace no longer applies, so both are stale.
        await _add_run(errored, result="error", started=now - timedelta(seconds=30), finished=now)
        await _add_run(
            interrupted, result="interrupted", started=now - timedelta(seconds=30), finished=now
        )

        by_id = {r.id: r.state for r in (await _health()).collections}
        assert by_id[virgin] == "pending"
        assert by_id[old] == "stale"
        assert by_id[errored] == "stale"
        assert by_id[interrupted] == "stale"

    asyncio.run(go())


def test_a_recent_stamp_or_upgrade_run_alone_is_still_stale(cairn_env):
    """Freshness counts `kind='scan'` only, so a nightly upgrade cannot refresh the switch."""

    async def go():
        env = cairn_env
        stamped = await _new_collection(env / "e-stamp", cadence=900)
        upgraded = await _new_collection(env / "e-upgrade", cadence=900)
        for cid in (stamped, upgraded):
            await _age_collection(cid, seconds=6 * 3600)
        now = _utcnow()
        await _add_run(stamped, kind="stamp", result="ok", started=now, finished=now)
        await _add_run(upgraded, kind="upgrade", result="ok", started=now, finished=now)
        # And a LIVE `running` stamp must not sneak in through leg (b) either.
        await _add_run(
            stamped, kind="stamp", result="running", started=now, heartbeat=now
        )

        report = await _health()
        assert report.status == "degraded"
        assert all(r.state == "stale" for r in report.collections)

    asyncio.run(go())


# --- 3.18 the collection id travels with the freshness record ---------------------------------


def test_healthz_carries_the_collection_id_alongside_the_existing_keys(cairn_env):
    async def seed():
        cid = await _new_collection(cairn_env / "hz", cadence=900)
        now = _utcnow()
        await _add_run(cid, result="ok", started=now, finished=now)

    with _make_client(seed) as client:
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        entry = body["collections"][0]
        # Additive: nothing renamed, retyped or removed, so an external monitor's parse is intact.
        assert set(entry) == {"id", "name", "state", "last_scan_age_seconds"}
        assert isinstance(entry["id"], int)
        assert entry["name"] == "hz"
        assert entry["state"] == "fresh"


def test_healthz_reports_error_when_the_datastore_cannot_be_reached(cairn_env):
    """app-runtime "Datastore unreachable" (matrix 4.4).

    The delta marks this scenario "existing", but the matrix walk found nothing asserting it: the
    503/``error`` leg of the dead-man's switch was the one `/healthz` outcome no test covered.
    It matters most exactly here — a switch that answered 200 with an unreadable datastore would
    report health it never computed, which is this change's whole defect class.
    """

    async def seed():
        await _new_collection(cairn_env / "hz-down", cadence=900)

    with _make_client(seed) as client:
        import src.main as main

        async def _dead() -> bool:
            return False

        original, main.ping = main.ping, _dead
        try:
            resp = client.get("/healthz")
        finally:
            main.ping = original

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "error"
        # No freshness list: an unreadable datastore yields no per-collection verdict to state.
        assert "collections" not in body

        # …and the switch recovers rather than latching.
        assert client.get("/healthz").status_code == 200


# --- 3.26 / 3.27 the panel is owner-scoped; /healthz is not -----------------------------------


def test_panel_health_is_owner_scoped_while_healthz_stays_fleet_global(cairn_env):
    """User A owns only fresh collections; user B owns a stale one.

    A's pill must read healthy and A's cards must carry no stale marker — a fleet count above an
    owner-scoped list names a collection that is not on the page it links to. `/healthz` is
    unauthenticated and monitors the installation, so it still reports the fleet degraded.
    """

    async def seed():
        mine = await _new_collection(cairn_env / "mine", cadence=900)
        theirs = await _second_user_collection(cairn_env / "theirs", username="bob")
        now = _utcnow()
        await _add_run(mine, result="ok", started=now, finished=now)
        await _age_collection(theirs, seconds=6 * 3600)  # no runs at all, past the grace → stale

    with _make_client(seed) as client:
        pill = client.get("/health-pill").text
        assert "Healthy" in pill
        assert "Degraded" not in pill

        collections = client.get("/collections").text
        assert "scan overdue" not in collections
        assert "theirs" not in collections  # owner scoping, unchanged

        body = client.get("/healthz").json()
        assert body["status"] == "degraded"
        names = {c["name"] for c in body["collections"]}
        assert names == {"mine", "theirs"}


def test_the_stale_marker_attaches_by_id_not_by_name(cairn_env):
    """Two collections, same name, different owners, one stale.

    No constraint makes a collection name unique across owners, so a name-keyed match would put
    another owner's overdue marker on this owner's card.
    """

    async def seed():
        mine = await _new_collection(cairn_env / "dup-mine", cadence=900, name="Photos")
        theirs = await _second_user_collection(
            cairn_env / "dup-theirs", username="bob", name="Photos"
        )
        now = _utcnow()
        await _add_run(mine, result="ok", started=now, finished=now)
        await _age_collection(theirs, seconds=6 * 3600)

    with _make_client(seed) as client:
        html = client.get("/collections").text
        assert html.count("Photos") >= 1
        assert "scan overdue" not in html  # the stale one belongs to the OTHER owner
        assert "Healthy" in client.get("/health-pill").text


def _card_for(html: str, cid: int) -> str:
    """The slice of a rendered page belonging to one collection card (cards are keyed by id)."""
    marker = f'collection-card" href="/collection/{cid}"'
    start = html.index(marker)
    nxt = html.find('collection-card" href="/collection/', start + len(marker))
    return html[start : nxt if nxt != -1 else len(html)]


@pytest.mark.parametrize("page", ["/", "/collections"])
def test_the_stale_marker_reaches_the_cards_on_both_listing_pages(cairn_env, page):
    """3.7: `dashboard()` and `collections_list()` both attach owner-scoped freshness to their cards.

    The pill says "Degraded - N collection(s)"; these markers are the only thing that says WHICH.
    The dashboard is the page the operator is already on, so a missing call there leaves an overdue
    collection rendering exactly like a freshly-scanned one.
    """

    async def seed():
        fresh = await _new_collection(cairn_env / "dash-fresh", cadence=900, name="Fresh One")
        stale = await _new_collection(cairn_env / "dash-stale", cadence=900, name="Stale One")
        now = _utcnow()
        await _add_run(fresh, result="ok", started=now, finished=now)
        await _age_collection(stale, seconds=6 * 3600)  # no runs at all, past the grace
        await _age_collection(fresh, seconds=6 * 3600)  # grace must not be what keeps it fresh
        return fresh, stale

    ids: dict[str, int] = {}

    async def seed_and_record():
        fresh, stale = await seed()
        ids["fresh"], ids["stale"] = fresh, stale

    with _make_client(seed_and_record) as client:
        html = client.get(page).text
        assert html.count("scan overdue") == 1
        assert "scan overdue" in _card_for(html, ids["stale"])
        assert "scan overdue" not in _card_for(html, ids["fresh"])
        # ...and the count the pill names comes from the same owner-scoped report.
        assert "Degraded" in client.get("/health-pill").text


# --- 3.19 the pill names what is degraded, and nothing fabricates a verdict --------------------


def test_the_degraded_pill_is_a_link_that_names_the_count(cairn_env):
    async def seed():
        cid = await _new_collection(cairn_env / "pill-stale", cadence=900)
        await _age_collection(cid, seconds=6 * 3600)

    with _make_client(seed) as client:
        html = client.get("/health-pill").text
        assert 'href="/collections"' in html
        assert "<a" in html
        # VISIBLE, not a `title`: touch never fires a tooltip and the hint is hidden on phones.
        assert "Degraded" in html
        assert "1 collection" in html
        assert "1 collections" not in html


def test_the_pill_claims_nothing_before_the_poll_has_answered(cairn_env):
    async def seed():
        cid = await _new_collection(cairn_env / "pre-poll", cadence=900)
        now = _utcnow()
        await _add_run(cid, result="ok", started=now, finished=now)

    with _make_client(seed) as client:
        page = client.get("/").text
        head = page.split('id="health-pill"')[0] + page.split('id="health-pill"')[1][:600]
        assert "Checking" in page
        # The server-rendered shell must not assert a verdict it has not computed.
        assert "health-pill__label" in page
        assert ">Healthy<" not in head
        assert 'hx-trigger="load, every 30s"' in page


def test_the_neutral_pill_is_rendered_by_every_page_shell(cairn_env):
    """Integration (4.2): the pill lives in `base.html`, so EVERY page that extends it — including
    Slice A's new `/review` — must ship the neutral pre-poll state and the `load` trigger.

    A page that rendered the shell without the include would show no health indicator at all; one
    that re-hardcoded a green pill would be the original defect returning by another door.
    """

    async def seed():
        cid = await _new_collection(cairn_env / "shell", cadence=900)
        now = _utcnow()
        await _add_run(cid, result="ok", started=now, finished=now)
        return cid

    ids: dict[str, int] = {}

    async def seed_and_record():
        ids["cid"] = await seed()

    with _make_client(seed_and_record) as client:
        for path in ("/", "/collections", "/review", "/settings", f"/collection/{ids['cid']}"):
            html = client.get(path).text
            assert 'id="health-pill"' in html, path
            assert "Checking" in html, path
            assert 'hx-trigger="load, every 30s"' in html, path
            head = html.split('id="health-pill"')[1][:600]
            assert ">Healthy<" not in head, path


def test_the_settings_page_shows_no_fabricated_health_pill(cairn_env):
    async def seed():
        await _new_collection(cairn_env / "settings-health", cadence=900)

    with _make_client(seed) as client:
        html = client.get("/settings").text
        assert "Health endpoint" in html  # the endpoint documentation stays
        assert "healthz-url" in html  # so does the copy control
        # …but the static `pill--ok` "Healthy" beside the title, computed from nothing, is gone.
        # Scoped to that card's own markup — other cards legitimately carry computed pills.
        card = html.split("Health endpoint", 1)[1].split("healthz-url", 1)[0]
        assert "pill--ok" not in card
        assert "Healthy" not in card


# --- 3.20 / 3.28 the scanner persists what it skipped, within bounds --------------------------


def _unstorable_name() -> str:
    """A filename `os.walk` surfaces as a lone surrogate — SQLite TEXT cannot bind it."""
    return "bad-\udcff-name.txt"


async def _scan(cid: int, *, deep: bool = False):
    from src.database import get_sessionmaker
    from src.models.db import Collection
    from src.services.scanner import scan_collection

    async with get_sessionmaker()() as s:
        c = await s.get(Collection, cid)
        return await scan_collection(s, c, deep=deep)


async def _latest_run(cid: int):
    from src.database import get_sessionmaker
    from src.models.db import Run

    async with get_sessionmaker()() as s:
        return await s.scalar(
            select(Run).where(Run.collection_id == cid).order_by(Run.id.desc()).limit(1)
        )


def test_an_unstorable_name_is_counted_and_named_in_the_run(cairn_env):
    root = cairn_env / "unstorable"

    async def go():
        cid = await _new_collection(root)
        (root / "fine.txt").write_text("ok")
        try:
            (root / _unstorable_name()).write_bytes(b"x")
        except (OSError, UnicodeEncodeError):  # pragma: no cover - filesystem refuses the name
            pytest.skip("this filesystem will not create a non-UTF-8 filename")
        summary = await _scan(cid)
        assert summary.result == "partial"

        run = await _latest_run(cid)
        assert run.result == "partial"
        assert run.errors >= 1
        assert run.error_sample
        # The stored value must round-trip as ASCII — writing the RAW name would reproduce the very
        # UnicodeEncodeError this column exists to report.
        run.error_sample.encode("ascii")
        entries = json.loads(run.error_sample)
        assert isinstance(entries, list)
        assert all(isinstance(e, str) for e in entries)
        assert any(e.startswith("unstorable-name: ") for e in entries)
        return cid

    cid = asyncio.run(go())

    from src import database

    database.reset_engine()
    from fastapi.testclient import TestClient

    from src.main import app

    with TestClient(app) as client:
        card = client.get("/collections").text
        assert "partial" in card
        assert "skipped" in card
        assert f"/collection/{cid}" in card


def test_an_oversized_entry_is_truncated_on_the_byte_bound_and_marked(cairn_env):
    from src.services.scanner import (
        RUN_ERROR_SAMPLE_ENTRY_BYTES,
        _render_skip,
    )

    entry = _render_skip("hash", "x" * 4000)
    encoded = entry.encode("utf-8")
    assert len(encoded) <= RUN_ERROR_SAMPLE_ENTRY_BYTES
    assert entry.endswith("...")
    entry.encode("ascii")  # still ASCII after the cut


def test_a_flood_of_skips_stays_inside_the_total_budget_and_counts_the_remainder(cairn_env):
    from src.services.scanner import (
        RUN_ERROR_SAMPLE_MAX,
        RUN_ERROR_SAMPLE_TOTAL_BYTES,
        _build_error_sample,
        _render_skip,
    )

    total = 5_000
    entries = [_render_skip("stat", f"deep/nested/path/{i}/" + "n" * 200) for i in range(40)]
    raw = _build_error_sample(entries, total)
    assert raw is not None
    encoded = raw.encode("utf-8")
    assert len(encoded) <= RUN_ERROR_SAMPLE_TOTAL_BYTES
    raw.encode("ascii")  # `json.dumps` runs at ensure_ascii=True

    parsed = json.loads(raw)
    assert isinstance(parsed, list)
    assert all(isinstance(e, str) for e in parsed)
    marker = parsed[-1]
    assert marker.startswith("+") and marker.endswith("more skipped (sample truncated)")
    real = parsed[:-1]
    assert len(real) <= RUN_ERROR_SAMPLE_MAX
    # The remainder is the TRUE one — total errors minus the entries actually stored — not the
    # remainder of the cap.
    dropped = int(re.match(r"\+(\d+) more", marker).group(1))
    assert dropped + len(real) == total


def test_the_entry_cap_alone_still_reports_the_true_remainder(cairn_env):
    from src.services.scanner import RUN_ERROR_SAMPLE_MAX, _build_error_sample, _render_skip

    total = RUN_ERROR_SAMPLE_MAX + 7
    entries = [_render_skip("hash", f"f{i}.bin") for i in range(RUN_ERROR_SAMPLE_MAX)]
    parsed = json.loads(_build_error_sample(entries, total))
    assert len(parsed) == RUN_ERROR_SAMPLE_MAX + 1
    assert parsed[-1] == "+7 more skipped (sample truncated)"


def test_no_skips_stores_no_sample(cairn_env):
    from src.services.scanner import _build_error_sample

    assert _build_error_sample([], 0) is None


class _Capture(logging.Handler):
    """Own handler on `cairn.scanner`, rather than `caplog`.

    `caplog` installs its handler on the ROOT logger, so what it sees depends on global logging
    state another test in this module may have changed (a TestClient lifespan configures logging).
    Attaching directly to the logger under test makes the assertion about the scanner, not about
    the suite's logging configuration.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_a_stat_only_skip_is_logged_at_finalize(cairn_env, monkeypatch):
    """The `stat` skip logged NOTHING before 0012 — it was counted into `partial` and named nowhere.

    Without the finalize WARNING the operator's only copy of "which files" would be the column, and
    a schema downgrade dropping it would destroy information that exists nowhere else.
    """
    root = cairn_env / "statskip"

    async def go():
        cid = await _new_collection(root)
        (root / "gone.txt").write_text("x")
        (root / "kept.txt").write_text("y")

        real_stat = Path.stat

        def flaky_stat(self, *a, **kw):
            # Only the scanner's own `full.stat()` (which follows symlinks); `is_symlink()` runs
            # first via `lstat` and is a different, unguarded call we are not exercising here.
            if self.name == "gone.txt" and kw.get("follow_symlinks", True):
                raise OSError(13, "Permission denied")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        log = logging.getLogger("cairn.scanner")
        handler = _Capture()
        # Pin the level (and un-disable) for the duration: another module's TestClient lifespan can
        # leave the root logger at a level that swallows a WARNING before any handler sees it, and
        # what is under test here is the SCANNER's line, not the suite's logging configuration.
        previous_level, previous_disabled = log.level, log.disabled
        log.setLevel(logging.WARNING)
        log.disabled = False
        log.addHandler(handler)
        try:
            summary = await _scan(cid)
        finally:
            log.removeHandler(handler)
            log.setLevel(previous_level)
            log.disabled = previous_disabled
        assert summary.result == "partial"

        run = await _latest_run(cid)
        assert run.errors == 1
        entries = json.loads(run.error_sample)
        assert any(e.startswith("stat: ") for e in entries)

        assert any("skipped 1 file(s)" in m for m in handler.messages), handler.messages
        # …and it names the sample, so the diagnosis survives outside the run row.
        assert any("stat: " in m for m in handler.messages), handler.messages

    asyncio.run(go())


# --- 3.21 / 3.22 / 3.29 the run-health line at every site --------------------------------------


def _render_sites(client, cid: int) -> dict[str, str]:
    """The three places a scan result is reported (design D13's site list)."""
    return {
        "card": client.get("/collections").text,
        "detail": client.get(f"/collection/{cid}").text,
    }


PARTIAL_SAMPLE = json.dumps(["unstorable-name: b'bad\\xff.txt'"])


def _seed_run_state(root_name: str):
    """Build a seeder for one collection with a scripted run history."""

    def build(cairn_env, runs):
        async def seed():
            cid = await _new_collection(cairn_env / root_name)
            for kwargs in runs:
                await _add_run(cid, **kwargs)
            return cid

        return seed

    return build


@pytest.mark.parametrize("ots_mode", ["none", "perfile"])
def test_a_partial_completed_scan_says_so_at_every_site(cairn_env, ots_mode):
    """3.29, both ``ots_mode`` values at the detail header.

    A `perfile` collection has no "Last scan" tile — that tile exists only for `ots_mode == "none"`
    — so the header note under the status pill is the ONLY place such a collection can learn its
    last scan was partial. Testing one mode left the other's single disclosure site unasserted.
    """
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / f"rh-partial-{ots_mode}", ots_mode=ots_mode)
        now = _utcnow()
        await _add_run(
            cid, result="partial", started=now - timedelta(minutes=5), finished=now,
            errors=3, error_sample=PARTIAL_SAMPLE,
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        cid = holder["cid"]
        for site, html in _render_sites(client, cid).items():
            assert "partial" in html, site
            assert "3 files skipped" in html, site
        detail = client.get(f"/collection/{cid}").text
        # The bounded diagnostic sample is offered, and labelled as a rendering rather than a path.
        assert "What was skipped" in detail
        assert "not usable paths" in detail
        assert "unstorable-name" in detail


def test_a_clean_scan_renders_no_run_health_note(cairn_env):
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-clean")
        now = _utcnow()
        await _add_run(cid, result="ok", started=now - timedelta(minutes=5), finished=now)
        holder["cid"] = cid

    with _make_client(seed) as client:
        for site, html in _render_sites(client, holder["cid"]).items():
            assert "files skipped" not in html, site
            assert "was interrupted" not in html, site
            assert "a later scan failed" not in html, site


def test_a_later_interrupted_run_is_disclosed_neutrally_and_does_not_move_last_scan(cairn_env):
    """`interrupted` is what a reclaimed abandoned claim writes — the routine record of a deploy.

    It must be disclosed (something happened after the last completed scan that the operator cannot
    otherwise see) and it must never be drawn as a failure, or the routine consequence of restarting
    the app becomes the alarm that teaches the operator to ignore alarms.
    """
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-interrupted")
        now = _utcnow()
        await _add_run(
            cid, result="ok", started=now - timedelta(hours=2),
            finished=now - timedelta(hours=2),
        )
        await _add_run(
            cid, result="interrupted", started=now - timedelta(minutes=10),
            finished=now - timedelta(minutes=5),
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        for site, html in _render_sites(client, holder["cid"]).items():
            assert "a later scan was interrupted" in html, site
            assert "a later scan failed" not in html, site
            assert "run-health--muted" in html, site
            assert "run-health--danger" not in html, site
        # "Last scan" still reports the earlier COMPLETED scan, never the reclaimed run.
        assert "never" not in client.get(f"/collection/{holder['cid']}").text.split("Last scan")[1][:200]


def test_a_collection_whose_only_runs_are_interrupted_reports_no_completed_scan(cairn_env):
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-neverdone")
        now = _utcnow()
        await _add_run(
            cid, result="interrupted", started=now - timedelta(minutes=20),
            finished=now - timedelta(minutes=15),
        )
        await _add_run(
            cid, result="interrupted", started=now - timedelta(minutes=10),
            finished=now - timedelta(minutes=5),
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        detail = client.get(f"/collection/{holder['cid']}").text
        assert "no completed scans yet" in detail
        assert "a later scan was interrupted" in detail
        assert "run-health--danger" not in detail


def test_a_later_failed_run_is_rendered_as_a_failure(cairn_env):
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-error")
        now = _utcnow()
        await _add_run(
            cid, result="ok", started=now - timedelta(hours=2), finished=now - timedelta(hours=2)
        )
        await _add_run(
            cid, result="error", started=now - timedelta(minutes=10),
            finished=now - timedelta(minutes=9),
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        for site, html in _render_sites(client, holder["cid"]).items():
            assert "a later scan failed" in html, site
            assert "run-health--danger" in html, site
            assert "a later scan was interrupted" not in html, site


def test_a_partial_scan_followed_by_a_failure_states_both(cairn_env):
    """The combination the audit named: neither disclosure may erase the other."""
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-both")
        now = _utcnow()
        await _add_run(
            cid, result="partial", started=now - timedelta(hours=2),
            finished=now - timedelta(hours=2), errors=2, error_sample=PARTIAL_SAMPLE,
        )
        await _add_run(
            cid, result="error", started=now - timedelta(minutes=10),
            finished=now - timedelta(minutes=9),
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        for site, html in _render_sites(client, holder["cid"]).items():
            assert "2 files skipped" in html, site
            assert "a later scan failed" in html, site


def test_the_last_scan_tile_of_a_tripwire_collection_carries_the_note(cairn_env):
    """`ots_mode='none'` is the only mode with a "Last scan" tile — its sub-line reports it too."""
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-tile")
        now = _utcnow()
        await _add_run(
            cid, result="partial", started=now - timedelta(minutes=5), finished=now,
            errors=1, error_sample=PARTIAL_SAMPLE,
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        detail = client.get(f"/collection/{holder['cid']}").text
        # Once under the status pill in the meta strip, once in the tripwire "Last scan" tile.
        assert detail.count("1 file skipped") == 2


# --- 3.23 / 3.24 the verify card's retry affordance --------------------------------------------

BACKEND_MESSAGE = "SENTINEL-BACKEND-MESSAGE-must-not-be-rendered"


async def _seed_verify_file(root: Path, *, ots_state: str, sha256: str | None, status: str = "ok"):
    """One stamped file (or an unstamped one when `ots_state='none'`)."""
    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    cid = await seed_collection(root)
    now = _utcnow()
    async with get_sessionmaker()() as s:
        s.add(
            FileEntry(
                collection_id=cid,
                relpath="doc.txt",
                size=5,
                sha256=sha256,
                status=status,
                ots_state=ots_state,
                ots_path=(str(root.parent / "p" / "doc.txt.ots") if ots_state != "none" else None),
                ots_stamped_at=(now if ots_state != "none" else None),
                first_seen=now,
                last_checked=now,
            )
        )
        await s.commit()
    return cid


def _verify_client(cairn_env, *, ots_state="complete", sha256="d" * 64, status="ok"):
    root = cairn_env / "vault"
    root.mkdir(parents=True, exist_ok=True)
    (root / "doc.txt").write_text("hello")
    return _make_client(
        lambda: _seed_verify_file(root, ots_state=ots_state, sha256=sha256, status=status)
    )


def _result(**kw):
    from src.services.ots import VerifyResult

    kw.setdefault("verified", False)
    kw.setdefault("state", "complete")
    kw.setdefault("message", BACKEND_MESSAGE)
    return VerifyResult(**kw)


def _post_verify(client, result, monkeypatch) -> str:
    from src.services import ots as ots_svc

    monkeypatch.setattr(ots_svc, "verify", lambda *a, **k: result)
    token = _csrf_token(client)
    r = client.post("/verify", data={"csrf_token": token, "file_id": 1})
    assert r.status_code == 200, r.text
    return r.text


def _has_retry(html: str) -> bool:
    return "Retry" in html and 'hx-post="/verify"' in html


def test_a_transport_failure_names_the_reason_and_offers_a_retry(cairn_env, monkeypatch):
    with _verify_client(cairn_env) as client:
        html = _post_verify(
            client,
            _result(transport_error="explorer unreachable: connection refused"),
            monkeypatch,
        )
        assert "Couldn&#39;t check right now" in html or "Couldn't check right now" in html
        # The TYPED reason, so the operator can tell a DNS failure from a refused connection.
        assert "explorer unreachable: connection refused" in html
        assert _has_retry(html)
        assert '"file_id": 1' in html
        # …and never the backend's generic message under a reason-attributed verdict.
        assert BACKEND_MESSAGE not in html


def test_an_inconclusive_result_offers_a_retry(cairn_env, monkeypatch):
    with _verify_client(cairn_env) as client:
        html = _post_verify(client, _result(inconclusive=True), monkeypatch)
        assert _has_retry(html)
        assert BACKEND_MESSAGE not in html


@pytest.mark.parametrize(
    "case",
    [
        "never_stamped",
        "queued",
        "unreadable_proof",
        "mismatch_file",
        "mismatch_proof",
        "mismatch_proof_stale",
        "mismatch_unknown",
    ],
)
def test_a_settled_outcome_offers_no_retry_and_prints_no_backend_message(
    cairn_env, monkeypatch, case
):
    """A retry button beside a settled finding presents it as provisional. It is never offered."""
    live_sha = __import__("hashlib").sha256(b"hello").hexdigest()

    if case == "never_stamped":
        client = _verify_client(cairn_env, ots_state="none", sha256=live_sha)
        result = None
    elif case == "queued":
        client = _verify_client(cairn_env, ots_state="pending", sha256=live_sha)
        result = _result(state="pending")
    elif case == "unreadable_proof":
        client = _verify_client(cairn_env, sha256=live_sha)
        result = _result(unreadable_proof=True)
    elif case == "mismatch_file":
        # Live bytes no longer hash to the recorded baseline → the FILE moved.
        client = _verify_client(cairn_env, sha256="f" * 64)
        result = _result(digest_mismatch=True)
    elif case == "mismatch_proof_stale":
        # Live == baseline and a re-stamp is owed → the proof predates this version (legacy row).
        client = _verify_client(cairn_env, sha256=live_sha, status="modified")
        result = _result(digest_mismatch=True)
    elif case == "mismatch_proof":
        client = _verify_client(cairn_env, sha256=live_sha)
        result = _result(digest_mismatch=True)
    else:  # mismatch_unknown — no recorded baseline to blame either artifact with
        client = _verify_client(cairn_env, sha256=None)
        result = _result(digest_mismatch=True)

    with client:
        if result is None:
            from src.services import ots as ots_svc

            monkeypatch.setattr(ots_svc, "verify", lambda *a, **k: None)
            token = _csrf_token(client)
            html = client.post("/verify", data={"csrf_token": token, "file_id": 1}).text
        else:
            html = _post_verify(client, result, monkeypatch)

        assert not _has_retry(html), case
        assert BACKEND_MESSAGE not in html, case


def test_a_transport_failure_under_a_verified_verdict_still_discloses_its_reason(
    cairn_env, monkeypatch
):
    """Precedence decides the headline, not what the card may say (the disclosure note, D2)."""
    with _verify_client(cairn_env) as client:
        html = _post_verify(
            client,
            _result(
                verified=True,
                transport_error="explorer timeout",
                transport_failures=1,
                existed_by=_utcnow(),
                block_height=800000,
            ),
            monkeypatch,
        )
        assert "explorer timeout" in html
        assert "attestation lookup" in html
        # A verified proof is a settled answer: no retry, and no backend message.
        assert not _has_retry(html)
        assert BACKEND_MESSAGE not in html



# --- post-audit: the failure paths the audit found open ---------------------------------------
#
# All three findings are one shape: a surface that answers with something OTHER than a health
# verdict when its own dependencies fail, leaving the reader with the last verdict — which, on a
# monitor, is the one most likely to have been "fine".


def test_healthz_reports_error_when_the_freshness_read_fails_after_ping_succeeds(cairn_env):
    """MAJOR 1: the probe and the freshness read are two trips to the datastore.

    `ping()` answering does not mean the next query will. Before the fix, only a false `ping()` was
    converted into the structured 503; a session that could not be opened, or a freshness SELECT
    that raised, escaped as a bare HTTP 500 — a body the polling monitor cannot parse, with no
    `status`, no `mode` and no `version`, indistinguishable from a reverse proxy's own error page.
    """

    async def seed():
        await _new_collection(cairn_env / "hz-midway", cadence=900)

    with _make_client(seed) as client:
        from src.services import scheduler as scheduler_svc

        async def _boom(*a, **kw):
            raise RuntimeError("datastore went away mid-read")

        original = scheduler_svc.compute_health
        scheduler_svc.compute_health = _boom
        try:
            resp = client.get("/healthz")
        finally:
            scheduler_svc.compute_health = original

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "error"
        assert body["mode"] and body["version"]  # still parseable by the monitor
        assert "collections" not in body

        # …and it recovers rather than latching.
        assert client.get("/healthz").status_code == 200


def test_the_health_pill_fails_closed_after_a_successful_healthy_fill(cairn_env):
    """MAJOR 2 (server half): htmx does not swap a 5xx, so a raising poll must not raise.

    The sequence is the one that matters: a poll answers "Healthy", then the datastore goes away.
    Every later poll fails the same way, so a propagated error would leave that green verdict on
    screen indefinitely — a health claim outliving the health of the thing it describes.
    """

    async def seed():
        cid = await _new_collection(cairn_env / "pill-failclosed", cadence=900)
        now = _utcnow()
        await _add_run(cid, result="ok", started=now, finished=now)

    with _make_client(seed) as client:
        assert "Healthy" in client.get("/health-pill").text  # the verdict that must not survive

        from src.services import scheduler as scheduler_svc

        async def _boom(*a, **kw):
            raise RuntimeError("freshness read failed")

        original = scheduler_svc.compute_health
        scheduler_svc.compute_health = _boom
        try:
            resp = client.get("/health-pill")
        finally:
            scheduler_svc.compute_health = original

        # 200, deliberately: an error status would not be swapped in, which is the defect.
        assert resp.status_code == 200
        html = resp.text
        assert "Healthy" not in html
        assert "Health check failed" in html
        assert "is-error" in html  # non-green
        assert "var(--danger)" in html

        # It is not latched either: the next good poll states the real verdict again.
        assert "Healthy" in client.get("/health-pill").text


def test_the_health_pill_carries_client_side_error_hooks_for_what_the_route_cannot_catch(
    cairn_env,
):
    """MAJOR 2 (client half): a failing dependency, a timeout or a dropped connection never reaches
    the route handler, so the fragment carries htmx error hooks and the shell defines what they do.

    Asserted by presence and shape — this suite has no browser — but the shape is the contract: the
    hooks name the three htmx failure events, and the handler they call replaces the label with the
    failed state rather than merely styling it.
    """

    async def seed():
        cid = await _new_collection(cairn_env / "pill-hooks", cadence=900)
        now = _utcnow()
        await _add_run(cid, result="ok", started=now, finished=now)

    with _make_client(seed) as client:
        for html in (client.get("/").text, client.get("/health-pill").text):
            # On the element itself, so a swapped-in replacement carries them too.
            assert 'hx-on::response-error="window.cairnHealthPillFailed(this)"' in html
            assert 'hx-on::send-error="window.cairnHealthPillFailed(this)"' in html
            assert 'hx-on::timeout="window.cairnHealthPillFailed(this)"' in html

        page = client.get("/").text
        assert "window.cairnHealthPillFailed = function" in page
        assert 'label.textContent = "Health check failed"' in page
        assert 'el.classList.add("is-error")' in page
        # The delegated listener covers the same events for a pill whose attributes were lost.
        assert '"htmx:responseError", "htmx:sendError", "htmx:timeout"' in page


def test_the_exact_abandonment_boundary_reads_the_same_to_health_and_to_both_reclaimers(
    cairn_env, monkeypatch
):
    """MINOR: one predicate, frozen clock, heartbeat EXACTLY one interval old.

    Health used to call that heartbeat live (`age <= timeout`) while both reclaimers called it
    abandoned (`heartbeat <= cutoff`), so at the boundary a claim could be reclaimed out from under
    a collection the dead-man's switch was still calling fresh. The boundary is now abandoned
    everywhere.
    """

    async def go():
        from src.services import collections as collections_svc
        from src.services import scheduler as scheduler_svc
        from src.services.collections import (
            RUN_HEARTBEAT_TIMEOUT_SECONDS,
            claim_is_live,
            heartbeat_cutoff,
        )
        from src.database import get_sessionmaker
        from src.models.db import Run

        cid = await _new_collection(cairn_env / "boundary", cadence=900)
        await _age_collection(cid, seconds=6 * 3600)  # past the startup grace
        frozen = _utcnow()
        on_the_line = frozen - timedelta(seconds=RUN_HEARTBEAT_TIMEOUT_SECONDS)
        run_id = await _add_run(
            cid, result="running", started=on_the_line, heartbeat=on_the_line
        )

        # Freeze both clocks so the boundary is exact rather than "a few microseconds past".
        monkeypatch.setattr(scheduler_svc, "_utcnow", lambda: frozen)
        monkeypatch.setattr(collections_svc, "utcnow", lambda: frozen)

        async with get_sessionmaker()() as s:
            run = await s.get(Run, run_id)
            # The predicate itself, and the SQL clause the reclaimers use, at the same instant.
            assert claim_is_live(run, frozen) is False
            stale_id = await collections_svc._stale_claim_id(
                s, cid, heartbeat_cutoff(frozen)
            )
            assert stale_id == run_id

        # The switch agrees: no freshness from an abandoned claim, at the boundary included.
        report = await _health()
        assert report.collections[0].state == "stale"
        assert report.status == "degraded"

        # …and the claim is genuinely reclaimable at that same instant, so the two never disagree.
        assert await collections_svc.reclaim_stale_claim(cid) is True

    asyncio.run(go())


def test_a_legacy_partial_run_with_no_recorded_count_still_says_files_were_skipped(cairn_env):
    """Verifier concern (a): a `partial` row written before migration 0012 has `errors = 0`.

    The zero means "this column did not exist when the run happened", not "nothing was skipped" —
    and `partial` is written by exactly one thing. Gating the note on the count rendered those rows
    as a clean scan. It now says what is known and admits what is not; the next scan replaces it
    with a real count.
    """
    holder = {}

    async def seed():
        cid = await _new_collection(cairn_env / "rh-legacy-partial")
        now = _utcnow()
        await _add_run(
            cid, result="partial", started=now - timedelta(minutes=5), finished=now,
            errors=0, error_sample=None,
        )
        holder["cid"] = cid

    with _make_client(seed) as client:
        for site, html in _render_sites(client, holder["cid"]).items():
            assert "partial" in html, site
            assert "files skipped (count not recorded)" in html, site
            assert "run-health--warn" in html, site
            # No fabricated number, and no sample it does not have.
            assert "0 files skipped" not in html, site
            assert "What was skipped" not in html, site


def test_health_answers_a_collections_three_freshness_legs_in_one_statement(cairn_env):
    """Round-2 MINOR: three reads are three snapshots, and a scan can commit between them.

    Python's `sqlite3` runs in legacy transaction mode — a SELECT opens no transaction — so the
    completed-run read, the running-run read and the any-scan-exists read each saw the database at
    a different instant. A first scan that committed between the first two was seen by NEITHER (no
    completed run when the first ran, no `running` row left when the second did), so a collection
    that had just finished scanning read `stale` and `/healthz` answered 503 for one poll: a false
    alarm manufactured by the reader, on the one surface whose false alarms teach an operator to
    stop reading it.

    The interleaving seam is gone rather than patched, so what is asserted is the property that
    removed it — one statement per collection, the way `test_the_fleet_page_never_reads_events_a_second_time`
    asserts the fleet page's single read. The count is also the loop budget: `_attach_health_state`
    calls this per render, so one statement per collection must stay one.
    """

    async def go():
        from sqlalchemy import event

        from src.config import get_settings
        from src.database import get_engine, get_sessionmaker
        from src.services.scheduler import compute_health

        one = await _new_collection(cairn_env / "onestmt-a", cadence=900)
        two = await _new_collection(cairn_env / "onestmt-b", cadence=900)
        now = _utcnow()
        # Every leg has something to find, so no leg can be "one statement" by being skipped:
        # a completed scan, a live in-flight scan, and a non-scan run the freshness read ignores.
        await _add_run(one, result="ok", started=now - timedelta(minutes=10), finished=now)
        await _add_run(one, result="running", started=now - timedelta(minutes=1), heartbeat=now)
        await _add_run(one, result="ok", started=now - timedelta(minutes=5), finished=now,
                       kind="upgrade")
        await _add_run(two, result="ok", started=now - timedelta(minutes=10), finished=now)

        statements: list[str] = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        engine = get_engine()
        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            async with get_sessionmaker()() as s:
                report = await compute_health(s, get_settings())
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)

        # The verdict is unchanged by the rewrite: both collections fresh on their completed scan.
        assert [r.state for r in report.collections] == ["fresh", "fresh"]

        runs_reads = [q for q in statements if "FROM runs" in q]
        assert len(runs_reads) == 2, runs_reads  # exactly one per collection, never per leg
        # …and that one statement really does answer all three legs, rather than two of them
        # while a third read hid somewhere else.
        assert all(
            "max(" in q.lower() and "count(" in q.lower() and "heartbeat_at" in q
            for q in runs_reads
        ), runs_reads

    asyncio.run(go())
