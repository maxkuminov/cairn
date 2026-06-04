## 1. Schema & migration (typed, progress-bearing runs)

- [x] 1.1 Add `Run.kind` (`scan|stamp|upgrade`, default `scan`, NOT NULL), `Run.processed` (int, default 0), and `Run.total` (nullable int) to `src/models/db.py`, with a `CheckConstraint` on `kind`.
- [x] 1.2 Write Alembic `0006` using `batch_alter_table` (SQLite table rebuild, as in `0005`): add the three columns + the `kind` CHECK, backfill existing rows to `kind='scan'`. Provide a `downgrade` dropping the columns.
- [x] 1.3 Run `alembic upgrade head` on a scratch DB and confirm the columns/CHECK exist and existing runs read `kind='scan'`.

## 2. Run lifecycle: progress + freshness + reaper

- [x] 2.1 `scanner.scan_corpus`: set the new run `kind='scan'`; at start set `run.total` = the last completed `kind='scan'` run's `processed` (else leave NULL); write `run.processed = processed` at each `_drain()` and at finish.
- [x] 2.2 Add a shared "is an operation in progress for this corpus?" check (a `running` run exists) and an `active_run(session, corpus_id)` helper in `src/services/corpora.py` (or scanner) reused by routes + scheduler.
- [x] 2.3 Freshness: change `scheduler.compute_health` and `routes._corpus_view`'s "last scan" lookup to filter `Run.kind == 'scan'` (keep result in `('ok','partial')`).
- [x] 2.4 Startup reaper: in the app lifespan, mark any leftover `result='running'` run as `error` with `finished=now` before the scheduler starts.

## 3. Stamp & upgrade as typed runs

- [x] 3.1 Wrap the on-demand stamp backfill so it creates a `kind='stamp'` run: `total` = the count `mark_unstamped_pending` queued, `processed` advanced per stamped batch in `proofs.stamp_pending` (thread a progress callback or update the run between batches), terminal result + `finished` at the end.
- [x] 3.2 `scheduler.run_daily_upgrade`: for a corpus with incomplete proofs, create a `kind='upgrade'` run (`total` = incomplete count, `processed` advanced as proofs upgrade); record nothing for a corpus with no work. Remove the old "update the latest run's `upgraded`" workaround.
- [x] 3.3 Make the scheduler skip a corpus that already has a `running` run (reuse 2.2) for both the scan pass and the upgrade pass.

## 4. Async, guarded operations from the panel

- [x] 4.1 Add a `run_operation(corpus_id, op)` runner that opens its own `AsyncSession`, runs the op, and is fired via `asyncio.create_task` with a module-level task reference (so it isn't GC'd).
- [x] 4.2 `POST /corpus/{id}/scan`: refuse if an operation is in progress (report "already running"); else launch the scan in the background and return immediately (htmx fragment swapping in the in-progress badge).
- [x] 4.3 `POST /corpus/{id}/stamp-all`: same async + guard treatment; keep the `perfile`-only and owner/admin scoping.

## 5. Folder-tree browser

- [x] 5.1 `corpora.browse_tree(session, corpus_id, prefix)`: one directory level from `relpath` via anchored `LIKE :prefix||'%'` — immediate files (no `/` in the remainder) and grouped subfolders (first segment) with file count + issue roll-up (`SUM(CASE WHEN status IN ('modified','missing') ...)`).
- [x] 5.2 `GET /corpus/{id}/tree?prefix=…` returning `partials/file_tree.html` (a folder's subfolders + its directly-contained files; files paginated when over one page, reusing `query_files` scoped to the prefix).
- [x] 5.3 `partials/file_tree.html`: expandable folder rows (`hx-get` the child level, swap in), file rows reusing the existing row markup, issue indicator on folders.
- [x] 5.4 `corpus_detail.html`: add a `[Tree] [List]` toggle; default to the tree view; keep `file_table.html` (List) unchanged.

## 6. Live operation-status badge

- [x] 6.1 `_corpus_view` (+ a small `op_status` context): report any `running` run with kind, `processed`, `total`, started time → label + percentage (`min(99, floor(100*processed/total))` when `total`, else indeterminate).
- [x] 6.2 `partials/op_status.html`: labelled badge ("Scanning…/Stamping…/Upgrading proofs…") + progress bar/indeterminate pulse + started-ago + deep marker for scans; carries `hx-trigger="every 4s"` only while a run is in progress.
- [x] 6.3 `GET /corpus/{id}/op-status` returns the fragment; on a running→done transition send `HX-Trigger` so the corpus page refreshes its stat row + file view once.
- [x] 6.4 Render the badge on `partials/_corpus_card.html` (dashboard) and the corpus detail status pill, with polling only on cards/pills that currently have a running op.
- [x] 6.5 `panel.css`: tree rows/indent/caret, progress bar, scanning pulse.

## 7. Tests

- [x] 7.1 Migration `0006` round-trips (upgrade/downgrade) and backfills `kind='scan'`.
- [x] 7.2 `browse_tree`: nested paths yield correct subfolders/files at root and a sub-prefix; folder counts + issue roll-up correct; no full-set materialization.
- [x] 7.3 Run progress: a scan writes growing `processed` and a `total` estimate from the prior scan; first-ever scan leaves `total` NULL.
- [x] 7.4 Stamp/upgrade typed runs: stamp-all creates a `kind='stamp'` run with `total`=queued; upgrade pass creates a `kind='upgrade'` run only when there is work; neither affects `/healthz` freshness.
- [x] 7.5 Concurrency guard + reaper: a second op on a corpus with a `running` run is refused; the scheduler skips an in-flight corpus; startup marks a leftover `running` run `error`.
- [x] 7.6 Routes: `scan`/`stamp-all` return immediately (don't block on the op); `op-status` returns the polling fragment while running and the static pill when idle.

## 8. Verify, deploy & archive

- [x] 8.1 Run the test suite + `openspec validate --strict`; manually exercise tree expand, the toggle, and the live badge on a large local corpus.
- [x] 8.2 Commit + push, `make deploy`, then `make migrate` (revision `0006` was added); verify with `make status` / `/healthz`.
- [x] 8.3 Update `CLAUDE.md` working notes (typed runs + tree browser + live status) and archive the change via the OpenSpec archive flow.
