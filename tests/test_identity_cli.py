"""`cairn users list|link|unlink|revoke-sessions` and the identity service (oidc-sso-login A.3–A.5).

Covers every scenario under "The CLI SHALL manage identity links and work without
authentication", plus the 512/513 issuer and 255/256 subject boundaries through the CLI, `link`
in `single` mode with `CAIRN_OIDC_ISSUER` set (refused without `--issuer`, linked with it), and a
`users` command on a `multi` database with no user (non-zero, one line).

`oidc` mode itself is configured by slice B (`src/config.py`); until then these tests put the CLI
and the identity service in `oidc` mode by overriding `get_settings()` with a view of the real
settings whose `auth_mode`/`oidc_issuer` are replaced, so the tests keep working after B lands.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import seed_collection

REPO = Path(__file__).resolve().parent.parent
ISS = "https://idp.example.com/realms/example"
OTHER_ISS = "https://login.example.org/tenant"


def _cli(capsys, *argv: str) -> tuple[int, str, str]:
    from src.cli import main

    rc = main(["users", *argv])
    out, err = capsys.readouterr()
    return rc, out, err


def _rows(tmp_path) -> list[tuple]:
    con = sqlite3.connect(tmp_path / "db" / "cairn.db")
    try:
        return con.execute(
            "SELECT username, oidc_issuer, oidc_subject, oidc_linked_at IS NOT NULL, "
            "session_epoch FROM users ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def _one_line_error(rc: int, err: str) -> str:
    assert rc != 0
    lines = err.strip().splitlines()
    assert len(lines) == 1, f"expected one line on stderr, got: {err!r}"
    return lines[0]


class _SettingsView:
    """The real settings with a few attributes overridden (no validation re-run)."""

    def __init__(self, real, **overrides) -> None:
        self._real = real
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._real, name)


@pytest.fixture
def oidc_mode(cairn_env, monkeypatch, capsys):
    """Bootstrap the owner (single), then switch the CLI and identity service to `oidc`."""
    _cli(capsys, "list")  # creates the implicit user, exactly as `cairn serve` would
    import src.config
    import src.services.identity as identity

    def install(issuer: str | None = ISS) -> None:
        real = src.config.get_settings()
        view = _SettingsView(real, auth_mode="oidc", oidc_issuer=issuer)
        monkeypatch.setattr(src.config, "get_settings", lambda: view)
        monkeypatch.setattr(identity, "get_settings", lambda: view)

    install()
    return install


def _add_user(tmp_path, username: str) -> None:
    con = sqlite3.connect(tmp_path / "db" / "cairn.db")
    con.execute(
        "INSERT INTO users (username,is_admin,is_active,created_at,session_epoch) "
        "VALUES (?,0,1,'2026-01-01 00:00:00',0)",
        (username,),
    )
    con.commit()
    con.close()


# --- Scenario: Link the operator ----------------------------------------------------------------


def test_link_operator_in_oidc_mode_keeps_collection_owner(
    cairn_env, oidc_mode, capsys, tmp_path
):
    watched = tmp_path / "watched"
    watched.mkdir()
    cid = asyncio.run(seed_collection(watched))
    owner_before = sqlite3.connect(tmp_path / "db" / "cairn.db").execute(
        "SELECT user_id FROM collections WHERE id=?", (cid,)
    ).fetchone()

    rc, out, err = _cli(capsys, "link", "--subject", "1234-abcd")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", ISS, "1234-abcd", 1, 1)]
    assert "1234-abcd" not in out  # the success line names the user, not the subject
    owner_after = sqlite3.connect(tmp_path / "db" / "cairn.db").execute(
        "SELECT user_id FROM collections WHERE id=?", (cid,)
    ).fetchone()
    assert owner_after == owner_before


# --- Scenario: Link outside oidc mode needs an explicit issuer --------------------------------


def test_link_in_single_mode_requires_issuer_even_when_configured(
    cairn_env, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("CAIRN_OIDC_ISSUER", ISS)
    from src.config import get_settings

    get_settings.cache_clear()
    _cli(capsys, "list")
    before = _rows(tmp_path)

    rc, _out, err = _cli(capsys, "link", "--subject", "1234-abcd")
    assert "--issuer" in _one_line_error(rc, err)
    assert _rows(tmp_path) == before

    rc, _out, err = _cli(capsys, "link", "--subject", "1234-abcd", "--issuer", ISS)
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", ISS, "1234-abcd", 1, 1)]


def test_link_in_oidc_mode_without_configured_issuer_is_refused(oidc_mode, capsys, tmp_path):
    oidc_mode(None)
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", "--subject", "s")
    assert "--issuer" in _one_line_error(rc, err)
    assert _rows(tmp_path) == before


def test_explicit_issuer_overrides_default_in_oidc_mode(oidc_mode, capsys, tmp_path):
    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--issuer", OTHER_ISS)
    assert rc == 0, err
    assert _rows(tmp_path)[0][1] == OTHER_ISS


# --- Scenario: Relink without --replace ---------------------------------------------------------


def test_relink_requires_replace(oidc_mode, capsys, tmp_path):
    assert _cli(capsys, "link", "--subject", "first")[0] == 0
    before = _rows(tmp_path)

    for subject in ("second", "first"):  # a different pair, and the very same pair
        rc, _out, err = _cli(capsys, "link", "--subject", subject)
        assert "--replace" in _one_line_error(rc, err)
        assert _rows(tmp_path) == before

    rc, _out, err = _cli(capsys, "link", "--subject", "second", "--replace")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", ISS, "second", 1, 2)]


# --- Scenario: Revoke sessions / unlink ---------------------------------------------------------


def test_revoke_sessions_bumps_epoch_and_keeps_link(oidc_mode, capsys, tmp_path):
    assert _cli(capsys, "link", "--subject", "s")[0] == 0
    rc, _out, err = _cli(capsys, "revoke-sessions")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", ISS, "s", 1, 2)]


def test_revoke_sessions_works_on_an_unlinked_user(oidc_mode, capsys, tmp_path):
    rc, _out, err = _cli(capsys, "revoke-sessions")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", None, None, 0, 1)]


def test_unlink_clears_triple_and_bumps_epoch(oidc_mode, capsys, tmp_path):
    assert _cli(capsys, "link", "--subject", "s")[0] == 0
    rc, _out, err = _cli(capsys, "unlink")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", None, None, 0, 2)]
    # Unlinking an unlinked user is refused and changes nothing.
    rc, _out, err = _cli(capsys, "unlink")
    _one_line_error(rc, err)
    assert _rows(tmp_path) == [("local", None, None, 0, 2)]


# --- Subject / issuer validation through the CLI -----------------------------------------------


def test_subject_boundary_255_256(oidc_mode, capsys, tmp_path):
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", "--subject", "x" * 256)
    assert "255" in _one_line_error(rc, err)
    assert _rows(tmp_path) == before
    rc, _out, err = _cli(capsys, "link", "--subject", "x" * 255)
    assert rc == 0, err
    assert _rows(tmp_path)[0][2] == "x" * 255


@pytest.mark.parametrize("subject", ["", "abc\x00def", "line\nbreak", "tab\there", "del\x7f", "c1\x85"])
def test_bad_subject_refused(oidc_mode, capsys, tmp_path, subject):
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", f"--subject={subject}")
    _one_line_error(rc, err)
    assert _rows(tmp_path) == before


def test_issuer_boundary_512_513(oidc_mode, capsys, tmp_path):
    base = "https://idp.example.com/"
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--issuer", base + "a" * (513 - len(base)))
    assert "512" in _one_line_error(rc, err)
    assert _rows(tmp_path) == before
    ok = base + "a" * (512 - len(base))
    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--issuer", ok)
    assert rc == 0, err
    assert _rows(tmp_path)[0][1] == ok


BAD_ISSUERS = [
    "http://idp.example.com/realms/example",
    "https://user:pw@idp.example.com/realms/example",
    "https://user@idp.example.com/",
    "https://idp.example.com/realms/example?x=1",
    "https://idp.example.com/realms/example?",
    "https://idp.example.com/realms/example#frag",
    "https:///no-host",
    "idp.example.com/realms/example",
    "https://idp.example.com/realms/ex ample",
    "https://idp.example.com:notaport/",
]


@pytest.mark.parametrize("issuer", BAD_ISSUERS)
def test_bad_issuer_refused_when_given(oidc_mode, capsys, tmp_path, issuer):
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--issuer", issuer)
    _one_line_error(rc, err)
    assert _rows(tmp_path) == before


@pytest.mark.parametrize("issuer", BAD_ISSUERS[:3] + ["https://idp.example.com/" + "a" * 600])
def test_bad_issuer_refused_when_defaulted(oidc_mode, capsys, tmp_path, issuer):
    oidc_mode(issuer)
    before = _rows(tmp_path)
    rc, _out, err = _cli(capsys, "link", "--subject", "s")
    _one_line_error(rc, err)
    assert _rows(tmp_path) == before


# --- User resolution and pair ownership --------------------------------------------------------


def test_multiple_users_require_user_flag(oidc_mode, capsys, tmp_path):
    _add_user(tmp_path, "second")
    before = _rows(tmp_path)
    for action in (["link", "--subject", "s"], ["unlink"], ["revoke-sessions"]):
        rc, _out, err = _cli(capsys, *action)
        assert "--user" in _one_line_error(rc, err)
        assert _rows(tmp_path) == before

    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--user", "second")
    assert rc == 0, err
    assert _rows(tmp_path) == [("local", None, None, 0, 0), ("second", ISS, "s", 1, 1)]

    rc, _out, err = _cli(capsys, "link", "--subject", "s", "--user", "nobody")
    _one_line_error(rc, err)


def test_pair_linked_to_another_user_is_refused_even_with_replace(oidc_mode, capsys, tmp_path):
    _add_user(tmp_path, "second")
    assert _cli(capsys, "link", "--subject", "shared", "--user", "local")[0] == 0
    before = _rows(tmp_path)
    for extra in ([], ["--replace"]):
        rc, _out, err = _cli(capsys, "link", "--subject", "shared", "--user", "second", *extra)
        assert "another user" in _one_line_error(rc, err)
        assert _rows(tmp_path) == before
    # The same subject at another issuer is a different identity.
    rc, _out, err = _cli(
        capsys, "link", "--subject", "shared", "--user", "second", "--issuer", OTHER_ISS
    )
    assert rc == 0, err


def test_list_shows_link(oidc_mode, capsys, tmp_path):
    assert _cli(capsys, "link", "--subject", "1234-abcd")[0] == 0
    rc, out, err = _cli(capsys, "list")
    assert rc == 0, err
    lines = out.strip().splitlines()
    assert lines[0].split()[:5] == ["username", "admin", "active", "epoch", "linked_at"]
    assert lines[1].split()[0] == "local" and ISS in lines[1] and "1234-abcd" in lines[1]


# --- Service-level contract --------------------------------------------------------------------


def test_find_linked_user_matches_the_exact_pair_only(oidc_mode, capsys):
    asyncio.run(_find_linked_user_scenario())


async def _find_linked_user_scenario():
    from src.database import get_sessionmaker
    from src.services import identity

    async with get_sessionmaker()() as s:
        user = await identity.link(s, subject="abc", issuer=ISS)
        assert user.session_epoch == 1 and user.oidc_linked_at is not None
    async with get_sessionmaker()() as s:
        assert (await identity.find_linked_user(s, ISS, "abc")).username == "local"
        assert await identity.find_linked_user(s, ISS, "ABC") is None
        assert await identity.find_linked_user(s, ISS + "/", "abc") is None
        assert await identity.find_linked_user(s, OTHER_ISS, "abc") is None
        assert await identity.find_linked_user(s, "", "") is None


def test_identity_error_is_one_line():
    from src.services.identity import IdentityError

    assert str(IdentityError("a\nb\r\nc")) == "a b c"


# --- multi mode: no user, one line, non-zero ---------------------------------------------------


@pytest.fixture
def multi_env(tmp_path, monkeypatch):
    db = tmp_path / "db" / "cairn.db"
    monkeypatch.setenv("CAIRN_DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("CAIRN_PROOF_STORE_PATH", str(tmp_path / "proofs"))
    monkeypatch.setenv("CAIRN_AUTH_MODE", "multi")
    monkeypatch.setenv("CAIRN_SECRET_KEY", "k" * 64)
    monkeypatch.setenv("CAIRN_SCHEDULER_ENABLED", "0")
    from src import database
    from src.config import get_settings

    get_settings.cache_clear()
    database.reset_engine()
    database.ensure_dirs()
    database.run_migrations()
    yield tmp_path
    get_settings.cache_clear()
    database.reset_engine()


@pytest.mark.parametrize(
    "action",
    [["list"], ["link", "--subject", "s", "--issuer", ISS], ["unlink"], ["revoke-sessions"]],
)
def test_multi_mode_with_no_user_exits_non_zero_one_line(multi_env, capsys, action):
    rc, _out, err = _cli(capsys, *action)
    assert "no user" in _one_line_error(rc, err)
    assert _rows(multi_env) == []  # nothing bootstrapped in multi


def test_users_cli_runs_as_a_real_process_without_a_server(tmp_path):
    """No server, no login: a real `python -m src.cli` process links and lists."""
    env = dict(
        os.environ,
        CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{tmp_path / 'cairn.db'}",
        CAIRN_PROOF_STORE_PATH=str(tmp_path / "proofs"),
        CAIRN_AUTH_MODE="single",
        PYTHONPATH=str(REPO),
    )
    env.pop("CAIRN_OIDC_ISSUER", None)

    def run(*argv):
        return subprocess.run(
            [sys.executable, "-m", "src.cli", *argv],
            env=env, cwd=REPO, capture_output=True, text=True,
        )

    assert run("init").returncode == 0
    r = run("users", "link", "--subject", "1234-abcd")
    assert r.returncode != 0 and len(r.stderr.strip().splitlines()) == 1
    assert "--issuer" in r.stderr and "Traceback" not in r.stderr
    r = run("users", "link", "--subject", "1234-abcd", "--issuer", ISS)
    assert r.returncode == 0, r.stderr
    r = run("users", "list")
    assert r.returncode == 0 and "1234-abcd" in r.stdout


# --- help ---------------------------------------------------------------------------------------


def test_top_level_help_lists_users_exactly_once(capsys):
    from src.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    entries = [ln for ln in out.splitlines() if ln.strip().split(" ")[0] == "users"]
    assert len(entries) == 1, entries
    # The slice 0 goldens strip exactly this one line from the top-level help; it must match.
    from tests.test_single_mode_goldens import _USERS_ENTRY

    assert _USERS_ENTRY.match(entries[0])
