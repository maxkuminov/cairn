## 1. Schema + migration

- [x] 1.1 In `src/models/db.py`, extend the `ck_runs_result` CheckConstraint on `Run` from
  `result in ('ok','error','partial','running')` to also allow `'interrupted'`.
- [x] 1.2 Add Alembic migration `0007_interrupted_run_result` (down_revision `0006`): in
  `upgrade`, `op.batch_alter_table('runs')` recreating `ck_runs_result` with the extended value set
  (SQLite batch rebuild, same pattern as `0005`). In `downgrade`, first `UPDATE runs SET
  result='error' WHERE result='interrupted'`, then recreate the original CHECK.

## 2. Reaper

- [x] 2.1 In `src/services/scheduler.py`, change `reap_orphaned_runs` to set `result='interrupted'`
  (keep `finished=_utcnow()`); update its docstring to say `interrupted`.

## 3. Tests

- [x] 3.1 Update `tests/test_folder_tree_and_progress.py::test_reaper_marks_orphaned_running_runs_error`
  to assert reaped runs are `interrupted` (rename the test to `_interrupted`); keep the assertions
  that no run stays `running` and that reaped runs get a `finished` timestamp; leave the pre-existing
  `ok` run untouched.

## 4. Verify

- [x] 4.1 `openspec validate reconcile-interrupted-runs --strict`.
- [x] 4.2 `npm run check` (or the repo's lint/format) and run the Python test suite for
  `tests/test_folder_tree_and_progress.py` + `tests/test_scheduler.py`.
- [x] 4.3 Apply the migration locally (`alembic upgrade head`) and confirm it round-trips
  (`downgrade -1` then `upgrade head`).
