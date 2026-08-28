"""Migration 0012 round-trip against a populated database.

Shared prep owns the single Alembic revision of the add-fleet-review-and-run-health change, so its
round-trip test lives here rather than with either implementation slice.

Covers the datastore spec delta's run-error scenario:
- the upgrade adds `runs.errors` (NOT NULL, default 0) and `runs.error_sample` (nullable TEXT)
  against a database holding existing rows, defaulting every existing run and rewriting nothing;
- the columns actually accept a recorded count and a JSON sample;
- the downgrade drops both columns and preserves every other row — no refusal guard here, unlike
  0011: the sample is a convenience copy of what the finalize WARNING already logs.

Run from the repo root: ``PYTHONPATH=. pytest tests/test_migration_0012.py``
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Pinned to this revision rather than `head` so a later revision's schema change cannot leak into
# this test's before/after comparisons (the reason 0011's test is pinned too).
REV = "0012_run_error_visibility"
PREV = "0011_proof_provenance_and_restored_changed"


def _snapshot(db: Path) -> dict[str, list[tuple]]:
    """Every row of every domain table, ordered — for proving the round trip changed nothing."""
    con = sqlite3.connect(db)
    try:
        return {
            t: con.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
            for t in ("collections", "files", "runs", "events")
        }
    finally:
        con.close()


def test_migration_0012_round_trip_on_populated_db(tmp_path):
    db = tmp_path / "scratch.db"
    env = dict(os.environ, CAIRN_DATABASE_URL=f"sqlite+aiosqlite:///{db}")

    def alembic(*args):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=env, cwd=REPO,
            capture_output=True, text=True,
        )
        assert r.returncode == 0, r.stderr
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
        "INSERT INTO runs (collection_id,started,added,modified,missing,moved,stamped,upgraded,"
        "deep,result,kind,processed) VALUES (1,'2026-01-02',0,0,0,0,0,0,1,'partial','scan',7)"
    )
    con.execute(
        "INSERT INTO events (collection_id,file_id,kind,detected_at,acknowledged_at) "
        "VALUES (1,1,'added','2026-01-01','2026-01-01')"
    )
    con.commit()
    con.close()
    seeded = _snapshot(db)

    # --- upgrade: two additive columns, existing rows defaulted and otherwise untouched ------
    alembic("upgrade", REV)
    con = sqlite3.connect(db)
    run_cols = {row[1]: row for row in con.execute("PRAGMA table_info(runs)")}
    assert "errors" in run_cols
    assert run_cols["errors"][3] == 1  # NOT NULL
    assert "0" in str(run_cols["errors"][4])  # server default 0
    assert "error_sample" in run_cols
    assert run_cols["error_sample"][3] == 0  # nullable
    # Every pre-existing run survived, defaulted: no recorded skips, no sample.
    assert con.execute(
        "SELECT errors, error_sample FROM runs ORDER BY id"
    ).fetchall() == [(0, None), (0, None)]
    # Nothing else about the pre-existing rows changed.
    assert con.execute(
        "SELECT collection_id,started,added,modified,missing,moved,stamped,upgraded,deep,"
        "result,kind,processed FROM runs ORDER BY id"
    ).fetchall() == [
        (1, "2026-01-01", 1, 0, 0, 0, 1, 0, 0, "ok", "scan", 1),
        (1, "2026-01-02", 0, 0, 0, 0, 0, 0, 1, "partial", "scan", 7),
    ]
    after_upgrade = _snapshot(db)
    assert after_upgrade["files"] == seeded["files"]
    assert after_upgrade["events"] == seeded["events"]
    assert after_upgrade["collections"] == seeded["collections"]

    # The columns actually hold what the change writes: a count and a bounded JSON sample.
    con.execute(
        "UPDATE runs SET errors = 3, error_sample = ? WHERE id = 2",
        ('["unstorable-name: b\'1\\\\xe0.jpg\'", "stat: x.txt", "+1 more skipped '
         '(sample truncated)"]',),
    )
    con.commit()
    assert con.execute("SELECT errors FROM runs WHERE id = 2").fetchone()[0] == 3
    con.close()

    # --- downgrade drops both columns and preserves every other row -------------------------
    alembic("downgrade", PREV)
    con = sqlite3.connect(db)
    remaining = {row[1] for row in con.execute("PRAGMA table_info(runs)")}
    assert "errors" not in remaining
    assert "error_sample" not in remaining
    con.close()
    assert _snapshot(db) == seeded
