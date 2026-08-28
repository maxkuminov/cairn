"""Migration 0011 round-trip against a populated database.

Shared prep owns the single Alembic revision of the guard-proof-and-restore-integrity change, so
its round-trip test lives here rather than with either implementation slice.

Covers the datastore spec delta's three scenarios:
- the upgrade adds nullable `files.ots_digest` (NULL on every existing row, nothing else rewritten)
  and widens `events.kind` to accept `restored_changed`, preserving existing event rows;
- the downgrade **refuses** while a `restored_changed` event exists, naming the kind and the count,
  and leaves every table exactly as it was;
- with no such rows the downgrade narrows the CHECK back, drops `ots_digest`, and preserves rows.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_migration_0011.py``
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PREV = "0010_auto_baseline_new"


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    """Every row of every domain table, ordered — for proving a refused downgrade changed nothing."""
    con = sqlite3.connect(db)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("collections", "files", "runs", "events")
        }
    finally:
        con.close()


def test_migration_0011_round_trip_and_refusing_downgrade(tmp_path):
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

    # --- seed a populated database at the previous head -------------------------------------
    alembic("upgrade", PREV)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO collections (user_id,name,root,mode,hash_cadence_seconds,"
        "verify_cadence_seconds,ots_mode,exclude_globs_json,alert_json,created_at,"
        "auto_baseline_new) VALUES (1,'c','/tmp','worm',900,604800,'perfile','[]','{}',"
        "'2026-01-01',0)"
    )
    con.execute(
        "INSERT INTO files (collection_id,relpath,size,sha256,first_seen,status,ots_path,"
        "ots_state) VALUES (1,'a.txt',3,'aa','2026-01-01','ok','/p/a.txt.ots','complete')"
    )
    con.execute(
        "INSERT INTO runs (collection_id,started,added,modified,missing,moved,stamped,upgraded,"
        "deep,result,kind,processed) VALUES (1,'2026-01-01',1,0,0,0,1,0,0,'ok','scan',1)"
    )
    con.execute(
        "INSERT INTO events (collection_id,file_id,kind,detected_at,acknowledged_at) "
        "VALUES (1,1,'added','2026-01-01','2026-01-01')"
    )
    con.commit()
    con.close()
    seeded = _snapshot(db)

    # --- upgrade: additive column, widened CHECK, existing rows untouched --------------------
    alembic("upgrade", "head")
    con = sqlite3.connect(db)
    cols = {row[1]: row for row in con.execute("PRAGMA table_info(files)")}
    assert "ots_digest" in cols
    assert cols["ots_digest"][3] == 0  # nullable
    assert con.execute("SELECT ots_digest FROM files").fetchall() == [(None,)]
    # Nothing else about the pre-existing rows changed.
    assert con.execute("SELECT relpath,size,sha256,status,ots_path,ots_state FROM files").fetchone() == (
        "a.txt", 3, "aa", "ok", "/p/a.txt.ots", "complete"
    )
    assert _snapshot(db)["events"] == seeded["events"]
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    assert "ck_events_kind" in ddl and "restored_changed" in ddl
    # The widened CHECK actually admits the new kind.
    con.execute(
        "INSERT INTO events (collection_id,file_id,kind,detected_at) "
        "VALUES (1,1,'restored_changed','2026-02-01')"
    )
    con.commit()
    con.close()
    with_stranded = _snapshot(db)

    # --- downgrade refuses while a restored_changed event exists ----------------------------
    r = alembic("downgrade", PREV, expect_ok=False)
    output = r.stdout + r.stderr
    assert "restored_changed" in output
    assert "1 events row" in output
    # Every table is exactly as it was: no event deleted, re-kinded, or otherwise altered.
    assert _snapshot(db) == with_stranded
    con = sqlite3.connect(db)
    assert "ots_digest" in {row[1] for row in con.execute("PRAGMA table_info(files)")}
    assert con.execute(
        "SELECT count(*) FROM events WHERE kind='restored_changed'"
    ).fetchone()[0] == 1
    con.close()

    # --- with the row gone the downgrade proceeds normally ----------------------------------
    con = sqlite3.connect(db)
    con.execute("DELETE FROM events WHERE kind='restored_changed'")
    con.commit()
    con.close()

    alembic("downgrade", PREV)
    con = sqlite3.connect(db)
    assert "ots_digest" not in {row[1] for row in con.execute("PRAGMA table_info(files)")}
    ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()[0]
    assert "ck_events_kind" in ddl and "restored_changed" not in ddl
    con.close()
    # Every other row survived the rebuild.
    assert _snapshot(db) == seeded
