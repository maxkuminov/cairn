"""Migration 0014 (add-path-rules, design D1) round-trip against a populated database.

- the upgrade adds `collections.rules_json` TEXT NOT NULL defaulting to '[]' — every existing
  collection reads '[]', and no other column or row changes;
- the downgrade drops the column and preserves every row.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_migration_0014.py``
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REV = "0014_path_rules"
PREV = "0013_scan_safety_nets"


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(db)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("collections", "files", "runs", "events")
        }
    finally:
        con.close()


def _columns(db: Path, table: str) -> dict[str, tuple]:
    con = sqlite3.connect(db)
    try:
        return {row[1]: row for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_migration_0014_round_trip(tmp_path):
    db = tmp_path / "scratch.db"
    env = dict(os.environ, CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{db}")

    def alembic(*args):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, cwd=REPO,
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
        return r

    alembic("upgrade", PREV)
    con = sqlite3.connect(db)
    for i, name in enumerate(("c1", "c2"), start=1):
        con.execute(
            "INSERT INTO collections (user_id,name,root,mode,hash_cadence_seconds,"
            "verify_cadence_seconds,ots_mode,exclude_globs_json,alert_json,created_at,"
            "auto_baseline_new) VALUES (1,?,'/tmp','worm',900,604800,'none','[\"*.tmp\"]','{}',"
            "'2026-01-01',0)",
            (name,),
        )
        con.execute(
            "INSERT INTO files (collection_id,relpath,size,mtime,sha256,first_seen,status,"
            "ots_state) VALUES (?,'a.txt',3,1700000000.5,'aa','2026-01-01','modified','none')",
            (i,),
        )
        con.execute(
            "INSERT INTO events (collection_id,file_id,kind,detected_at,detail) "
            "VALUES (?,?,'modified','2026-01-01','{\"path\": \"a.txt\"}')",
            (i, i),
        )
    con.commit()
    con.close()
    seeded = _snapshot(db)
    before_cols = _columns(db, "collections")
    assert "rules_json" not in before_cols

    # --- upgrade -------------------------------------------------------------------------------
    alembic("upgrade", REV)
    cols = _columns(db, "collections")
    assert list(cols)[: len(before_cols)] == list(before_cols)
    rules = cols["rules_json"]
    assert rules[2].upper() == "TEXT" and rules[3] == 1  # type, NOT NULL
    assert rules[4] in ("'[]'", "[]")
    after = _snapshot(db)
    assert [row[:-1] for row in after["collections"]] == seeded["collections"]
    assert [row[-1] for row in after["collections"]] == ["[]", "[]"]
    for t in ("files", "runs", "events"):
        assert after[t] == seeded[t]
    # A collection inserted without the column (an older writer) still gets '[]'.
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO collections (user_id,name,root,mode,hash_cadence_seconds,"
        "verify_cadence_seconds,ots_mode,exclude_globs_json,alert_json,created_at,"
        "auto_baseline_new) VALUES (1,'c3','/tmp','worm',900,604800,'none','[]','{}',"
        "'2026-01-01',0)"
    )
    con.commit()
    assert con.execute("SELECT rules_json FROM collections WHERE name='c3'").fetchone() == ("[]",)
    con.execute("DELETE FROM collections WHERE name='c3'")
    con.commit()
    con.close()

    # --- downgrade -----------------------------------------------------------------------------
    alembic("downgrade", PREV)
    assert "rules_json" not in _columns(db, "collections")
    assert _snapshot(db) == seeded

    # And back up again cleanly.
    alembic("upgrade", REV)
    assert "rules_json" in _columns(db, "collections")
