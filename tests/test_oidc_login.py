"""The `oidc` login wall, flow, sessions and attribution (oidc-sso-login B.3–B.7).

Every scenario of the `oidc-login` capability against a local fake provider
(``tests/oidc_fake.py``: keys generated here, never committed; no network). The app is built with
``create_app()`` after the environment is switched to `oidc`, and driven over
``https://testserver`` so the `Secure` session cookie is sent back.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest

from tests.conftest import seed_collection
from tests.oidc_fake import (
    AUTH_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    CODE_MARKER,
    DISCOVERY_URL,
    ISSUER,
    JWKS_URL,
    LINKED_SUB,
    PUBLIC_URL,
    SECRET_KEY,
    TOKEN_URL,
    FakeProvider,
    make_code,
    oidc_env,
)

HTML = {"Accept": "text/html,application/xhtml+xml"}


# --------------------------------------------------------------------------------------------
# fixtures + helpers
# --------------------------------------------------------------------------------------------


def _cli(*argv: str) -> int:
    from src.cli import main

    return main(list(argv))


def _db(tmp_path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "db" / "cairn.db")


@pytest.fixture
def oidc(cairn_env, monkeypatch, capsys):
    import src.auth.oidc as oidc_mod
    from src import database
    from src.config import get_settings

    for k, v in oidc_env().items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    database.reset_engine()
    provider = FakeProvider()
    monkeypatch.setattr(oidc_mod, "transport_factory", provider.transport)

    # Bootstrap the implicit owner and link it, exactly as an operator would.
    assert _cli("users", "link", "--subject", LINKED_SUB) == 0
    capsys.readouterr()
    database.reset_engine()

    from fastapi.testclient import TestClient

    from src.main import create_app

    app = create_app()
    client = TestClient(app, base_url="https://testserver")
    yield SimpleNamespace(app=app, client=client, provider=provider, tmp=cairn_env,
                          new_client=lambda: TestClient(app, base_url="https://testserver"))
    client.close()
    access = logging.getLogger("uvicorn.access")
    for f in list(access.filters):
        access.removeFilter(f)
    database.reset_engine()


def _csrf(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, "no csrf token on the page"
    return m.group(1)


def start(client, next_path: str | None = None, headers: dict | None = None):
    url = "/login" + (f"?next={quote(next_path, safe='')}" if next_path else "")
    page = client.get(url)
    assert page.status_code == 200
    data = {"csrf_token": _csrf(page.text)}
    if next_path is not None:
        data["next"] = next_path
    resp = client.post("/auth/oidc/start", data=data, headers=headers or {},
                       follow_redirects=False)
    return resp


def start_params(resp) -> dict[str, str]:
    assert resp.status_code == 303, resp.text
    loc = resp.headers["location"]
    assert loc.startswith(AUTH_URL + "?"), loc
    return {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


def callback(client, params: dict):
    return client.get("/auth/oidc/callback", params=params, follow_redirects=False)


def login(c, sub: str = LINKED_SUB, next_path: str | None = None, client=None,
          iss: str | None = ISSUER):
    client = client or c.client
    q = start_params(start(client, next_path))
    params = {"code": make_code(q["nonce"], sub), "state": q["state"]}
    if iss is not None:
        params["iss"] = iss
    return callback(client, params), q


def assert_failed(resp):
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?error=failed"
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def signed_in(client) -> bool:
    r = client.get("/", headers=HTML, follow_redirects=False)
    return r.status_code == 200


# --------------------------------------------------------------------------------------------
# the wall
# --------------------------------------------------------------------------------------------


def test_anonymous_dashboard_redirects_to_login(oidc):
    r = oidc.client.get("/", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/"
    assert "fixture" not in r.text and "<html" not in r.text


def test_next_carries_path_and_query(oidc):
    r = oidc.client.get("/collection/1?view=tree", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert parse_qs(urlsplit(loc).query)["next"] == ["/collection/1?view=tree"]


def test_healthz_public_reports_oidc(oidc):
    r = oidc.client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "oidc" and body["status"] == "ok"


def test_htmx_and_api_refusal_shapes(oidc):
    r = oidc.client.get("/collection/1/files", headers={"HX-Request": "true"},
                        follow_redirects=False)
    assert r.status_code == 401 and r.headers["hx-redirect"] == "/login"
    r = oidc.client.get("/api/anything", follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    r = oidc.client.post("/scan", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_every_non_allowlisted_route_is_walled(oidc):
    """Enumerate `app.routes`: every route not on the public allowlist refuses without a session."""
    from starlette.routing import Mount

    from src.auth.wall import PUBLIC_PATHS, is_public

    def flatten(routes):
        # FastAPI >= 0.16x keeps an included router as one `_IncludedRouter` entry (no prefix is
        # used by Cairn); older releases copy its routes in. Walk both shapes.
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from flatten(inner.routes)
            else:
                yield route

    anon = oidc.new_client()
    seen_public = set()
    checked = 0
    for route in flatten(oidc.app.routes):
        if isinstance(route, Mount):
            assert route.path == "/static"
            assert is_public("/static/css/panel.css")
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        if is_public(path):
            seen_public.add(path)
            continue
        for method in sorted(route.methods - {"HEAD"}):
            r = anon.request(method, path, headers=HTML, follow_redirects=False)
            assert r.status_code in (303, 401), (method, path, r.status_code)
            if r.status_code == 303:
                assert r.headers["location"].startswith("/login"), (method, path)
            assert "set-cookie" not in r.headers or "cairn_mode" not in r.headers["set-cookie"]
            checked += 1
    assert checked > 30
    # Every allowlisted path is a real route (no dead allowlist entries).
    assert seen_public == set(PUBLIC_PATHS)


def test_a_new_route_is_walled_by_default(oidc):
    @oidc.app.get("/brand-new-route")
    async def _new():  # pragma: no cover - must never run without a session
        return {"leak": True}

    r = oidc.new_client().get("/brand-new-route", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


@pytest.mark.parametrize("bad", ["//evil.example.com/", "https://evil.example.com/",
                                 "/\\evil.example.com", "/\t/evil.example.com", "javascript:x"])
def test_open_redirect_via_next_refused(oidc, bad):
    r, _ = login(oidc, next_path=bad)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_returns_to_safe_next(oidc):
    r, _ = login(oidc, next_path="/collections?x=1")
    assert r.headers["location"] == "/collections?x=1"


def test_login_page_has_the_sso_button_and_no_password(oidc):
    from src.auth.ui import SSO_BUTTON_LABEL

    r = oidc.client.get("/login?next=/collections")
    assert r.status_code == 200
    assert r.text.count(SSO_BUTTON_LABEL) == 1
    assert 'type="password"' not in r.text
    assert 'name="next" value="/collections"' in r.text
    assert oidc.client.get("/login?next=//evil.example").text.count('name="next"') == 0


# --------------------------------------------------------------------------------------------
# theme toggle (B.4a), oidc side; the single side is in test_oidc_mode_toggle_single
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("referer,expected", [
    ("https://evil.example/phish", "/phish"),
    ("//evil.example/x", "/"),
    ("/\\evil.example/x", "/"),
    ("https://evil.example\\@good/x", "/"),
    (None, "/"),
])
def test_mode_toggle_never_leaves_origin_oidc(oidc, referer, expected):
    headers = {"Referer": referer} if referer else {}
    r = oidc.client.get("/mode/toggle", headers=headers, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == expected


def test_mode_toggle_signed_in_returns_to_panel_page(oidc):
    login(oidc)
    r = oidc.client.get("/mode/toggle", headers={"Referer": "https://testserver/collections"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/collections"
    assert r.headers["set-cookie"].startswith("cairn_mode=dark; Max-Age=31536000; Path=/; SameSite=lax")


# --------------------------------------------------------------------------------------------
# start
# --------------------------------------------------------------------------------------------


def test_start_redirects_to_provider_with_pkce(oidc):
    q = start_params(start(oidc.client))
    assert q["response_type"] == "code"
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == PUBLIC_URL + "/auth/oidc/callback"
    assert q["scope"] == "openid"
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 43 and len(q["nonce"]) >= 43
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", q["code_challenge"])


def test_forwarded_host_does_not_change_redirect_uri(oidc):
    q = start_params(start(oidc.client, headers={"X-Forwarded-Host": "attacker.example.com",
                                                 "Host": "attacker.example.com"}))
    assert q["redirect_uri"] == PUBLIC_URL + "/auth/oidc/callback"


def test_start_requires_csrf(oidc):
    oidc.client.get("/login")
    r = oidc.client.post("/auth/oidc/start", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert oidc.provider.count(DISCOVERY_URL) == 0


def test_verifier_matches_challenge_and_basic_auth(oidc):
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    r, q = login(oidc)
    assert r.headers["location"] == "/"
    form = oidc.provider.token_forms[-1]
    assert create_s256_code_challenge(form["code_verifier"]) == q["code_challenge"]
    assert form["redirect_uri"] == PUBLIC_URL + "/auth/oidc/callback"
    assert form["_authorization"].startswith("Basic ")
    assert "client_secret" not in form


def test_client_secret_post(oidc, monkeypatch):
    oidc.app.state.oidc.settings.oidc_token_auth_method = "client_secret_post"
    r, _ = login(oidc)
    assert r.headers["location"] == "/"
    form = oidc.provider.token_forms[-1]
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == CLIENT_SECRET
    assert form["_authorization"] is None


def test_unsupported_auth_method_refuses_with_config(oidc, caplog):
    oidc.provider.discovery_extra["token_endpoint_auth_methods_supported"] = ["private_key_jwt"]
    with caplog.at_level(logging.INFO, logger="cairn.auth"):
        r = start(oidc.client)
    assert r.headers["location"] == "/login?error=failed"
    assert "reason=config" in caplog.text


# --------------------------------------------------------------------------------------------
# callback: state, iss, transport
# --------------------------------------------------------------------------------------------


def test_success_session_and_cookie(oidc):
    r, _ = login(oidc)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    cookie = r.headers["set-cookie"]
    assert "secure" in cookie.lower() and "samesite=lax" in cookie.lower()
    assert "Max-Age=43200" in cookie
    assert signed_in(oidc.client)


def test_session_holds_no_subject_or_token(oidc):
    import base64
    import json

    login(oidc)
    raw = oidc.client.cookies.get("session")
    payload = json.loads(base64.b64decode(raw.split(".")[0] + "=="))
    assert set(payload) == {"uid", "epoch", "auth", "login_at", "csrf_nonce"}
    assert payload["auth"] == "oidc"
    assert LINKED_SUB not in json.dumps(payload)


def test_replayed_state_refused(oidc):
    q = start_params(start(oidc.client))
    params = {"code": make_code(q["nonce"]), "state": q["state"], "iss": ISSUER}
    assert callback(oidc.client, params).headers["location"] == "/"
    other = oidc.new_client()
    # Same browser replay (state consumed and unbound) and another browser replay.
    assert_failed(callback(oidc.client, params))
    assert_failed(callback(other, params))


def test_cross_browser_state_refused(oidc):
    victim, attacker = oidc.new_client(), oidc.new_client()
    q = start_params(start(victim))
    start(attacker)  # the attacker has a session with its own state
    r = callback(attacker, {"code": make_code(q["nonce"]), "state": q["state"], "iss": ISSUER})
    assert_failed(r)
    assert not signed_in(attacker)
    # The victim's own transaction was not consumed by the attacker's attempt.
    r = callback(victim, {"code": make_code(q["nonce"]), "state": q["state"], "iss": ISSUER})
    assert r.headers["location"] == "/"


def test_unknown_and_expired_state_refused(oidc):
    q = start_params(start(oidc.client))
    assert_failed(callback(oidc.client, {"code": "x", "state": "unknown", "iss": ISSUER}))
    q = start_params(start(oidc.client))
    for txn in oidc.app.state.oidc.transactions._items.values():
        txn.expires = 0.0
    assert_failed(callback(oidc.client, {"code": make_code(q["nonce"]), "state": q["state"],
                                         "iss": ISSUER}))
    assert not signed_in(oidc.client)


@pytest.mark.parametrize("iss", [None, "https://other.example.com/realms/example"])
def test_iss_parameter_missing_or_mismatched_refused_before_exchange(oidc, iss):
    r, _ = login(oidc, iss=iss)
    assert_failed(r)
    assert oidc.provider.count(TOKEN_URL) == 0


def test_iss_parameter_optional_when_not_advertised_but_checked_when_present(oidc):
    oidc.provider.discovery_extra["authorization_response_iss_parameter_supported"] = None
    r, _ = login(oidc, iss=None)
    assert r.headers["location"] == "/"
    c2 = oidc.new_client()
    r, _ = login(oidc, iss="https://other.example.com/x", client=c2)
    assert_failed(r)


def test_provider_error_parameter_fails(oidc):
    q = start_params(start(oidc.client))
    r = callback(oidc.client, {"error": "access_denied", "state": q["state"], "iss": ISSUER})
    assert_failed(r)


@pytest.mark.parametrize("target", [DISCOVERY_URL, JWKS_URL, TOKEN_URL])
@pytest.mark.parametrize("kind", ["oversized", "redirect", "oversized-no-length"])
def test_oversized_or_redirecting_provider_response_refused(oidc, target, kind):
    if kind == "redirect":
        resp = httpx.Response(302, headers={"Location": "https://idp.example.com/elsewhere"})
    elif kind == "oversized":
        resp = httpx.Response(200, content=b'{"x":"' + b"a" * (128 * 1024 + 10) + b'"}',
                              headers={"Content-Type": "application/json"})
    else:
        class Chunks(httpx.AsyncByteStream):  # no Content-Length: the cap must bite mid-read
            async def __aiter__(self):
                yield b'{"x":"'
                for _ in range(200):
                    yield b"a" * 1024
                yield b'"}'

        resp = httpx.Response(200, stream=Chunks())
    oidc.provider.overrides[target] = resp
    if target == TOKEN_URL:
        r, _ = login(oidc)
        assert_failed(r)
    else:
        r = start(oidc.client)
        assert r.status_code == 303 and r.headers["location"] == "/login?error=failed"
        assert oidc.app.state.oidc._provider is None  # nothing cached
    assert not signed_in(oidc.client)


def test_discovery_issuer_mismatch_and_http_endpoint_refused(oidc):
    oidc.provider.discovery_extra["issuer"] = ISSUER + "/"
    assert start(oidc.client).headers["location"] == "/login?error=failed"
    oidc.provider.discovery_extra = {"token_endpoint": "http://idp.example.com/token"}
    assert start(oidc.client).headers["location"] == "/login?error=failed"
    assert oidc.app.state.oidc._provider is None


def test_transport_disables_redirects_and_env_proxies(oidc, monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    start(oidc.client)
    assert seen["follow_redirects"] is False and seen["trust_env"] is False
    assert seen["timeout"] == 10.0


def test_provider_unreachable_fails_generic_and_healthz_unaffected(oidc):
    oidc.provider.fail_all = True
    r = start(oidc.client)
    assert r.headers["location"] == "/login?error=failed"
    page = oidc.client.get("/login?error=failed")
    assert "Sign-in failed, try again." in page.text
    assert oidc.client.get("/healthz").json()["status"] == "ok"


def test_discovery_cached(oidc):
    login(oidc)
    login(oidc, client=oidc.new_client())
    assert oidc.provider.count(DISCOVERY_URL) == 1


# --------------------------------------------------------------------------------------------
# ID token profile
# --------------------------------------------------------------------------------------------


def _with_claims(oidc, **claims):
    oidc.provider.claim_overrides = claims
    r, _ = login(oidc)
    return r


def test_multi_audience_without_azp_refused(oidc):
    assert_failed(_with_claims(oidc, aud=[CLIENT_ID, "other"]))


def test_multi_audience_with_this_client_as_azp_accepted(oidc):
    r = _with_claims(oidc, aud=[CLIENT_ID, "other"], azp=CLIENT_ID)
    assert r.headers["location"] == "/"


def test_single_element_array_audience_accepted(oidc):
    assert _with_claims(oidc, aud=[CLIENT_ID]).headers["location"] == "/"


@pytest.mark.parametrize("claims", [
    {"aud": [CLIENT_ID, "other"], "azp": "other"},
    {"aud": ["other"]},
    {"aud": "other"},
    {"aud": []},
    {"aud": CLIENT_ID, "azp": "other"},
    {"aud": [CLIENT_ID, 7], "azp": CLIENT_ID},
])
def test_foreign_azp_or_missing_client_refused(oidc, claims):
    assert_failed(_with_claims(oidc, **claims))


@pytest.mark.parametrize("claims", [
    {"iss": "https://other.example.com"},
    {"exp": int(time.time()) - 120},
    {"iat": int(time.time()) + 600},
    {"exp": "9999999999"},
    {"iat": 1.5},
    {"sub": ""},
    {"sub": "x" * 256},
    {"nonce": "wrong"},
])
def test_bad_claims_refused(oidc, claims):
    assert_failed(_with_claims(oidc, **claims))


def test_missing_nonce_refused(oidc):
    oidc.provider.drop_claims = {"nonce"}
    r, _ = login(oidc)
    assert_failed(r)


def test_clock_leeway(oidc):
    now = int(time.time())
    assert _with_claims(oidc, iat=now + 20).headers["location"] == "/"


@pytest.mark.parametrize("alg", ["none", "HS256"])
def test_unsigned_or_symmetric_token_refused_even_if_advertised(oidc, alg):
    oidc.provider.discovery_extra["id_token_signing_alg_values_supported"] = [
        "none", "HS256", "RS256"]
    oidc.provider.alg = alg
    r, _ = login(oidc)
    assert_failed(r)


@pytest.mark.parametrize("alg", ["PS256", "ES256"])
def test_other_allowed_algorithms_accepted(oidc, alg):
    oidc.provider.alg = alg
    r, _ = login(oidc)
    assert r.headers["location"] == "/"


def test_algorithm_not_advertised_refused(oidc):
    oidc.provider.discovery_extra["id_token_signing_alg_values_supported"] = ["ES256"]
    oidc.provider.alg = "RS256"
    assert_failed(login(oidc)[0])


def test_empty_algorithm_intersection_is_config(oidc, caplog):
    oidc.provider.discovery_extra["id_token_signing_alg_values_supported"] = ["HS256"]
    with caplog.at_level(logging.INFO, logger="cairn.auth"):
        assert start(oidc.client).headers["location"] == "/login?error=failed"
    assert "reason=config" in caplog.text


def test_bad_signature_refused(oidc):
    from joserfc import jwt as jjwt
    from joserfc.jwk import RSAKey

    rogue = RSAKey.generate_key(2048, parameters={"kid": "rsa-1"})
    q = start_params(start(oidc.client))
    oidc.provider.raw_id_token = jjwt.encode({"alg": "RS256", "kid": "rsa-1"},
                                             oidc.provider.claims(q["nonce"], LINKED_SUB), rogue)
    r = callback(oidc.client, {"code": make_code(q["nonce"]), "state": q["state"], "iss": ISSUER})
    assert_failed(r)


def test_unknown_kid_refetches_jwks_at_most_once_a_minute(oidc):
    login(oidc)  # discovery + JWKS cached
    assert oidc.provider.count(JWKS_URL) == 1
    oidc.provider.header_overrides = {"kid": "rotated"}
    c2 = oidc.new_client()
    assert_failed(login(oidc, client=c2)[0])
    # The refetch is rate-limited: the cache is fresh (< 60 s), so no refetch at all.
    assert oidc.provider.count(JWKS_URL) == 1
    # Once the window has passed, ONE refetch picks up a rotated key.
    prov = oidc.app.state.oidc._provider
    prov.jwks_fetched_at -= 120
    rotated = oidc.provider.rsa.as_dict(private=False)
    rotated["kid"] = "rotated"
    oidc.provider.jwks_keys = [rotated]
    c3 = oidc.new_client()
    assert login(oidc, client=c3)[0].headers["location"] == "/"
    assert oidc.provider.count(JWKS_URL) == 2
    c4 = oidc.new_client()
    oidc.provider.header_overrides = {"kid": "unknown-again"}
    assert_failed(login(oidc, client=c4)[0])
    assert oidc.provider.count(JWKS_URL) == 2


# --------------------------------------------------------------------------------------------
# admission + the refusal page
# --------------------------------------------------------------------------------------------


def _users(tmp_path):
    con = _db(tmp_path)
    try:
        return con.execute("SELECT * FROM users ORDER BY id").fetchall()
    finally:
        con.close()


def test_unlinked_identity_refused_without_change(oidc):
    before = _users(oidc.tmp)
    r, _ = login(oidc, sub="zz-stranger-0006")
    assert r.status_code == 303 and r.headers["location"] == "/auth/refused"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    page = oidc.client.get("/auth/refused")
    assert page.status_code == 403
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["cache-control"] == "no-store"
    assert "This identity is not authorized for this Cairn instance" in page.text
    assert page.text.count("zz-stranger-0006") == 1 and ISSUER in page.text
    assert "local" not in page.text  # no local username
    again = oidc.client.get("/auth/refused")
    assert again.status_code == 403 and "zz-stranger-0006" not in again.text
    assert _users(oidc.tmp) == before
    assert not signed_in(oidc.client)


def test_refusal_page_without_value_is_generic(oidc):
    page = oidc.client.get("/auth/refused")
    assert page.status_code == 403
    assert "subject:" not in page.text


def test_refused_value_expires(oidc, monkeypatch):
    login(oidc, sub="zz-stranger-0006")
    import src.auth.routes as routes

    real = time.time
    monkeypatch.setattr(routes, "time", SimpleNamespace(time=lambda: real() + 301))
    page = oidc.client.get("/auth/refused")
    assert "zz-stranger-0006" not in page.text


def test_same_email_different_subject_refused(oidc):
    oidc.provider.claim_overrides = {"email": "local@localhost",
                                     "preferred_username": "local"}
    r, _ = login(oidc, sub="zz-other-subject-0007")
    assert r.headers["location"] == "/auth/refused"


def test_inactive_user_refused_like_unlinked(oidc):
    con = _db(oidc.tmp)
    con.execute("UPDATE users SET is_active=0")
    con.commit()
    con.close()
    r, _ = login(oidc)
    assert r.headers["location"] == "/auth/refused"


@pytest.mark.parametrize("groups,ok", [
    (["/cairn-admins", "/other"], True),
    (None, False),
    ("/cairn-admins", False),
    (["/Cairn-Admins"], False),
    (["/cairn-admins", 5], False),
    (["x"] * 257 + ["/cairn-admins"], False),
    (["a" * 513, "/cairn-admins"], False),
])
def test_required_group(oidc, groups, ok):
    oidc.app.state.oidc.settings.oidc_required_group = "/cairn-admins"
    if groups is None:
        oidc.provider.drop_claims = {"groups"}
    else:
        oidc.provider.claim_overrides = {"groups": groups}
    r, _ = login(oidc)
    assert r.headers["location"] == ("/" if ok else "/auth/refused")


def test_no_group_check_when_unset(oidc):
    oidc.provider.drop_claims = {"groups"}
    assert login(oidc)[0].headers["location"] == "/"


# --------------------------------------------------------------------------------------------
# sessions: re-validation, lifetime, fixation, logout, error page
# --------------------------------------------------------------------------------------------


def test_unlink_ends_an_active_session(oidc, capsys):
    login(oidc)
    assert signed_in(oidc.client)
    from src import database

    assert _cli("users", "unlink") == 0
    capsys.readouterr()
    database.reset_engine()
    r = oidc.client.get("/collections", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_revoke_sessions_ends_an_active_session_and_keeps_link(oidc, capsys):
    login(oidc)
    from src import database

    assert _cli("users", "revoke-sessions") == 0
    capsys.readouterr()
    database.reset_engine()
    assert not signed_in(oidc.client)
    assert login(oidc)[0].headers["location"] == "/"
    assert signed_in(oidc.client)


def test_relink_to_another_issuer_config_refuses_old_session(oidc):
    login(oidc)
    oidc.app.state.oidc.settings  # same settings object as get_settings()
    from src.config import get_settings

    get_settings().oidc_issuer = "https://idp.example.com/realms/other"
    try:
        assert not signed_in(oidc.client)
    finally:
        get_settings().oidc_issuer = ISSUER


def test_absolute_lifetime(oidc, monkeypatch):
    login(oidc)
    assert signed_in(oidc.client)
    import src.auth.wall as wall

    real = time.time
    # Patch only the wall's clock (not the global `time.time`, which the client's cookie jar
    # also reads: an "expired" cookie would simply not be sent, proving nothing).
    monkeypatch.setattr(wall, "time", SimpleNamespace(time=lambda: real() + 43200 + 1))
    r = oidc.client.get("/", headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "1970" in r.headers["set-cookie"]  # the session was cleared, not just refused
    monkeypatch.setattr(wall, "time", time)
    # The refusal cleared the session: it does not come back when the clock does.
    assert not signed_in(oidc.client)


def test_session_fixation(oidc):
    page = oidc.client.get("/login")
    old_token = _csrf(page.text)
    old_cookie = oidc.client.cookies.get("session")
    login(oidc)
    assert oidc.client.cookies.get("session") != old_cookie
    # The pre-login CSRF token is dead: its nonce did not survive into the new session.
    r = oidc.client.post("/events/ack-all", headers={"X-CSRF-Token": old_token, **HTML},
                         follow_redirects=False)
    assert r.status_code == 403
    # And a fixed (attacker-planted) pre-login session value is gone too.


def test_forged_session_fields_refused(oidc):
    from itsdangerous import TimestampSigner
    import base64
    import json

    signer = TimestampSigner("not-the-key")
    data = base64.b64encode(json.dumps({"uid": 1, "epoch": 1, "auth": "oidc",
                                        "login_at": int(time.time())}).encode())
    c = oidc.new_client()
    c.cookies.set("session", signer.sign(data).decode(), domain="testserver")
    assert not signed_in(c)


def test_logout_requires_csrf_and_clears(oidc):
    login(oidc)
    r = oidc.client.post("/auth/logout", data={}, follow_redirects=False)
    assert r.status_code == 403
    assert signed_in(oidc.client)
    page = oidc.client.get("/collections", headers=HTML)
    token = _csrf(page.text)
    r = oidc.client.post("/auth/logout", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert not signed_in(oidc.client)


def test_panel_page_shows_user_and_sign_out(oidc):
    login(oidc)
    page = oidc.client.get("/collections", headers=HTML)
    assert page.status_code == 200
    assert 'class="session-user"' in page.text and ">local<" in page.text
    assert 'action="/auth/logout"' in page.text


def test_authenticated_404_has_username_and_working_sign_out(oidc):
    login(oidc)
    page = oidc.client.get("/collection/99999", headers=HTML)
    assert page.status_code == 404
    assert 'class="session-user"' in page.text and ">local<" in page.text
    m = re.search(r'action="/auth/logout">\s*<input type="hidden" name="csrf_token" value="([^"]+)"',
                  page.text)
    assert m and m.group(1)
    r = oidc.client.post("/auth/logout", data={"csrf_token": m.group(1)}, follow_redirects=False)
    assert r.status_code == 303
    assert not signed_in(oidc.client)


def test_anonymous_error_page_shows_no_user(oidc):
    page = oidc.client.get("/static/nope.css", headers=HTML)
    assert page.status_code == 404
    assert "session-user" not in page.text and 'action="/auth/logout"' not in page.text


# --------------------------------------------------------------------------------------------
# attribution
# --------------------------------------------------------------------------------------------


def _seed_issue(oidc) -> int:
    from src import database
    from src.database import get_sessionmaker
    from src.models.db import Event, FileEntry

    root = oidc.tmp / "watched"
    root.mkdir()
    (root / "a.txt").write_text("x")

    async def go():
        cid = await seed_collection(root)
        async with get_sessionmaker()() as s:
            s.add(FileEntry(collection_id=cid, relpath="a.txt", size=1, sha256="b" * 64,
                            status="modified", ots_state="none"))
            await s.flush()
            fid = (await s.execute(__import__("sqlalchemy").select(FileEntry.id))).scalar()
            s.add(Event(collection_id=cid, file_id=fid, kind="modified"))
            await s.commit()
        return cid

    cid = asyncio.run(go())
    database.reset_engine()
    return cid


def test_accept_after_sso_login_is_attributed(oidc, caplog):
    cid = _seed_issue(oidc)
    login(oidc)
    page = oidc.client.get(f"/collection/{cid}/review", headers=HTML)
    m = re.search(rf'action="/collection/{cid}/review/adopt-changed".*?'
                  r'name="population_fp" value="([0-9a-f]{64})"', page.text, re.S)
    assert m, "no adopt-changed form"
    with caplog.at_level(logging.INFO):
        r = oidc.client.post(f"/collection/{cid}/review/adopt-changed",
                             data={"csrf_token": _csrf(page.text), "population_fp": m.group(1)},
                             follow_redirects=False)
        assert r.status_code == 303 and "stale" not in r.headers["location"]
        r = oidc.client.post("/events/ack-all", headers={"X-CSRF-Token": _csrf(page.text)})
        assert r.status_code == 200
    uid = _db(oidc.tmp).execute("SELECT id FROM users").fetchone()[0]
    acked = _db(oidc.tmp).execute(
        "SELECT acknowledged_by FROM events WHERE collection_id=?", (cid,)).fetchall()
    assert acked and all(row[0] == uid for row in acked)
    lines = [r.getMessage() for r in caplog.records if r.name == "cairn.audit"]
    assert f"action=accept:adopt-changed collection={cid} user=local auth=oidc" in lines
    assert any(line.startswith("action=ack-all ") and line.endswith("user=local auth=oidc")
               for line in lines)
    assert LINKED_SUB not in caplog.text


# --------------------------------------------------------------------------------------------
# bounds: transaction store, rate limits
# --------------------------------------------------------------------------------------------


def test_full_transaction_store_refuses_with_retry_after(oidc):
    store = oidc.app.state.oidc.transactions
    for i in range(store.capacity):
        assert store.admit(f"s{i}", "n", "v", "/")
    live = dict(store._items)
    r = start(oidc.client)
    assert r.status_code == 303 and r.headers["location"] == "/login?error=busy"
    assert r.headers["retry-after"]
    assert store._items == live  # nothing evicted
    assert "Too many sign-ins" in oidc.client.get("/login?error=busy").text


def test_expired_transactions_purged_before_admission(oidc):
    store = oidc.app.state.oidc.transactions
    for i in range(store.capacity):
        store.admit(f"s{i}", "n", "v", "/")
    for txn in store._items.values():
        txn.expires = 0.0
    start_params(start(oidc.client))
    assert len(store) == 1


def test_per_address_start_limit(oidc):
    for _ in range(10):
        start_params(start(oidc.client))
    r = start(oidc.client)
    assert r.headers["location"] == "/login?error=busy"


def test_global_start_budget(oidc):
    limits = oidc.app.state.oidc.limits
    for i in range(60):
        assert limits.allow_start(f"10.0.0.{i}")
    assert not limits.allow_start("192.0.2.1")


def test_per_address_callback_limit(oidc):
    for _ in range(10):
        assert_failed(callback(oidc.client, {"state": "x"}))
    assert_failed(callback(oidc.client, {"state": "x"}))
    assert not oidc.app.state.oidc.limits.allow_callback("testclient")


# --------------------------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------------------------


def test_logs_carry_no_secret_token_state_nonce_verifier_or_subject(oidc, caplog):
    # Everything Cairn and uvicorn emit, at every level; third-party DEBUG tracing (aiosqlite
    # prints bound SQL parameters at DEBUG) is an operator opt-in outside this contract.
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="cairn")
    caplog.set_level(logging.DEBUG, logger="uvicorn")
    r, q = login(oidc)  # success
    login(oidc, sub="zz-stranger-0006", client=oidc.new_client())  # refusal
    c3 = oidc.new_client()
    q3 = start_params(start(c3))
    callback(c3, {"code": "zzBADcode", "state": q3["state"], "iss": ISSUER})  # failure
    verifier = oidc.provider.token_forms[0]["code_verifier"]
    # The test's own browser-side client logs its request URLs through `httpx`; those lines are
    # the client's, not the server's. The server's provider calls (also `httpx`) stay in.
    text = "\n".join(
        f"{r.name} {r.getMessage()}" for r in caplog.records
        if not (r.name == "httpx" and "https://testserver" in r.getMessage())
    )
    forbidden = [CLIENT_SECRET, SECRET_KEY, LINKED_SUB, "zz-stranger-0006", q["state"],
                 q["nonce"], verifier, CODE_MARKER, "zzBADcode", q3["state"],
                 "zz-access-token-RECOGNISABLE-0005", "eyJ"]
    for value in forbidden:
        assert value not in text, value
    reasons = re.findall(r"reason=(\w+)", text)
    from src.auth.oidc import REASONS

    assert reasons and set(reasons) <= REASONS
    assert "oidc login succeeded user=local" in text


# --------------------------------------------------------------------------------------------
# access-log filter (unit) — the real-server proof is test_oidc_server_logs.py
# --------------------------------------------------------------------------------------------


def _record(args):
    return logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d', args, None)


def test_access_log_filter_rewrites_and_drops():
    from src.auth.access_log import CallbackQueryFilter

    f = CallbackQueryFilter()
    rec = _record(("1.2.3.4:5", "GET", "/auth/oidc/callback?code=SECRET&state=S", "1.1", 303))
    assert f.filter(rec) and rec.getMessage() == '1.2.3.4:5 - "GET /auth/oidc/callback HTTP/1.1" 303'
    rec = _record(("1.2.3.4:5", "GET", "/auth/oidc/%63allback?code=SECRET", "1.1", 303))
    assert f.filter(rec) and "SECRET" not in rec.getMessage()
    rec = _record(("1.2.3.4:5", "GET", "/collections?x=1", "1.1", 200))
    assert f.filter(rec) and rec.getMessage().endswith('"GET /collections?x=1 HTTP/1.1" 200')
    assert not f.filter(_record(("only", "three", "args")))
    bad = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", None, None)
    bad.args = {"a": 1}
    assert not f.filter(bad)


# --------------------------------------------------------------------------------------------
# provider down at startup: the app starts, scans and answers /healthz
# --------------------------------------------------------------------------------------------


def test_provider_down_at_startup_does_not_block(oidc):
    oidc.provider.fail_all = True
    with oidc.new_client() as c:  # runs the lifespan
        assert c.get("/healthz").json()["status"] == "ok"
    assert oidc.provider.requests == []  # startup never contacts the provider
