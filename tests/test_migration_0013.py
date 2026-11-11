"""Migration 0013 (add-scan-safety-nets, design D7) round-trip against a populated database.

- the upgrade adds nullable `files.prior_sha256` / `prior_size` / `prior_mtime` (NULL on every
  existing row, nothing else rewritten) and widens `events.kind` to accept `corrupted`, preserving
  existing event rows;
- the downgrade **refuses** while a `corrupted` event exists and leaves every table as it was;
- with no such rows the downgrade narrows the CHECK back, drops the three columns, and preserves
  every row.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_migration_0013.py``
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REV = "0013_scan_safety_nets"
PREV = "0012_run_error_visibility"


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(db)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("collections", "files", "runs", "events")
        }
    finally:
        con.close()


def test_migration_0013_round_trip_and_refusing_downgrade(tmp_path):
    db = tmp_path / "scratch.db"
    env = dict(os.environ, CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{db}")

    def alembic(*args, expect_ok: bool = True):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, cwd=REPO,
            capture_output=True, text=True,
        )
        if expect_ok:
            assert r.returncode == 0, r.stderr
        else:
            assert r.returncode != 0, f"expected failure, got:\n{r.stdout}\n{r.stderr}"
        return r

    alembic("upgrade", PREV)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO collections (user_id,name,root,mode,hash_cadence_seconds,"
        "verify_cadence_seconds,ots_mode,exclude_globs_json,alert_json,created_at,"
        "auto_baseline_new) VALUES (1,'c','/tmp','churn',900,604800,'none','[]','{}',"
        "'2026-01-01',0)"
    )
    con.execute(
        "INSERT INTO files (collection_id,relpath,size,mtime,sha256,first_seen,status,ots_state) "
        "VALUES (1,'a.txt',3,1700000000.5,'aa','2026-01-01','modified','none')"
    )
    con.execute(
        "INSERT INTO runs (collection_id,started,added,modified,missing,moved,stamped,upgraded,"
        "deep,result,kind,processed,errors) VALUES (1,'2026-01-01',1,0,0,0,0,0,0,'ok','scan',1,0)"
    )
    con.execute(
        "INSERT INTO events (collection_id,file_id,kind,detected_at,detail) "
        "VALUES (1,1,'restored_changed','2026-01-01','recorded aa → found bb')"
    )
    con.commit()
    con.close()
    seeded = _snapshot(db)

    # --- upgrade -------------------------------------------------------------------------------
    alembic("upgrade", REV)
    con = sqlite3.connect(db)
    cols = {row[1]: row for row in con.execute("PRAGMA table_info(files)")}
    for name in ("prior_sha256", "prior_size", "prior_mtime"):
        assert name in cols
        assert cols[name][3] == 0  # nullable
    assert con.execute(
        "SELECT prior_sha256, prior_size, prior_mtime FROM files"
    ).fetchall() == [(None, None, None)]
    # Existing rows otherwise unchanged (the snapshot's file rows gain three trailing NULLs).
    after = _snapshot(db)
    assert [r[:-3] for r in after["files"]] == seeded["files"]
    assert after["events"] == seeded["events"]
    assert after["runs"] == seeded["runs"]
    assert after["collections"] == seeded["collections"]
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    assert "ck_events_kind" in ddl and "corrupted" in ddl and "restored_changed" in ddl
    con.execute(
        "INSERT INTO events (collection_id,file_id,kind,detected_at) "
        "VALUES (1,1,'corrupted','2026-02-01')"
    )
    con.commit()
    # The CHECK still refuses an unknown kind.
    try:
        con.execute(
            "INSERT INTO events (collection_id,file_id,kind,detected_at) "
            "VALUES (1,1,'bogus','2026-02-01')"
        )
        raise AssertionError("an unknown kind was admitted")
    except sqlite3.IntegrityError:
        pass
    con.close()
    with_stranded = _snapshot(db)

    # --- downgrade refuses while a corrupted event exists ---------------------------------------
    r = alembic("downgrade", PREV, expect_ok=False)
    output = r.stdout + r.stderr
    assert "corrupted" in output
    assert "1 events row" in output
    assert _snapshot(db) == with_stranded

    # --- with the row gone the downgrade proceeds normally --------------------------------------
    con = sqlite3.connect(db)
    con.execute("DELETE FROM events WHERE kind='corrupted'")
    con.commit()
    con.close()

    alembic("downgrade", PREV)
    con = sqlite3.connect(db)
    names = {row[1] for row in con.execute("PRAGMA table_info(files)")}
    assert not names & {"prior_sha256", "prior_size", "prior_mtime"}
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    assert "ck_events_kind" in ddl and "corrupted" not in ddl and "restored_changed" in ddl
    con.close()
    assert _snapshot(db) == seeded
