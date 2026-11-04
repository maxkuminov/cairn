"""`/mode/toggle` never redirects to another origin, in `single` mode too (oidc-sso-login B.4a).

The `oidc` half lives in `tests/test_oidc_login.py`. The local-path rule is ONE function
(`src.auth.wall.safe_local_path`), used by `next`, the callback redirect and the toggle.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def single_client(cairn_env):
    from fastapi.testclient import TestClient

    from src import database
    from src.main import create_app

    database.reset_engine()
    with TestClient(create_app()) as client:
        yield client
    database.reset_engine()


@pytest.mark.parametrize("referer,expected", [
    ("https://evil.example/phish", "/phish"),
    ("https://evil.example/phish?x=1#frag", "/phish?x=1"),
    ("//evil.example/x", "/"),
    ("/\\evil.example/x", "/"),
    ("\\\\evil.example\\x", "/"),
    ("https://evil.example", "/"),
    ("https://evil.example//other.example/x", "/"),
    ("javascript:alert(1)", "/"),
    ("/collections\r\nSet-Cookie: x=1", "/"),
])
def test_cross_origin_referer_single(single_client, referer, expected):
    r = single_client.get("/mode/toggle", headers={"Referer": referer}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == expected
    assert "evil.example" not in r.headers["location"]


def test_same_site_panel_page_single(single_client):
    r = single_client.get("/mode/toggle", headers={"Referer": "http://testserver/collections"},
                          follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/collections"
    assert r.headers["set-cookie"] == "cairn_mode=dark; Max-Age=31536000; Path=/; SameSite=lax"


def test_one_local_path_rule():
    from src.auth import routes, wall
    from src.control_panel import routes as panel

    assert panel.local_target_from_referer is wall.local_target_from_referer
    assert routes.safe_local_path is wall.safe_local_path
    for good in ("/", "/collections", "/collection/1?view=tree"):
        assert wall.safe_local_path(good) == good
    for bad in ("", "x", "//h", "/\\h", "\\/h", "https://h/", "/a b", "/\t/h", None, 5):
        assert wall.safe_local_path(bad) is None
