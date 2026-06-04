## Why

The startup orphaned-run reaper (DESIGN.md §5) marks every run left at `result = 'running'` by a
killed/crashed process as terminal — but it uses `result = 'error'`, conflating a *benign*
interruption (a routine `make deploy` killing the long Photos scan mid-flight) with a *genuine*
scan failure (an I/O/permission error the operator should investigate). For an integrity-monitoring
tool, that distinction matters: every deploy leaves `error` runs in history that can mask a real
error. A distinct `interrupted` terminal state separates "the process was restarted" from "the scan
went wrong". The reaper is committed but not yet deployed, so adding the state now means the live
system never has to relabel `error` rows later.

## What Changes

- The startup reaper marks orphaned `running` runs as **`interrupted`** (with `finished` set)
  instead of `error`. Behavior is otherwise unchanged: it still clears the stale in-progress
  indicator and unblocks the corpus's concurrency guard.
- `interrupted` becomes a new allowed value of the `runs.result` CHECK constraint
  (`ck_runs_result`), extending `('ok','error','partial','running')` → `(…,'interrupted')`. A
  normal scan still only ever finishes `ok`/`partial`/`error`; `interrupted` is reaper-only.
- New Alembic migration (SQLite batch-rebuild of the CHECK, same pattern as `0005`).
- No UI change: the terminal `result` value is not rendered as a badge anywhere (the resting status
  pill derives from file counts; the live badge from run progress), so nothing needs to learn the
  new value.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `integrity-scanning`: the "Orphaned running runs are reconciled on startup" requirement changes
  its terminal state from `error` to `interrupted` (and `interrupted` joins the set of allowed
  `runs.result` values).

## Impact

- Code: `src/services/scheduler.py` (`reap_orphaned_runs` terminal value), `src/models/db.py`
  (`ck_runs_result` CheckConstraint), new `alembic/versions/0007_*.py`.
- Tests: `tests/test_folder_tree_and_progress.py` (reaper asserts `interrupted`).
- Spec: `openspec/specs/integrity-scanning/spec.md`.
- Data/migration: extends a CHECK constraint; no data rewrite. Freshness reporting is unaffected
  (`compute_health` keys on `ok`/`partial` only).

## Non-goals

- Reconciling dangling runs left by a killed cron `cairn scan --once` in `CAIRN_SCHEDULER_ENABLED=0`
  deployments — the lifespan reaper covers the in-process homelab deploy; a CLI-path reaper is a
  separate follow-up.
- Surfacing `interrupted` in the panel (a badge/run-history view) — the value is for operators
  reading the DB/logs; rendering it is out of scope.
- Changing the concurrency guard, run typing/progress, or normal-scan terminal values.
