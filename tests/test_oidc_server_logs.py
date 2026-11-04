"""Real-server proof that the callback query never reaches a log (oidc-sso-login B.5b, design D11).

A real uvicorn runs in a subprocess through each supported entrypoint:

* ``cairn serve`` (``src.cli.main(["serve", ...])`` → ``uvicorn.run``);
* the image's CMD argv (``uvicorn src.main:app --host … --port … --workers 1 --proxy-headers
  --forwarded-allow-ips *``, run through uvicorn's own CLI entry point).

Each child first points ``src.auth.oidc.transport_factory`` at the in-process fake provider
(``tests/oidc_fake.py``), the only difference from production, then hands over to the entrypoint
unchanged. The test drives the success, failure and refusal paths with recognisable ``code``,
``state`` and ``iss`` values and asserts none of them, nor the raw query string, appears in the
captured stdout/stderr — while the access log itself is demonstrably on (the bare callback path
is logged).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from tests.oidc_fake import CODE_MARKER, ISSUER, LINKED_SUB, make_code, oidc_env

REPO = Path(__file__).resolve().parent.parent

_PRELUDE = (
    "import sys; sys.path.insert(0, {repo!r}); "
    "import src.auth.oidc as o; from tests.oidc_fake import FakeProvider; "
    "p = FakeProvider(); o.transport_factory = p.transport; "
)
ENTRYPOINTS = {
    "cairn-serve": _PRELUDE + (
        "from src.cli import main; sys.exit(main(['serve', '--host', '127.0.0.1', "
        "'--port', '{port}']))"
    ),
    "image-cmd": _PRELUDE + (
        "import uvicorn.main as um; sys.argv = ['uvicorn', 'src.main:app', '--host', '127.0.0.1', "
        "'--port', '{port}', '--workers', '1', '--proxy-headers', '--forwarded-allow-ips', '*']; "
        "sys.exit(um.main())"
    ),
}


def _dockerfile_cmd() -> list[str]:
    text = (REPO / "Dockerfile").read_text()
    m = re.search(r"^CMD \[(.*)\]\s*$", text, re.M)
    assert m
    return [part.strip().strip('"') for part in m.group(1).split(",")]


def test_image_cmd_argv_matches_dockerfile():
    """The `image-cmd` variant replays exactly the Dockerfile's CMD (host/port aside)."""
    cmd = _dockerfile_cmd()
    assert cmd[:2] == ["uvicorn", "src.main:app"]
    assert cmd[2:] == ["--host", "0.0.0.0", "--port", "8000", "--workers", "1",
                       "--proxy-headers", "--forwarded-allow-ips", "*"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Browser:
    """A minimal cookie-carrying client: the session cookie is `Secure`, so httpx would not send it
    back over plain http; this sends it explicitly, as a browser behind a TLS proxy would."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie: str | None = None
        self.http = httpx.Client(base_url=base, follow_redirects=False, timeout=10,
                                 trust_env=False)

    def _remember(self, resp: httpx.Response) -> None:
        for value in resp.headers.get_list("set-cookie"):
            if value.startswith("session="):
                self.cookie = value.split(";", 1)[0]

    def request(self, method: str, url: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {})
        if self.cookie:
            headers["Cookie"] = self.cookie
        resp = self.http.request(method, url, headers=headers, **kw)
        self._remember(resp)
        return resp

    def start(self) -> dict[str, str]:
        page = self.request("GET", "/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        resp = self.request("POST", "/auth/oidc/start", data={"csrf_token": token})
        assert resp.status_code == 303, resp.text
        return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


@pytest.mark.parametrize("entrypoint", sorted(ENTRYPOINTS))
def test_callback_query_never_logged(tmp_path, entrypoint):
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("CAIRN_")}
    env.update(oidc_env())
    env.update({
        "PYTHONPATH": str(REPO),
        "PYTHONUNBUFFERED": "1",
        "CAIRN_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'db' / 'cairn.db'}",
        "CAIRN_PROOF_STORE_PATH": str(tmp_path / "proofs"),
        "CAIRN_SCHEDULER_ENABLED": "0",
    })
    # Bootstrap + link the owner with the real CLI (oidc mode, configured issuer).
    init = subprocess.run(
        [sys.executable, "-c", "import sys; from src.cli import main; "
         "sys.exit(main(['init']) or main(['users', 'link', '--subject', sys.argv[1]]))",
         LINKED_SUB],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )
    assert init.returncode == 0, init.stderr

    log_path = tmp_path / "server.log"
    with open(log_path, "w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-c", ENTRYPOINTS[entrypoint].format(repo=str(REPO), port=port)],
            cwd=str(tmp_path), env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    base = f"http://127.0.0.1:{port}"
    secrets_seen: list[str] = []
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(base + "/healthz", timeout=2, trust_env=False).status_code:
                    break
            except httpx.HTTPError:
                pass
            assert proc.poll() is None, log_path.read_text()
            assert time.monotonic() < deadline, log_path.read_text()
            time.sleep(0.2)

        # success
        b = _Browser(base)
        q = b.start()
        params = {"code": make_code(q["nonce"]), "state": q["state"], "iss": ISSUER}
        r = b.request("GET", "/auth/oidc/callback?" + urlencode(params))
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert b.request("GET", "/collections").status_code == 200
        secrets_seen += [params["code"], params["state"], q["nonce"], urlencode(params)]

        # refusal (valid token, unlinked subject)
        b = _Browser(base)
        q = b.start()
        params = {"code": make_code(q["nonce"], "zz-stranger-0006"), "state": q["state"],
                  "iss": ISSUER}
        r = b.request("GET", "/auth/oidc/callback?" + urlencode(params))
        assert r.status_code == 303 and r.headers["location"] == "/auth/refused"
        secrets_seen += [params["code"], params["state"], urlencode(params)]

        # failure (bad code; and a wrong iss on a fresh transaction)
        b = _Browser(base)
        q = b.start()
        params = {"code": "zzFAILcode0008", "state": q["state"], "iss": ISSUER}
        r = b.request("GET", "/auth/oidc/callback?" + urlencode(params))
        assert r.headers["location"] == "/login?error=failed"
        secrets_seen += [params["code"], params["state"], urlencode(params)]
        b = _Browser(base)
        q = b.start()
        params = {"code": "zzFAILcode0009", "state": q["state"],
                  "iss": "https://zz-wrong-issuer.example.com"}
        r = b.request("GET", "/auth/oidc/callback?" + urlencode(params))
        assert r.headers["location"] == "/login?error=failed"
        secrets_seen += [params["code"], params["state"], "zz-wrong-issuer", urlencode(params)]
        # An encoded-path variant of the callback is sanitised too.
        r = b.request("GET", "/auth/oidc/%63allback?code=zzENCODEDcode0010&state=x")
        secrets_seen += ["zzENCODEDcode0010"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            proc.wait()

    out = log_path.read_text()
    for value in secrets_seen + [CODE_MARKER, LINKED_SUB, "zz-stranger-0006"]:
        assert value not in out, value
    assert "code=" not in out and "state=" not in out
    # The access log is on, and saw the callback — as a bare path.
    assert '"GET /auth/oidc/callback HTTP/1.1" 303' in out, out
    assert "/collections" in out
