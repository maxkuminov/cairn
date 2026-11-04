"""OIDC-off regression goldens (oidc-sso-login, slice 0).

These record what ``single`` mode does **today**, before the ``oidc`` mode exists, so that every
later slice can prove it left ``single`` untouched (configuration spec, "Single mode is
unchanged"). Three things are recorded:

* panel/HTTP responses (status, headers, normalized body) for a fixed request script against a
  seeded fixture database (task 0.1);
* the ``SessionMiddleware`` arguments the app is built with, and the ``--help`` text of every
  pre-existing CLI command plus the top-level ``cairn --help`` (task 0.2);
* the same capture with ``CAIRN_OIDC_*`` variables set and ``CAIRN_AUTH_MODE`` unset, which must be
  identical (task 0.3).

Every capture runs in a **fresh interpreter**: ``src.main`` reads settings at import time to build
its middleware, so an in-process capture would only ever see whichever environment imported it
first. The child process starts from the parent's environment with every ``CAIRN_*`` variable
removed, so a developer's shell cannot leak into the recording.

``/mode/toggle`` is recorded with a **same-site** ``Referer`` only. The cross-origin case is an open
redirect that slice B fixes in every mode (task B.4a) and is deliberately not goldened.

Normalization replaces only values that legitimately vary between runs (the CSRF token, the
session cookie value, the temporary directory, the package version, wall-clock dates/times). Each
normalizer is a narrow pattern, every replaced CSRF token is checked to be a real token for this
session, and the per-response replacement counts are part of the golden, so a normalizer that
starts swallowing something else changes the recording and fails the test.

Regenerate (only on purpose, and only on a base whose ``single`` behaviour is meant to change):

    CAIRN_REGEN_GOLDENS=1 .venv/bin/python -m pytest tests/test_single_mode_goldens.py
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "single"
REGEN = os.environ.get("CAIRN_REGEN_GOLDENS") == "1"

# The one line the top-level help comparison may drop: the `users` subcommand entry that slice A
# adds. Its presence is asserted by task A.4, not hidden here.
_USERS_ENTRY = re.compile(r"^\s{2,}users(\s{2,}.*)?$")

# Pre-existing commands (before oidc-sso-login). `add-corpus` is an alias of `add-collection`.
PRE_EXISTING_COMMANDS = (
    "init",
    "serve",
    "add-collection",
    "add-corpus",
    "scan",
    "accept",
    "import-manifest",
    "verify",
    "export",
    "upgrade",
    "stamp",
    "bench",
    "status",
)

# Environment variants. Each must reproduce the same recording. OIDC_VARS covers every
# CAIRN_OIDC_* setting the change names, with a complete, otherwise-valid configuration.
OIDC_VARS = {
    "CAIRN_OIDC_ISSUER": "https://idp.example.com/realms/example",
    "CAIRN_OIDC_CLIENT_ID": "cairn",
    "CAIRN_OIDC_CLIENT_SECRET": "golden-test-client-secret-not-real",
    "CAIRN_OIDC_EXTRA_SCOPES": "groups",
    "CAIRN_OIDC_TOKEN_AUTH_METHOD": "client_secret_post",
    "CAIRN_OIDC_SESSION_MAX_AGE": "3600",
    "CAIRN_OIDC_REQUIRED_GROUP": "/cairn-admins",
}
VARIANTS = {
    "single-explicit": {"CAIRN_AUTH_MODE": "single"},
    "auth-mode-unset": {},
    "oidc-vars-set": dict(OIDC_VARS),
}

# Response headers never worth recording (they follow the body length).
_SKIP_HEADERS = {"content-length"}

FIXED = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

# Seeded files (fingerprinted instants use FIXED, freshness uses "now"; see _seed).
_SEED_FILES = (
    # relpath, status, ots_state
    ("ok/alpha.txt", "ok", "complete"),
    ("ok/beta.txt", "ok", "complete"),
    ("mod/gamma.txt", "modified", "incomplete"),
    ("gone/delta.txt", "missing", "none"),
    ("new/epsilon.txt", "new", "none"),
)


# --------------------------------------------------------------------------------------------
# child-process side: seed, request, normalize
# --------------------------------------------------------------------------------------------


def _seed(root: Path) -> dict[str, int]:
    from sqlalchemy import select

    from src.database import ensure_implicit_user, get_engine, get_sessionmaker
    from src.models.db import Event, FileEntry, Run, User
    from src.services.collections import create_collection

    async def go() -> dict[str, int]:
        # `now` drives scan freshness (so /healthz and the pill read "ok"); FIXED is used for every
        # field the review fingerprints hash (collection.created_at, files.first_seen,
        # events.detected_at), so those hex digests are reproducible without normalizing them.
        now = datetime.now(timezone.utc)
        try:
            async with get_sessionmaker()() as s:
                await ensure_implicit_user(s)
                uid = await s.scalar(select(User.id))
                c = await create_collection(
                    s, user_id=uid, name="fixture", root=str(root), mode="worm",
                    ots_mode="perfile",
                )
                c.created_at = FIXED
                for relpath, status, ots_state in _SEED_FILES:
                    s.add(
                        FileEntry(
                            collection_id=c.id, relpath=relpath, size=10, sha256="a" * 64,
                            status=status, ots_state=ots_state,
                            ots_path=(f"/proofs/{c.id}/{relpath}.ots"
                                      if ots_state != "none" else None),
                            first_seen=FIXED, last_checked=now,
                        )
                    )
                s.add(
                    Run(
                        collection_id=c.id, kind="scan", result="ok", processed=5, total=5,
                        started=now - timedelta(seconds=30), finished=now,
                    )
                )
                await s.flush()
                e1 = Event(collection_id=c.id, kind="missing", detected_at=FIXED)
                e2 = Event(collection_id=c.id, kind="modified", detected_at=FIXED)
                s.add_all([e1, e2])
                await s.commit()
                return {"collection_id": c.id, "event_id": e1.id}
        finally:
            await get_engine().dispose()

    return asyncio.run(go())


_CSRF_RE = re.compile(r"[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{20,}")
# Exactly the shapes the panel renders for wall-clock instants, and nothing broader: an early
# draft matched "Mark 2" as a month-day pair, which is the failure the replacement counts exist to
# catch. ISO instants (`2026-01-15 12:00` / `...T12:00:00+00:00`), humanize_date ("4 Nov 2026") and
# humanize_delta ("just now", "3 min ago", "1 hour ago", "2 days ago").
_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(\+00:00|Z)?")
_DATE_RE = re.compile(r"\b\d{1,2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4}\b")
_AGO_RE = re.compile(r"\b(just now|\d+ min ago|\d+ hours? ago|\d+ days? ago)\b")
_SESSION_COOKIE_RE = re.compile(r"^(session=)[^;]+")
# Cookie expiry (an HTTP-date one year after "now").
_EXPIRES_RE = re.compile(r"(?i)(expires=)[^;]+")


class _Normalizer:
    def __init__(self, tmp: str, version: str, csrf_check) -> None:
        self.tmp = tmp
        self.version = version
        self.csrf_check = csrf_check

    def text(self, value: str, counts: dict[str, int]) -> str:
        def sub(pattern: re.Pattern[str], repl: str, s: str, key: str, check=None) -> str:
            def _r(m: re.Match[str]) -> str:
                if check is not None:
                    check(m.group(0))
                counts[key] = counts.get(key, 0) + 1
                return repl
            return pattern.sub(_r, s)

        n = value.count(self.tmp)
        if n:
            counts["tmp"] = counts.get("tmp", 0) + n
            value = value.replace(self.tmp, "<TMP>")
        value = sub(_CSRF_RE, "<CSRF>", value, "csrf", self.csrf_check)
        value = sub(_ISO_RE, "<DATETIME>", value, "datetime")
        value = sub(_DATE_RE, "<DATE>", value, "date")
        value = sub(_AGO_RE, "<AGO>", value, "ago")
        return value


def _capture(out_path: str) -> None:
    """Child entry point: build env-specific state, run the request script, dump JSON."""
    from src import __version__, database
    from src.config import get_settings

    tmp = os.environ["CAIRN_GOLDEN_TMP"]
    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    root = Path(tmp) / "fixture"
    root.mkdir(exist_ok=True)
    ids = _seed(root)
    database.reset_engine()

    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    from src import csrf
    from src.main import app

    # --- 0.2: SessionMiddleware arguments ---------------------------------------------------
    import inspect

    mws = [m for m in app.user_middleware if m.cls is SessionMiddleware]
    assert len(mws) == 1, "expected exactly one SessionMiddleware"
    kwargs = dict(mws[0].kwargs)
    secret = kwargs.pop("secret_key")
    sig = inspect.signature(SessionMiddleware.__init__)
    effective = {
        name: p.default for name, p in sig.parameters.items()
        if name not in ("self", "app", "secret_key")
    }
    effective.update(kwargs)
    configured = get_settings().secret_key
    # Only the values that decide the cookie's security, not Starlette's whole signature: a new
    # keyword with a default (e.g. `partitioned`) must not fail this golden on a dependency bump.
    session_mw = {
        "session_cookie": effective["session_cookie"],
        "same_site": effective["same_site"],
        "https_only": effective["https_only"],
        "max_age": effective["max_age"],
        "secret_source": (
            "built-in-dev-fallback" if secret == "cairn-single-user-dev-key"
            else "settings"
            if configured is not None and secret == configured.get_secret_value()
            else "other"
        ),
        "csrf_secret_source": (
            "built-in-dev-fallback" if csrf._secret_key() == csrf._DEV_FALLBACK_KEY else "settings"
        ),
    }

    # --- 0.1: HTTP goldens --------------------------------------------------------------------
    cid, eid = ids["collection_id"], ids["event_id"]
    client = TestClient(app)

    def csrf_check(token: str) -> None:
        # Only real CSRF tokens for this client's session may be normalized away.
        from itsdangerous import URLSafeTimedSerializer

        URLSafeTimedSerializer(csrf._secret_key(), salt="csrf-token").loads(token)

    norm = _Normalizer(tmp, __version__, csrf_check)
    html = {"Accept": "text/html,application/xhtml+xml"}
    script = [
        ("dashboard", "GET", "/", html),
        ("collections", "GET", "/collections", html),
        ("collection", "GET", f"/collection/{cid}", html),
        ("collection_review", "GET", f"/collection/{cid}/review", html),
        ("fleet_review", "GET", "/review", html),
        ("settings", "GET", "/settings", html),
        ("verify", "GET", "/verify", html),
        ("healthz", "GET", "/healthz", {}),
        ("mode_toggle_same_site", "GET", "/mode/toggle",
         {"Referer": "http://testserver/collections"}),
        # The same local page as a path-only Referer: the form slice B.4a's rule produces as a
        # Location, so this one is expected to survive B unchanged in every respect.
        ("mode_toggle_same_site_path", "GET", "/mode/toggle", {"Referer": "/collection/1?view=tree"}),
        ("htmx_files_fragment", "GET", f"/collection/{cid}/files",
         {"HX-Request": "true", "HX-Current-URL": f"http://testserver/collection/{cid}"}),
        ("not_found_page", "GET", "/collection/99999", html),
        ("ack_post", "POST", f"/events/{eid}/ack", None),
    ]
    results: dict[str, dict] = {}
    for name, method, path, headers in script:
        if method == "POST":
            page = client.get("/", headers=html).text
            m = re.search(r'name="csrf-token" content="([^"]+)"', page)
            assert m, "no csrf token on the dashboard"
            headers = {"X-CSRF-Token": m.group(1), "HX-Request": "true"}
            resp = client.post(path, headers=headers)
        else:
            resp = client.request(method, path, headers=headers, follow_redirects=False)
        counts: dict[str, int] = {}
        hdrs: list[list[str]] = []
        for k, v in resp.headers.multi_items():
            k = k.lower()
            if k in _SKIP_HEADERS:
                continue
            if k == "set-cookie":
                v, n = _SESSION_COOKIE_RE.subn(r"\1<SESSION>", v)
                if n:
                    counts["session_cookie"] = counts.get("session_cookie", 0) + n
                v, n = _EXPIRES_RE.subn(r"\1<HTTPDATE>", v)
                if n:
                    counts["cookie_expires"] = counts.get("cookie_expires", 0) + n
            v = norm.text(v, counts)
            hdrs.append([k, v])
        body = resp.text
        if name == "healthz":
            data = json.loads(body)
            assert data.get("version") == __version__
            data["version"] = "<VERSION>"
            counts["version"] = 1
            for c in data.get("collections", []):
                age = c.get("last_scan_age_seconds")
                # Seconds since the seeded scan: a wall-clock value. Only a real number is replaced.
                assert isinstance(age, (int, float)) and 0 <= age < 300, age
                c["last_scan_age_seconds"] = "<AGE>"
                counts["scan_age"] = counts.get("scan_age", 0) + 1
            body = json.dumps(data, indent=2, sort_keys=True)
        body = norm.text(body, counts)
        results[name] = {
            "request": {"method": method, "path": path,
                        "headers": sorted((headers or {}).keys())},
            "status": resp.status_code,
            "headers": sorted(hdrs),
            "normalized": dict(sorted(counts.items())),
            "body": body,
        }

    # --- 0.2: CLI help ------------------------------------------------------------------------
    from src.cli import build_parser

    def help_of(argv: list[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            build_parser().parse_args(argv)
        return buf.getvalue()

    cli = {"cairn": help_of(["--help"])}
    for cmd in PRE_EXISTING_COMMANDS:
        cli[cmd] = help_of([cmd, "--help"])

    Path(out_path).write_text(json.dumps(
        {"http": results, "session_middleware": session_mw, "cli_help": cli},
        indent=2, sort_keys=True,
    ))


# --------------------------------------------------------------------------------------------
# parent side
# --------------------------------------------------------------------------------------------


def _run_capture(tmp_path: Path, extra_env: dict[str, str]) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAIRN_")}
    work = tmp_path / "work"
    work.mkdir()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "COLUMNS": "100",  # argparse wraps help to the terminal width
            "CAIRN_GOLDEN_TMP": str(work),
            "CAIRN_DATABASE_URL": f"sqlite+aiosqlite:///{work / 'db' / 'cairn.db'}",
            "CAIRN_PROOF_STORE_PATH": str(work / "proofs"),
            "CAIRN_SCHEDULER_ENABLED": "0",
        }
    )
    env.update(extra_env)
    out = tmp_path / "capture.json"
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; from tests.test_single_mode_goldens import _capture; _capture(sys.argv[1])",
         str(out)],
        cwd=str(work),  # no stray .env in the cwd
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"capture failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(out.read_text())


def _write_goldens(cap: dict) -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for name, r in cap["http"].items():
        meta = {k: v for k, v in r.items() if k != "body"}
        (GOLDEN_DIR / f"http_{name}.json").write_text(json.dumps(meta, indent=2) + "\n")
        (GOLDEN_DIR / f"http_{name}.body").write_text(r["body"])
    (GOLDEN_DIR / "session_middleware.json").write_text(
        json.dumps(cap["session_middleware"], indent=2, sort_keys=True) + "\n"
    )
    for cmd, text in cap["cli_help"].items():
        # The top-level golden is the pre-`users` help: regenerating must not bake the entry in.
        if cmd == "cairn":
            text = _strip_users_entry(text)
        (GOLDEN_DIR / f"cli_help_{cmd}.txt").write_text(text)


def _strip_users_entry(text: str) -> str:
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if _USERS_ENTRY.match(line.rstrip("\n"))]
    assert len(hits) <= 1, f"more than one `users` entry in cairn --help: {hits}"
    return "".join(line for i, line in enumerate(lines) if i not in hits)


def _compare(cap: dict) -> None:
    golden_http = sorted(p.stem[len("http_"):] for p in GOLDEN_DIR.glob("http_*.json"))
    assert sorted(cap["http"]) == golden_http
    for name, r in cap["http"].items():
        meta = json.loads((GOLDEN_DIR / f"http_{name}.json").read_text())
        got = {k: v for k, v in r.items() if k != "body"}
        assert json.loads(json.dumps(got)) == meta, f"{name}: status/headers/normalization differ"
        body = (GOLDEN_DIR / f"http_{name}.body").read_text()
        assert r["body"] == body, f"{name}: body differs from golden"
    sm = json.loads((GOLDEN_DIR / "session_middleware.json").read_text())
    assert cap["session_middleware"] == sm
    for cmd in PRE_EXISTING_COMMANDS:
        golden = (GOLDEN_DIR / f"cli_help_{cmd}.txt").read_text()
        assert cap["cli_help"][cmd] == golden, f"`cairn {cmd} --help` differs"
    top = (GOLDEN_DIR / "cli_help_cairn.txt").read_text()
    assert _strip_users_entry(cap["cli_help"]["cairn"]) == top, "`cairn --help` differs"


@pytest.fixture(scope="module")
def captures(tmp_path_factory) -> dict[str, dict]:
    return {
        name: _run_capture(tmp_path_factory.mktemp(name), env)
        for name, env in VARIANTS.items()
    }


def test_goldens_match(captures):
    base = captures["single-explicit"]
    if REGEN:
        _write_goldens(base)
    _compare(base)


@pytest.mark.parametrize("variant", [v for v in VARIANTS if v != "single-explicit"])
def test_variant_identical(captures, variant):
    """0.3: auth mode unset, and CAIRN_OIDC_* set outside oidc mode, change nothing."""
    _compare(captures[variant])
    assert captures[variant] == captures["single-explicit"]


def test_normalization_hides_nothing_else(captures):
    """Every placeholder in the golden is accounted for by a recorded replacement count, and the
    expected normalizations actually happened (a page that stopped rendering its CSRF token, or a
    cookie whose value stopped being replaced, would show up here)."""
    http = captures["single-explicit"]["http"]
    placeholders = {
        "<CSRF>": "csrf", "<TMP>": "tmp", "<DATETIME>": "datetime", "<DATE>": "date",
        "<AGO>": "ago", "<VERSION>": "version", "<SESSION>": "session_cookie",
        "<HTTPDATE>": "cookie_expires", "<AGE>": "scan_age",
    }
    for name, r in http.items():
        text = r["body"] + "".join(v for _, v in r["headers"])
        for ph, key in placeholders.items():
            assert text.count(ph) == r["normalized"].get(key, 0), (name, ph)
        # No unexpected angle-bracket placeholder shape we did not introduce.
        assert not re.search(r"<(?!CSRF>|TMP>|DATETIME>|DATE>|AGO>|VERSION>|SESSION>|HTTPDATE>|AGE>)[A-Z_]{3,}>",
                             text), name
    # Pages that carry the shell must carry a CSRF token; the first response sets the session.
    for name in ("dashboard", "collections", "collection", "settings", "verify"):
        assert http[name]["normalized"].get("csrf", 0) >= 1, name
    assert http["dashboard"]["normalized"].get("session_cookie") == 1
    assert http["healthz"]["normalized"] == {"scan_age": 1, "version": 1}
    assert http["mode_toggle_same_site"]["status"] == 303
    # B.4a: an absolute same-site Referer now yields its path only (never an origin).
    assert ["location", "/collections"] in http["mode_toggle_same_site"]["headers"]

