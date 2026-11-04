"""Migration 0013 (oidc identity) — preservation, no-rebuild trace, and schema constraints.

Task A.1a / A.1b of oidc-sso-login, datastore spec delta. The risk this file exists for: a SQLite
table *rebuild* of ``users`` (Alembic batch mode: copy, ``DROP TABLE users``, rename) runs with
``PRAGMA foreign_keys=ON`` (``alembic/env.py`` sets it on every migration connection), and
``collections.user_id`` is ``ON DELETE CASCADE`` — so a rebuild would silently delete every
collection, file, run and event, and null every ``events.acknowledged_by``. The migration must
therefore alter ``users`` in place, and this file proves it on a populated database:

- row counts, full ID sets, every ``collections.user_id`` and ``events.acknowledged_by`` (and, for
  good measure, every row of the four child tables) are identical after upgrade AND after
  downgrade, and ``PRAGMA foreign_key_check`` is empty after each;
- the migration runs through the real ``alembic/env.py`` (so foreign keys are ON exactly as in
  production — the trace proves the pragma was issued), in a subprocess that installs a
  ``sqlite3`` trace callback on every DBAPI connection, and no traced statement is a
  ``CREATE TABLE`` / ``DROP TABLE`` / ``ALTER TABLE … RENAME`` touching ``users``;
- the downgrade restores ``users``' DDL byte-for-byte.

Never run against a real Cairn database: every database here lives under ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
REV = "0013_oidc_identity"
PREV = "0012_run_error_visibility"

# Runs Alembic exactly as `alembic` would (through alembic/env.py), but first registers a
# pool-level "connect" listener on every Engine that installs a sqlite3 trace callback, so every
# statement the migration issues — including env.py's own PRAGMA — is recorded.
_TRACED_ALEMBIC = r"""
import json, sys
from sqlalchemy import event
from sqlalchemy.engine import Engine

statements = []

@event.listens_for(Engine, "connect")
def _trace(dbapi_conn, _record):
    dbapi_conn.set_trace_callback(statements.append)

from alembic.config import main
try:
    main(argv=sys.argv[2:])
finally:
    with open(sys.argv[1], "w") as fh:
        json.dump(statements, fh)
"""

# A statement that would rebuild, drop or rename `users` (or a batch-mode temp copy of it).
_REBUILD = re.compile(
    r"^\s*(?:CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\b(?:\s+IF\s+NOT\s+EXISTS)?\s+[\"`\[]?"
    r"(?:_alembic_tmp_)?users\b"
    r"|DROP\s+TABLE\b(?:\s+IF\s+EXISTS)?\s+[\"`\[]?(?:_alembic_tmp_)?users\b"
    r"|ALTER\s+TABLE\s+[\"`\[]?(?:_alembic_tmp_)?users[\"`\]]?\s+RENAME\b"
    r"|ALTER\s+TABLE\s+\S+\s+RENAME\s+TO\s+[\"`\[]?users\b)",
    re.IGNORECASE,
)


def _env(db: Path) -> dict[str, str]:
    return dict(
        os.environ,
        CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{db}",
        PYTHONPATH=str(REPO),
    )


def _alembic(db: Path, *args: str) -> None:
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=_env(db), cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr


def _traced_alembic(db: Path, trace_file: Path, *args: str) -> list[str]:
    r = subprocess.run(
        [sys.executable, "-c", _TRACED_ALEMBIC, str(trace_file), *args],
        env=_env(db), cwd=REPO, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(trace_file.read_text())


def _seed_0012(db: Path) -> None:
    """Two users (non-contiguous ids), collections owned by each, files, runs, acked events."""
    _alembic(db, "upgrade", PREV)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executemany(
        "INSERT INTO users (id,username,password_hash,is_admin,is_active,created_at) "
        "VALUES (?,?,NULL,?,1,'2026-01-01 00:00:00')",
        [(1, "local", 1), (7, "second", 0)],
    )
    collections = [(1, 1, "alpha"), (2, 7, "beta"), (5, 7, "gamma"), (9, 1, "delta")]
    con.executemany(
        "INSERT INTO collections (id,user_id,name,root,mode,hash_cadence_seconds,"
        "verify_cadence_seconds,ots_mode,exclude_globs_json,alert_json,created_at,"
        "auto_baseline_new) VALUES (?,?,?,'/w/'||?,'worm',900,604800,'perfile','[]','{}',"
        "'2026-01-01',0)",
        [(cid, uid, name, name) for cid, uid, name in collections],
    )
    file_id = 0
    for cid, _uid, _name in collections:
        for n in range(3):
            file_id += 1
            con.execute(
                "INSERT INTO files (id,collection_id,relpath,size,sha256,first_seen,status,"
                "ots_state) VALUES (?,?,?,3,'aa','2026-01-01','ok','none')",
                (file_id * 10, cid, f"f{n}.txt"),
            )
        con.execute(
            "INSERT INTO runs (collection_id,started,finished,result,kind,processed) "
            "VALUES (?,'2026-01-01','2026-01-01','ok','scan',3)",
            (cid,),
        )
    events = [
        # (collection, file, kind, acknowledged_by)
        (1, 10, "added", 1),
        (2, 40, "missing", 7),
        (5, 70, "modified", 7),
        (9, 100, "added", 1),
        (9, 110, "missing", None),
    ]
    for cid, fid, kind, ack in events:
        con.execute(
            "INSERT INTO events (collection_id,file_id,kind,detected_at,acknowledged_at,"
            "acknowledged_by) VALUES (?,?,?,'2026-01-02',?,?)",
            (cid, fid, kind, "2026-01-03" if ack else None, ack),
        )
    con.commit()
    con.close()


_USERS_0012_COLS = "id,username,password_hash,is_admin,is_active,created_at,last_login_at"


def _snapshot(db: Path) -> dict:
    con = sqlite3.connect(db)
    try:
        snap: dict = {}
        for t in ("users", "collections", "files", "runs", "events"):
            snap[f"{t}.count"] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            snap[f"{t}.ids"] = [r[0] for r in con.execute(f"SELECT id FROM {t} ORDER BY id")]
        snap["collections.user_id"] = con.execute(
            "SELECT id, user_id FROM collections ORDER BY id"
        ).fetchall()
        snap["events.acknowledged_by"] = con.execute(
            "SELECT id, acknowledged_by FROM events ORDER BY id"
        ).fetchall()
        # Stronger than required: every pre-0013 column of every row, child tables in full.
        snap["users.rows"] = con.execute(
            f"SELECT {_USERS_0012_COLS} FROM users ORDER BY id"
        ).fetchall()
        for t in ("collections", "files", "runs", "events"):
            snap[f"{t}.rows"] = con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
        return snap
    finally:
        con.close()


def _fk_check(db: Path) -> list:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()


def _users_ddl(db: Path) -> list:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='users' ORDER BY name"
        ).fetchall()
    finally:
        con.close()


def _assert_no_users_rebuild(statements: list[str]) -> None:
    hits = [s for s in statements if _REBUILD.search(s)]
    assert hits == [], f"migration rebuilt/dropped/renamed `users`: {hits}"


def test_rebuild_detector_catches_a_batch_rebuild():
    """Negative control: the detector must flag what Alembic batch mode actually emits."""
    batch_mode = [
        "CREATE TABLE _alembic_tmp_users (id INTEGER NOT NULL, PRIMARY KEY (id))",
        "INSERT INTO _alembic_tmp_users (id) SELECT users.id FROM users",
        "DROP TABLE users",
        "ALTER TABLE _alembic_tmp_users RENAME TO users",
    ]
    assert [bool(_REBUILD.search(s)) for s in batch_mode] == [True, False, True, True]
    assert _REBUILD.search('CREATE TABLE "users" (id INTEGER)')
    assert _REBUILD.search("ALTER TABLE users RENAME TO users_old")
    for benign in (
        "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users DROP COLUMN oidc_issuer",
        "CREATE UNIQUE INDEX uq_users_oidc_identity ON users (oidc_issuer, oidc_subject)",
        "CREATE TABLE users_audit (id INTEGER)",
    ):
        assert not _REBUILD.search(benign), benign


def test_migration_0013_preserves_every_row_and_never_rebuilds_users(tmp_path):
    db = tmp_path / "scratch.db"
    _seed_0012(db)
    before = _snapshot(db)
    ddl_before = _users_ddl(db)
    assert before["users.count"] == 2 and before["collections.count"] == 4
    assert before["files.count"] == 12 and before["runs.count"] == 4
    assert before["events.count"] == 5
    assert {a for _, a in before["events.acknowledged_by"]} == {1, 7, None}
    assert _fk_check(db) == []

    # --- upgrade -----------------------------------------------------------------------------
    up = _traced_alembic(db, tmp_path / "up.json", "upgrade", REV)
    assert any(re.fullmatch(r"PRAGMA foreign_keys\s*=\s*ON", s.strip(), re.I) for s in up), (
        "the migration did not run with foreign keys enforced (env.py's pragma not traced)"
    )
    _assert_no_users_rebuild(up)
    adds = [s for s in up if re.match(r"\s*ALTER TABLE users ADD COLUMN", s, re.I)]
    assert len(adds) == 4, adds
    assert any("CREATE UNIQUE INDEX uq_users_oidc_identity" in s for s in up)

    assert _snapshot(db) == before
    assert _fk_check(db) == []
    con = sqlite3.connect(db)
    new_cols = con.execute(
        "SELECT id, oidc_issuer, oidc_subject, oidc_linked_at, session_epoch FROM users "
        "ORDER BY id"
    ).fetchall()
    con.close()
    assert new_cols == [(1, None, None, None, 0), (7, None, None, None, 0)]

    # --- downgrade ---------------------------------------------------------------------------
    down = _traced_alembic(db, tmp_path / "down.json", "downgrade", PREV)
    assert any(re.fullmatch(r"PRAGMA foreign_keys\s*=\s*ON", s.strip(), re.I) for s in down)
    _assert_no_users_rebuild(down)
    drops = [s for s in down if re.match(r"\s*ALTER TABLE users DROP COLUMN", s, re.I)]
    assert [s.split()[-1] for s in drops] == [
        "oidc_linked_at", "oidc_subject", "oidc_issuer", "session_epoch",
    ]

    assert _snapshot(db) == before
    assert _fk_check(db) == []
    assert _users_ddl(db) == ddl_before, "downgrade did not restore users' DDL exactly"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "mig0013", REPO / "alembic" / "versions" / "0013_oidc_identity.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeOp:
    def __init__(self, version: str) -> None:
        self.version = version
        self.executed: list[str] = []

    def get_bind(self):
        version = self.version

        class _Result:
            def scalar(self):
                return version

        class _Dialect:
            name = "sqlite"

        class _Bind:
            dialect = _Dialect()

            def exec_driver_sql(self, _sql):
                return _Result()

        return _Bind()

    def execute(self, stmt):
        self.executed.append(stmt)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migration_refuses_sqlite_older_than_3_35(direction):
    mod = _load_migration()
    fake = _FakeOp("3.34.1")
    mod.op = fake
    with pytest.raises(RuntimeError, match=r"3\.35.*3\.34\.1"):
        getattr(mod, direction)()
    assert fake.executed == [], "a refused migration must issue no statement"

    ok = _FakeOp("3.35.0")
    mod.op = ok
    getattr(mod, direction)()
    assert ok.executed and not any(_REBUILD.search(s) for s in ok.executed)
    assert not any("batch" in s.lower() for s in ok.executed)


# --- A.1b: schema constraints, by direct SQL (not the service) --------------------------------

ISS = "https://idp.example.com/realms/example"
NOW = "2026-11-04 00:00:00"


def _migrated_head(tmp_path) -> Path:
    db = tmp_path / "head.db"
    _alembic(db, "upgrade", "head")
    return db


def _create_all(tmp_path) -> Path:
    from sqlalchemy import create_engine

    from src.models.db import Base

    db = tmp_path / "model.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db


@pytest.fixture(params=["migration", "model"])
def schema_db(request, tmp_path):
    """The same constraints must hold on a migrated schema and on a `create_all` one."""
    db = _migrated_head(tmp_path) if request.param == "migration" else _create_all(tmp_path)
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executemany(
        "INSERT INTO users (id,username,is_admin,is_active,created_at,session_epoch) "
        "VALUES (?,?,1,1,?,0)",
        [(1, "a", NOW), (2, "b", NOW)],
    )
    con.commit()
    yield con
    con.close()


def _link(con, uid, issuer, subject, linked_at=NOW):
    con.execute(
        "UPDATE users SET oidc_issuer=?, oidc_subject=?, oidc_linked_at=? WHERE id=?",
        (issuer, subject, linked_at, uid),
    )


def test_schema_unique_pair(schema_db):
    _link(schema_db, 1, ISS, "sub-1")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        _link(schema_db, 2, ISS, "sub-1")
    # Same subject at a different issuer is a different identity.
    _link(schema_db, 2, "https://other.example.com", "sub-1")


@pytest.mark.parametrize(
    "issuer,subject,linked_at",
    [
        (ISS, None, None),
        (None, "sub", None),
        (None, None, NOW),
        (ISS, "sub", None),
        (ISS, None, NOW),
        (None, "sub", NOW),
    ],
)
def test_schema_refuses_half_linked(schema_db, issuer, subject, linked_at):
    with pytest.raises(sqlite3.IntegrityError, match="ck_users_oidc_all_or_none"):
        _link(schema_db, 1, issuer, subject, linked_at)


def test_schema_issuer_length_bound(schema_db):
    base = "https://idp.example.com/"
    _link(schema_db, 1, base + "a" * (512 - len(base)), "s1")
    with pytest.raises(sqlite3.IntegrityError, match="ck_users_oidc_issuer_len"):
        _link(schema_db, 2, base + "a" * (513 - len(base)), "s2")


def test_schema_subject_length_bound(schema_db):
    _link(schema_db, 1, ISS, "s" * 255)
    with pytest.raises(sqlite3.IntegrityError, match="ck_users_oidc_subject_len"):
        _link(schema_db, 2, ISS, "t" * 256)


def test_schema_session_epoch_defaults_to_zero(schema_db):
    schema_db.execute(
        "INSERT INTO users (id,username,is_admin,is_active,created_at) VALUES (3,'c',0,1,?)",
        (NOW,),
    )
    assert schema_db.execute("SELECT session_epoch FROM users WHERE id=3").fetchone() == (0,)
