# Tasks — tolerate non-UTF-8 filenames and never wedge a scan run

## 1. Skip un-storable paths (scanner)
- [x] 1.1 In `src/services/scanner.py`, add a small `_db_storable(relpath: str) -> bool` helper that
  returns whether the relpath round-trips through UTF-8 (`relpath.encode("utf-8")` succeeds) — the
  precondition for binding it as SQLite TEXT.
- [x] 1.2 At the top of the walk loop in `scan_corpus`, before any `stat`/`hash`/row creation, skip
  a relpath that is not `_db_storable`: `summary.errors += 1`, accumulate a capped sample of the raw
  `os.fsencode(relpath)` bytes, and `continue` (never add it to `seen`, never create a `files` row).
- [x] 1.3 After the walk, emit one batched `WARNING` (`cairn.scanner`) naming the count and the
  sampled raw-byte paths when any were skipped, so the operator can locate them.

## 2. Guarantee a terminal run state (scanner)
- [x] 2.1 In `scan_corpus`'s scan-body `except Exception`, log the exception and
  `await session.rollback()` so a pending-rollback session cannot block finalizing the run.
- [x] 2.2 Wrap the finalizing `await session.commit()`: on failure, `rollback()` then force the run
  terminal with a direct `UPDATE runs SET result='error', finished=<now> WHERE id=<run.id>` and
  commit — a scan never leaves its run at `running`.

## 3. Tests & verification
- [x] 3.1 Unit: a corpus containing one non-UTF-8 filename (created via a bytes path,
  e.g. `b"1\xe0.jpg"`) scans to completion with `result='partial'`, the storable files are tracked
  normally, and **no** `files` row exists for the bad name.
- [x] 3.2 Unit: re-scanning the same corpus is stable — the bad file does not produce `missing`/
  `added` churn, no new run is left `running`, and storable files stay `ok`.
- [x] 3.3 Unit: a forced failure in the scan body (monkeypatch a commit/flush to raise mid-walk)
  finalizes the run to `result='error'` with `finished` set — the run is never left `running`.
- [x] 3.4 Run the scanner suite: `PYTHONPATH=. .venv/bin/pytest tests/test_scanner.py -q`.
- [x] 3.5 `openspec validate tolerate-unencodable-paths --strict` passes.

## 4. Operational note (no migration)
- [x] 4.1 Confirm no Alembic revision is needed (no schema change) and that the existing startup
  reaper (`scheduler.reap_orphaned_runs`) clears the currently-wedged Photos run on deploy/restart;
  record this in the change's CLAUDE.md note at archive time.
