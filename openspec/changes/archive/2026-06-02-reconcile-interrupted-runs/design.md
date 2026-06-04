## Context

The orphaned-run reaper (`scheduler.reap_orphaned_runs`, run once from the app lifespan before the
scheduler starts — `main.py`) already marks every leftover `result='running'` run terminal on boot,
because a fresh process cannot own a run started by a dead one. It currently writes `result='error'`.
The `runs.result` column has a CHECK constraint `ck_runs_result` allowing `('ok','error','partial','running')`.

## Goals

- Give a reaped run a terminal state distinct from a real failure: `interrupted`.
- Keep the change minimal and reversible-by-migration; no data rewrite, no behavior change beyond
  the label.

## Decisions

### Decision: New terminal value `interrupted`, reaper-only
A scan/stamp/upgrade that actually runs to completion still finishes `ok`/`partial`/`error`. Only
the startup reaper produces `interrupted`. This keeps the normal-run state machine untouched and
makes `interrupted` unambiguously mean "a previous process was terminated mid-run".

*Alternative considered:* reuse `error` (zero migration). Rejected — it permanently conflates routine
deploy interruptions with genuine scan errors, defeating the diagnostic value for an integrity tool.

### Decision: Extend the CHECK via a SQLite batch-rebuild migration
SQLite cannot `ALTER` a CHECK in place. Follow the established pattern (migration `0005` rebuilt the
events-kind CHECK): `op.batch_alter_table('runs')` recreating `ck_runs_result` with the extended
value set. `downgrade` first relabels any `interrupted` rows back to `error` (so the narrower
constraint still holds) then restores the original CHECK.

### Decision: No UI change
The terminal `run.result` value is not rendered anywhere — the resting status pill comes from file
counts (`_corpus_status`), the live op badge from run progress (`_op_view`), and the dashboard
"Last activity" tile shows a timestamp + corpus name, never the result string. So no template needs
to learn `interrupted`. (Surfacing it is listed as a non-goal.)

## Risks / Migration

- A deploy that ships this must run the migration (`make migrate`) so the extended CHECK exists
  before the reaper can write `interrupted`; otherwise the reaper's UPDATE would violate the old
  constraint. Migration-before-first-reap is guaranteed because the lifespan runs migrations (when
  `CAIRN_AUTO_MIGRATE`) before the reaper, and `make deploy && make migrate` ordering covers the
  manual path.
- `compute_health` keys freshness on `ok`/`partial` runs only, so `interrupted` (like `error`)
  never refreshes the dead-man's switch — no freshness regression.
