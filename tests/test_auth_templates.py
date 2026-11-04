"""oidc-sso-login slice C: the login page, the refusal page and the signed-in header.

Every template is rendered with a hand-built context (the Fixed contracts in the change's
tasks.md) through a throwaway Starlette app, so `url_for('static', …)` resolves and nothing here
depends on slice B's routes, the datastore or the configured auth mode.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.testclient import TestClient

from src.auth import ui
from src.auth.ui import SSO_BUTTON_LABEL

REPO = Path(__file__).resolve().parent.parent
PANEL = REPO / "src" / "control_panel"
TEMPLATES_DIR = PANEL / "templates"

XSS = '"><script>alert(1)</script>'


def _render(template: str, ctx: dict) -> str:
    """Render `template` with exactly `ctx` (plus the request) and return the HTML body."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    ui.register_template_globals(templates)

    async def page(request: Request):
        return templates.TemplateResponse(request, template, dict(ctx))

    app = Starlette(
        routes=[
            Route("/page", page),
            Mount("/static", app=StaticFiles(directory=str(PANEL / "static")), name="static"),
        ]
    )
    with TestClient(app) as client:
        r = client.get("/page")
    assert r.status_code == 200
    return r.text


def _login(**over) -> str:
    ctx = {"csrf_token": "tok-123", "mode": "light", "next": None, "error": None}
    ctx.update(over)
    return _render("login.html", ctx)


def _refused(**over) -> str:
    ctx = {
        "mode": "light",
        "issuer": "https://idp.example.com/realms/example",
        "subject": "0f1e2d3c-aaaa-bbbb-cccc-123456789abc",
    }
    ctx.update(over)
    return _render("auth_refused.html", ctx)


def _base(**over) -> str:
    ctx = {
        "page": "",
        "mode": "light",
        "auth_mode": "oidc",
        "session_username": "owner",
        "username": "owner",
        "is_admin": True,
        "user_email": "owner@localhost",
        "sidebar_collections": [],
        "alert_count": 0,
        "csrf_token": "tok-456",
    }
    ctx.update(over)
    return _render("base.html", ctx)


# --- C.1 the label --------------------------------------------------------------------------


def test_label_value_and_global_registration():
    assert SSO_BUTTON_LABEL == "Sign in with SSO"
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    ui.register_template_globals(templates)
    ui.register_template_globals(templates.env)  # idempotent, accepts a bare Environment
    assert templates.env.globals["SSO_BUTTON_LABEL"] == SSO_BUTTON_LABEL


def test_label_is_spelled_only_in_ui_py():
    hits = []
    for root, _dirs, files in os.walk(REPO / "src"):
        for name in files:
            p = Path(root) / name
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if SSO_BUTTON_LABEL in text:
                hits.append(p.relative_to(REPO).as_posix())
    assert hits == ["src/auth/ui.py"]
    assert (REPO / "src/auth/ui.py").read_text().count(SSO_BUTTON_LABEL) == 1


# --- C.2 login.html -------------------------------------------------------------------------


def test_login_has_one_sso_post_form_and_no_password_fields():
    html = _login(next="/collections?x=1")
    assert html.count(SSO_BUTTON_LABEL) == 1
    forms = re.findall(r"<form\b[^>]*>", html)
    assert len(forms) == 1
    assert 'method="post"' in forms[0] and 'action="/auth/oidc/start"' in forms[0]
    assert '<input type="hidden" name="csrf_token" value="tok-123" />' in html
    assert '<input type="hidden" name="next" value="/collections?x=1" />' in html
    assert 'type="password"' not in html
    assert 'name="username"' not in html and 'name="password"' not in html
    # light/dark toggle kept; the stale multi-user strip is gone
    assert 'href="/mode/toggle"' in html
    assert "multi-user" not in html
    assert 'role="alert"' not in html


def test_login_without_next_omits_the_field():
    html = _login(next=None)
    assert 'name="next"' not in html


@pytest.mark.parametrize(
    "error, text",
    [("failed", "Sign-in failed, try again."), ("busy", "Too many sign-ins are in progress")],
)
def test_login_error_messages(error, text):
    html = _login(error=error)
    assert text in html
    assert html.count('role="alert"') == 1
    assert html.count(SSO_BUTTON_LABEL) == 1


def test_login_unknown_error_shows_nothing():
    assert 'role="alert"' not in _login(error="<b>other</b>")


def test_login_escapes_context():
    html = _login(next=XSS, csrf_token=XSS)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_login_dark_mode_attribute():
    html = _login(mode="dark")
    assert 'data-mode="dark"' in html


# --- C.3 auth_refused.html ------------------------------------------------------------------


def test_refused_shows_own_issuer_and_subject():
    html = _refused()
    assert "This identity is not authorized for this Cairn instance" in html
    m = re.search(r'<pre class="auth-identity[^"]*"[^>]*>(.*?)</pre>', html, re.S)
    assert m, "copyable identity block missing"
    block = m.group(1)
    assert "https://idp.example.com/realms/example" in block
    assert "0f1e2d3c-aaaa-bbbb-cccc-123456789abc" in block
    assert 'href="/login"' in html
    # No local account information of any kind.
    assert "username" not in html.lower()
    assert "csrf" not in html.lower()


def test_refused_without_identity_is_generic():
    html = _refused(issuer=None, subject=None)
    assert "This identity is not authorized for this Cairn instance" in html
    assert "auth-identity" not in html
    assert 'href="/login"' in html


def test_refused_escapes_identity():
    html = _refused(issuer=XSS, subject=XSS)
    assert "<script>alert(1)</script>" not in html
    assert html.count("&lt;script&gt;") == 2


# --- C.4 base.html ------------------------------------------------------------------------


def _signout_forms(html: str) -> list[str]:
    return re.findall(r'<form class="session-signout"[^>]*>.*?</form>', html, re.S)


def test_base_oidc_shows_user_and_csrf_signout():
    html = _base(session_username="alice-op")
    forms = _signout_forms(html)
    assert len(forms) == 1
    assert 'method="post"' in forms[0] and 'action="/auth/logout"' in forms[0]
    assert '<input type="hidden" name="csrf_token" value="tok-456" />' in forms[0]
    assert 'aria-label="Sign out"' in forms[0]
    assert '<span class="session-user" title="Signed in as alice-op">alice-op</span>' in html


def test_base_oidc_anonymous_shows_neither():
    html = _base(session_username=None)
    assert _signout_forms(html) == []
    assert "session-user" not in html


@pytest.mark.parametrize("mode", ["single", "multi"])
def test_base_other_modes_have_no_oidc_controls(mode):
    html = _base(auth_mode=mode, session_username="someone")
    assert _signout_forms(html) == []
    assert "/auth/logout" not in html
    assert "session-user" not in html


def test_base_single_mode_unchanged_without_session_username_key():
    """Before slice B wires `session_username`, single-mode contexts do not carry it at all."""
    ctx_missing = _base(auth_mode="single", session_username=None)
    ctx_none = _render(
        "base.html",
        {
            "page": "",
            "mode": "light",
            "auth_mode": "single",
            "username": "owner",
            "is_admin": True,
            "user_email": "owner@localhost",
            "sidebar_collections": [],
            "alert_count": 0,
            "csrf_token": "tok-456",
        },
    )
    assert ctx_missing == ctx_none


def test_base_escapes_username():
    html = _base(session_username=XSS)
    assert "<script>alert(1)</script>" not in html


# --- C.5 phone width ------------------------------------------------------------------------


@pytest.mark.parametrize("render", [_login, _refused])
def test_auth_pages_are_phone_usable(render):
    html = render()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1" />' in html
    # No fixed pixel width that could overflow a 320px screen; only max-width caps.
    assert not re.search(r"(?<![-\w])width:\s*\d{3,}px", html)


def test_auth_css_fits_a_phone():
    css = (PANEL / "static/css/panel.css").read_text()
    section = css[css.index("oidc-sso-login: login / refusal screens") :]
    assert not re.search(r"(?<![-\w])width:\s*\d{3,}px", section)
    assert "overflow-wrap: anywhere" in section  # long issuer/subject wrap instead of scrolling
    assert re.search(r"@media \(max-width: 480px\)\s*\{[^}]*\.session-user\s*\{\s*display: none", section)
