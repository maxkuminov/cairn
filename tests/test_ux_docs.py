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
