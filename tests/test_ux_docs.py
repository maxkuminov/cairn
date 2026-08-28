"""UX audit sprint 1, slice D — /learn's verification instructions, the Verification settings tab
and the mobile rules.

These are copy and CSS assertions, so they are deliberately worded against *meaning* rather than
exact sentences: what /learn must not stop saying is that an auditor needs both halves of a proof,
that the command-line route needs a Bitcoin node, and that the two pre-confirmation proof states
have two different names. The Settings tab must not re-grow a control for a setting the panel
cannot persist.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from tests.conftest import seed_collection

CSS_PATH = Path(__file__).resolve().parents[1] / "src/control_panel/static/css/panel.css"


def _make_client(cairn_env, seed_coro):
    """Seed on a throwaway loop, drop the engine, return a TestClient (mirrors tests/test_panel)."""
    from fastapi.testclient import TestClient

    from src import database
    from src.main import app

    asyncio.run(seed_coro())
    database.reset_engine()  # rebuild on TestClient's loop (avoids cross-loop aiosqlite warning)
    return TestClient(app)


def _client(cairn_env):
    root = cairn_env / "docs-collection"
    root.mkdir()

    async def seed():
        await seed_collection(root, ots_mode="perfile")

    return _make_client(cairn_env, seed)


# --- /learn ----------------------------------------------------------------------------------


def test_learn_leads_with_the_public_verifier_and_needs_both_halves(cairn_env):
    """#26: the drag-and-drop route first, and an auditor needs the file *and* the .ots.

    The panel's export route serves only the proof, so a reader told to "download the proof and
    verify it" is one step short of being able to.
    """
    with _client(cairn_env) as client:
        r = client.get("/learn")

    assert r.status_code == 200
    body = r.text
    assert "opentimestamps.org" in body
    lower = body.lower()
    # Both halves are named as jointly required, not just mentioned somewhere on the page.
    assert "both halves" in lower
    assert "drag" in lower and ".ots" in body
    # The verifier bullet comes before the command-line one.
    assert lower.index("opentimestamps.org") < lower.index("ots verify")


def test_learn_says_the_cli_path_needs_a_bitcoin_node_and_never_suggests_no_bitcoin(cairn_env):
    """#26 / #12's rejected fix 4.

    `ots verify` can only check against a Bitcoin Core node; `ots --no-bitcoin verify` exits 1
    having verified nothing, which is worse than the visible error. It must never be documented.
    """
    with _client(cairn_env) as client:
        body = client.get("/learn").text

    lower = body.lower()
    assert "bitcoin core node" in lower
    # The node requirement is attached to the command-line route, not floated loose on the page.
    assert lower.index("ots verify") < lower.index("bitcoin core node")
    # And the reason Cairn itself defaults elsewhere is stated.
    assert "explorer" in lower
    assert "--no-bitcoin" not in body


def test_learn_names_all_three_proof_states_distinctly(cairn_env):
    """#23 / design D13: queued-to-stamp and pending-confirmation are two states, two names."""
    with _client(cairn_env) as client:
        body = client.get("/learn").text

    lower = body.lower()
    assert "queued to stamp" in lower
    assert "pending confirmation" in lower
    assert "anchored" in lower
    # The queued state is introduced before the submitted one, and is never called "pending
    # confirmation" itself: nothing is waiting on Bitcoin until it has been submitted.
    assert lower.index("queued to stamp") < lower.index("pending confirmation")


# --- Settings -> Verification ----------------------------------------------------------------


def test_verify_tab_has_no_clickable_backend_cards(cairn_env):
    """#34: the backend is env-only, so the tab may not render a control that cannot save."""
    with _client(cairn_env) as client:
        r = client.get("/settings?tab=verify")

    assert r.status_code == 200
    assert "radio-card" not in r.text


def test_verify_tab_names_the_env_vars_and_the_restart(cairn_env):
    with _client(cairn_env) as client:
        r = client.get("/settings?tab=verify")

    body = r.text
    assert "CAIRN_VERIFY_BACKEND" in body
    assert "CAIRN_NODE_RPC_URL" in body
    assert "restart" in body.lower()
    # The active backend is marked (explorer is the default in the test environment).
    assert "In use" in body


def test_verify_tab_reports_an_unset_node_rpc_url(cairn_env):
    """An unset RPC address must say so rather than render a blank the reader has to interpret."""
    with _client(cairn_env) as client:
        body = client.get("/settings?tab=verify").text

    assert "not set" in body.lower()


def test_verify_tab_shows_a_configured_node_rpc_url(cairn_env, monkeypatch):
    monkeypatch.setenv("CAIRN_NODE_RPC_URL", "http://bitcoin.lan:8332")

    from src.config import get_settings

    get_settings.cache_clear()
    try:
        with _client(cairn_env) as client:
            body = client.get("/settings?tab=verify").text
        assert "http://bitcoin.lan:8332" in body
    finally:
        get_settings.cache_clear()


# --- mobile CSS (#33) --------------------------------------------------------------------------


def _mobile_blocks() -> list[str]:
    """Return the bodies of every `@media (max-width: 768px)` block in panel.css."""
    css = CSS_PATH.read_text()
    blocks: list[str] = []
    marker = "@media (max-width: 768px) {"
    idx = css.find(marker)
    while idx != -1:
        depth = 0
        i = idx + len(marker) - 1
        start = i + 1
        depth = 1
        while depth and i + 1 < len(css):
            i += 1
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
        blocks.append(css[start:i])
        idx = css.find(marker, i)
    return blocks


def test_op_bar_is_hidden_on_small_viewports():
    """The progress bar squeezed the collection name to zero width on a 320px row."""
    assert any(".op-bar { display: none; }" in b for b in _mobile_blocks())


def test_status_meta_cell_takes_a_full_row_and_stops_ellipsizing():
    blocks = _mobile_blocks()
    assert any(".meta-cell--wide { flex: 1 1 100%; }" in b for b in blocks)
    assert any(
        ".meta-cell--wide .meta-cell__value" in b and "white-space: normal" in b for b in blocks
    )


# --- live UX pass: the mobile findings the first mobile slice only half-fixed ------------------


def _media_blocks(max_width: int) -> list[str]:
    """Bodies of every `@media (max-width: Npx)` block in panel.css."""
    css = CSS_PATH.read_text()
    blocks: list[str] = []
    marker = f"@media (max-width: {max_width}px) {{"
    idx = css.find(marker)
    while idx != -1:
        i = idx + len(marker) - 1
        start = i + 1
        depth = 1
        while depth and i + 1 < len(css):
            i += 1
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
        blocks.append(css[start:i])
        idx = css.find(marker, i)
    return blocks


def test_the_collection_card_badge_takes_a_row_of_its_own_so_the_name_keeps_its_line():
    """M3 / #33: hiding the progress bar was only half the fix.

    The badge is still ~269px (deep-scan variant) and shares a `space-between` flex row with the
    collection NAME, so flex resolved the overflow by shrinking the name — the only item with
    `min-width: 0` — to ~23px. A nowrap badge cannot be negotiated down, so it gets its own row.
    """
    blocks = _media_blocks(768)
    assert any(".collection-card__title-row { flex-wrap: wrap; }" in b for b in blocks)
    badge_rule = [
        b for b in blocks if ".collection-card__title-row > .op-status" in b
    ]
    assert badge_rule, "the card's op-status is not given a row of its own under the breakpoint"
    rule = badge_rule[0]
    assert "flex: 0 0 100%" in rule  # a full-width basis is what forces the wrap
    assert "order: 2" in rule
    # …and the name block owns the line above it.
    assert any(".collection-card__title-row > :first-child { flex: 1 1 100%;" in b for b in blocks)


def test_the_deep_tag_is_dropped_from_the_card_badge_at_phone_width():
    """Even on its own row the deep-scan badge overruns a 320px card's ~252px content box."""
    blocks = _media_blocks(480)
    assert any(
        ".collection-card__title-row .op-badge__deep { display: none; }" in b for b in blocks
    )


def test_the_bulk_review_button_may_wrap_instead_of_overflowing_the_rail_card():
    """#7: a nowrap button wider than its 390px card pushed the card off the right edge."""
    blocks = _media_blocks(768)
    assert any("#open-events-pill .btn" in b and "white-space: normal" in b for b in blocks)
    assert any("#open-events-pill { flex-wrap: wrap;" in b for b in blocks)
    assert any(".row-between { flex-wrap: wrap;" in b for b in blocks)


def test_the_mono_root_path_cell_wraps_rather_than_clipping_on_a_phone():
    """#13: the ellipsis ate the whole path, and touch has no hover to recover a `title` with."""
    blocks = _media_blocks(768)
    assert any(
        ".meta-cell__value.mono" in b and "white-space: normal" in b and "overflow-wrap" in b
        for b in blocks
    )


def test_the_root_path_cell_carries_a_title_for_the_pointer_case(cairn_env):
    with _client(cairn_env) as client:
        body = client.get("/collection/1").text

    assert 'class="meta-cell__value mono" style="font-size:12px" title=' in body


def test_the_panel_ships_an_inline_favicon_so_no_request_404s(cairn_env):
    """#17: every page load logged a /favicon.ico 404 in the browser console."""
    with _client(cairn_env) as client:
        body = client.get("/").text

    assert 'rel="icon"' in body
    assert "data:image/svg+xml" in body


async def _seed_file(cid: int, *, status: str) -> None:
    from datetime import datetime, timezone

    from src.database import get_sessionmaker
    from src.models.db import FileEntry

    now = datetime.now(timezone.utc)
    async with get_sessionmaker()() as s:
        s.add(FileEntry(
            collection_id=cid, relpath="a/b.txt", size=4, sha256="e" * 64,
            status=status, ots_state="none", first_seen=now, last_checked=now,
        ))
        await s.commit()


def test_the_card_legend_reports_the_baseline_comparison_not_a_notary_verdict(cairn_env):
    """#9: "verified" is the notary's word for a proof checked against Bitcoin. A scan compares
    against the recorded baseline and confirms no proof at all."""
    root = cairn_env / "clean-collection"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_file(cid, status="ok")

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text

    assert "All files verified" not in body
    assert "matching baseline" in body.lower()


def test_the_alert_pills_say_unreviewed_rather_than_need_action(cairn_env):
    """#12: "need action" overstated it — clearing the pill is a reading log, not a repair."""
    async def seed():
        from datetime import datetime, timezone

        from src.database import get_sessionmaker
        from src.models.db import Event

        root = cairn_env / "pill-collection"
        root.mkdir()
        cid = await seed_collection(root)
        async with get_sessionmaker()() as s:
            s.add(Event(collection_id=cid, kind="missing", detected_at=datetime.now(timezone.utc)))
            await s.commit()

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text

    assert "unreviewed" in body
    assert "need action" not in body


def test_the_keyboard_activated_issue_count_answers_space_as_well_as_enter(cairn_env):
    """#19: a `role="link"` must respond to both activation keys, not only Enter."""
    root = cairn_env / "issue-collection"
    root.mkdir()

    async def seed():
        cid = await seed_collection(root)
        await _seed_file(cid, status="missing")

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text

    assert 'role="link"' in body
    assert "event.key===' '" in body


# --- live-pass fixes: C1 (digest line) and W6 (dead log-out control) ---------------------------


def _rule_body(selector: str) -> str:
    """Body of the last top-level `selector { ... }` rule in panel.css."""
    css = CSS_PATH.read_text()
    marker = f"{selector} {{"
    idx = css.rfind(marker)
    assert idx != -1, f"panel.css declares no `{selector}` rule"
    start = idx + len(marker)
    end = css.index("}", start)
    return css[start:end]


def test_the_changed_restore_digest_line_wraps_instead_of_ellipsizing():
    """C1: "recorded <64hex> → found <64hex>" is 146 characters and the only surviving record of
    the pre-restore digest. On `.event-row__relpath` (nowrap + ellipsis) the second digest never
    reached the screen, so it gets a line that wraps at any width."""
    body = _rule_body(".event-row__detail")
    assert "white-space: normal" in body
    assert "overflow-wrap: anywhere" in body
    assert "nowrap" not in body, "the digest line must never clip"
    assert "text-overflow" not in body, "the digest line must never ellipsize"
    # It is still a hash line: monospace, muted, and small enough not to shout over the path.
    assert "var(--font-mono)" in body
    assert "var(--text-2)" in body


def test_single_mode_chrome_offers_no_log_out(cairn_env):
    """W6: single mode has no login wall, so a "Log out" anchor is a control that cannot work.
    The theme toggle stays, and carries an aria-label matching its title."""
    root = cairn_env / "chrome-collection"
    root.mkdir()

    async def seed():
        await seed_collection(root)

    with _make_client(cairn_env, seed) as client:
        body = client.get("/").text

    assert "Log out" not in body, "a dead control teaches the operator that buttons are decorative"
    assert 'href="/mode/toggle"' in body, "the theme toggle is not part of the auth chrome"
    assert 'aria-label="Switch to light mode"' in body or (
        'aria-label="Switch to dark mode"' in body
    ), "the icon-only theme toggle needs a label a screen reader can announce"


def test_the_fleet_group_header_wraps_rather_than_clipping_on_a_phone():
    """Live-pass W1 on add-fleet-review-and-run-health: at 390px the legend row measured 452px
    inside an overflow-hidden card, cutting the per-group CTA mid-label."""
    joined = "\n".join(_media_blocks(640))
    assert ".review-list__legend" in joined
    assert "flex-wrap: wrap" in joined
