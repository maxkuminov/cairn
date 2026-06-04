# Tasks — content-addressed move/rename detection

## 1. Schema & migration
- [x] 1.1 In `src/models/db.py`, add `moved` to the `events.kind` CheckConstraint and add a nullable
  `detail` TEXT column to `events` (records old → new path for moves).
- [x] 1.2 In `src/models/db.py`, add an integer `moved` count column (default 0, not null) to `runs`.
- [x] 1.3 Generate an Alembic revision using **SQLite batch mode** (table rebuild) to alter the
  `events.kind` CHECK constraint and add the new columns. Confirm `alembic upgrade head` then
  `alembic downgrade base` round-trips cleanly on a fresh DB.

## 2. Reconciliation pass (scanner)
- [x] 2.1 In `src/services/scanner.py`, after the missing-sweep, build in-memory indexes of the rows
  newly marked `missing` and the rows newly `added` this run, keyed by `(sha256, size)`.
- [x] 2.2 Reconcile each `missing` row whose key matches exactly one `added` row and is unique across
  both sets (skip `size == 0`): delete the `added` row, repoint the `missing` row's `relpath` to the
  new path, set `status='ok'`, refresh `last_checked`, preserve `first_seen`/`sha256`/`ots_*`.
- [x] 2.3 Emit one `moved` event (`file_id` = surviving row, `detail` = old → new path) in place of
  the `missing` + `added` events; adjust the run summary so reconciled moves are excluded from the
  `missing`/`added` counts and `runs.moved` is incremented.
- [x] 2.4 Log ambiguous / multi-match fallbacks at INFO (why a reorganization still produced
  `missing` + `added`).

## 3. Downstream wiring
- [x] 3.1 Stamp pass: confirm reconciled moves are not re-queued (they are `ok`, not `pending`).
- [x] 3.2 `src/notify/`: `moved` is informational — never routed as an alarm.
- [x] 3.3 `src/control_panel/`: render `moved` events (old → new path) in the feed and surface the
  per-run `moved` count on the dashboard.

## 4. Tests & verification
- [x] 4.1 Unit: a 1:1 move → one `moved` event, no `missing`/`added`; surviving row keeps
  `first_seen` + `ots_path`, status `ok`.
- [x] 4.2 Unit: ambiguous case (two files share content, one moves) → no reconciliation, falls back
  to `missing` + `added`, logged.
- [x] 4.3 Unit (worm): a move raises NO alert; a genuine deletion still does.
- [x] 4.4 Smoke (OTS CLI integration): in a `perfile` corpus, move an already-stamped file; confirm
  it is NOT re-stamped (proof reused) and that `ots verify` still passes against the carried-forward
  `.ots`.
- [x] 4.5 Migration round-trip: `alembic upgrade head` / `downgrade base` on a fresh DB; existing
  rows untouched by the upgrade.
- [x] 4.6 `openspec validate add-rename-detection --strict` passes.
