## Context

The corpus detail page (DESIGN.md §5) renders files as a flat, server-side-paginated list
(`query_files` → `partials/file_table.html`, 50 rows/page). That works for documents but is unusable
for a 100k+ file photo corpus: there is no way to drill into a folder. Separately, long-running
per-corpus work is invisible: `_corpus_view` queries only the last *completed* scan, and the manual
`POST /corpus/{id}/scan` and `POST /corpus/{id}/stamp-all` both run **synchronously inside the
request** then return — so a long scan/backfill blocks the HTTP request (OAuth-proxy/Traefik timeout
risk) and no live status ever shows.

Cairn already runs three distinct per-corpus background operations, recorded inconsistently:
- **Integrity scan** (`scanner.scan_corpus`) — walk → diff → hash → classify. Inserts a
  `Run(result="running")` and commits every `BATCH` (500) files via `_drain()`, so an in-flight scan
  is already observable; it just lacks a persisted progress number and any panel reading it. The
  auto-stamp of new/changed files runs as the tail of this same run.
- **On-demand "Stamp all"** (`proofs.mark_unstamped_pending` + `stamp_pending`) — the baseline
  backfill. Runs synchronously in the request and creates **no** run row.
- **Daily OTS upgrade** (`scheduler.run_daily_upgrade` → `proofs.upgrade_incomplete`) — deliberately
  creates **no** run row, instead updating the latest run's `upgraded` count, because
  `compute_health` derives the dead-man's switch from the newest run and a phantom upgrade "run"
  would falsely refresh freshness.

Two enabling facts: **`relpath` already encodes the tree** (every directory level is derivable in
SQL — no new tables, no write-path work), and a **run row is already the natural place to carry
operation state**. Constraints (DESIGN.md §3): SQLite single-writer (the scanner) with WAL for
concurrent reads; server-side paging is mandatory; watched roots are read-only.

## Goals / Non-Goals

**Goals:**
- A lazy, drill-down folder tree on the corpus page, one directory level per request, toggleable with
  the existing flat list (retained as-is).
- One typed run record per operation (`scan` / `stamp` / `upgrade`) carrying live progress, surfaced
  as a labelled, auto-polling badge on the dashboard card and corpus status pill.
- Make "Scan now" and "Stamp all" asynchronous, with no two operations on one corpus at once.
- Keep `/healthz` freshness keyed on scans only, so typed stamp/upgrade runs don't perturb it.

**Non-Goals:**
- A client-side tree, full-set load, websocket/SSE per-file streaming, or a materialized-path table.
- A separate run for the auto-stamp tail of a scan (it stays in the scan run).
- Exact percentage on a first-ever scan (no baseline → indeterminate); file ops from the tree.

## Decisions

### 1. Derive the tree from `relpath` in SQL — no schema for the tree

`browse_tree(session, corpus_id, prefix)` returns the contents of one directory level under `prefix`
(`""` = corpus root, else e.g. `"2024/jan/"`):
- **Files directly at this level**: rows where `relpath LIKE :prefix || '%'` and the remainder after
  the prefix contains no `/` — `instr(substr(relpath, :plen+1), '/') = 0`.
- **Immediate subfolders**: for rows whose remainder *does* contain a `/`, the first segment
  `substr(remainder, 1, instr(remainder,'/')-1)`, `GROUP BY` that segment for each subfolder's file
  count and a status roll-up (`SUM(CASE WHEN status IN ('modified','missing') THEN 1 END)` → the
  issue dot).

`LIKE :prefix || '%'` is an **anchored prefix** (no leading wildcard), so SQLite range-scans the
`(corpus_id, relpath)` unique index over just that subtree. Files at a level reuse the existing
paginated `query_files` path scoped to the prefix, so a single huge folder still pages 50 at a time.

*Alternatives.* A maintained `directories` table — rejected (write-path complexity + migration for
data implied by `relpath`); kept as a fallback if root-level aggregation ever gets slow. Building the
tree from a full `relpath` fetch in Python — rejected (violates "never materialize the full set").
Recursive CTE — unnecessary; we only fetch one level per request (lazy expand).

### 2. A run becomes a typed, progress-bearing record (Alembic `0006`)

Add to `runs`:
- `kind TEXT NOT NULL DEFAULT 'scan'`, CHECK `kind IN ('scan','stamp','upgrade')`. (Adding a CHECK on
  SQLite needs a `batch_alter_table` table rebuild — same pattern migration `0005` used.) Existing
  rows backfill to `'scan'`.
- `processed INTEGER NOT NULL DEFAULT 0` — items handled so far (files walked / files stamped /
  proofs upgraded), updated as the operation runs.
- `total INTEGER NULL` — the planned denominator; **NULL = unknown** (indeterminate badge).

The numerator/denominator are interpreted uniformly by the badge: `total` set → bar =
`min(99, floor(100·processed/total))` (capped <100 so it never reads "done" early); `total` NULL →
pulsing indeterminate badge with elapsed + running count.

Per kind:
- **scan**: at start set `total =` the last completed `kind='scan'` run's `processed` (the best
  estimate of how many files this walk will cover), or NULL if none. The scanner already keeps a
  local `processed` counter and commits each `BATCH`; set `run.processed = processed` at each
  `_drain()` and at finish. We deliberately do *not* use live `count(*) FROM files` as the
  denominator (a first scan inserts rows as it walks, so `processed ≈ file_count` → a false ~100%).
  First scan, and the first scan after this migration (older runs have `processed = 0` → estimate 0
  → NULL), are gracefully indeterminate.
- **stamp**: `total =` the count queued by `mark_unstamped_pending` (known up front) → **exact** bar;
  `processed` advances per stamped batch.
- **upgrade**: `total =` the count of incomplete proofs to process (known up front) → **exact** bar.

### 3. "Scan now" and "Stamp all" run in the background, one op per corpus

A small `run_operation(corpus_id, op)` helper opens its **own** `AsyncSession` from the sessionmaker
(the request session closes with the response) and runs the operation, fired via
`asyncio.create_task` with a module-level reference set so it isn't GC'd mid-flight. The routes call
it and return immediately — an htmx fragment that swaps the status pill to the in-progress badge
(which then polls).

**Concurrency guard (SQLite single-writer).** Before launching, check for any existing
`result='running'` run for that corpus; if present, refuse and report "an operation is already
running" rather than starting a second writer. The scheduler reuses the same check so a scheduled
scan/upgrade skips a corpus that already has an op in flight (and vice-versa). WAL would serialize an
unguarded overlap safely, but we prevent it rather than rely on lock contention.

**Stale-run reaper.** A crash mid-operation orphans a `running` row (`finished` NULL forever),
freezing the badge and blocking future ops. On app startup (FastAPI lifespan) mark any leftover
`result='running'` run as `error` with `finished=now` — a restarted process cannot still be running
it. Robust, and preferred over a "running but `started` is old" heuristic in the poll path.

### 4. Freshness keys on `kind='scan'` only

`compute_health` (and `_corpus_view`'s "last scan") change from "newest run with result in
(ok,partial)" to "newest **`kind='scan'`** run with result in (ok,partial)". This is what lets the
daily upgrade pass finally record a real `kind='upgrade'` run (with live progress) instead of the
current "update the latest run's `upgraded`" workaround — the dead-man's switch ignores non-scan
kinds. The upgrade pass only creates a run for a corpus that actually has incomplete proofs to work
(no empty daily runs).

### 5. Auto-poll that stops itself; tree/list toggle

`GET /corpus/{id}/op-status` returns the status fragment. **While a run is in flight** the fragment
carries `hx-trigger="every 4s"`; when no run is in flight it renders the resting status pill
**without** the trigger, so polling halts automatically — an idle corpus never polls. On the
running→done transition the response sends an `HX-Trigger` header so the corpus page refreshes its
stat row + file view once. The dashboard card uses the same endpoint/fragment, and only cards with a
running op carry the polling trigger.

The corpus page defaults to the **tree** view; a `[Tree] [List]` toggle swaps the browser region via
htmx. The list view is `file_table.html` unchanged. Tree expansion is per-folder
`hx-get /corpus/{id}/tree?prefix=…` that swaps in children — no client state machine, no full reload.

## Risks / Trade-offs

- **Root-level expand of a huge corpus scans the whole `relpath` index once** → it's a single
  anchored index range-scan with SQLite-side aggregation (only grouped subfolders + one page of files
  reach Python); acceptable at ~186k. Fallback: a `directories` roll-up table (out of scope).
- **Scan progress denominator is an estimate** → folders added/removed since the last scan skew the
  bar; the 99% cap and resolve-to-real-status-on-finish keep it honest; indeterminate covers
  no-baseline. (Stamp/upgrade totals are exact.)
- **Freshness redefinition could regress the dead-man's switch** → mitigated by keying strictly on
  `kind='scan'` and backfilling all existing runs to `kind='scan'`, so behavior is identical for
  scans; only the new stamp/upgrade kinds are excluded (which is the point).
- **Background op loses the request's error surface** (the sync versions swallowed errors too) → the
  run row records `result='error'`; the badge resolves to the real status; the reaper prevents a
  stuck badge.
- **Two writers (manual + scheduled) on one SQLite file** → the `running`-run guard on both entry
  points prevents it.
- **`asyncio.create_task` not awaited by the request** → keep a module-level reference so the task
  isn't GC'd; it owns its session/commit/close.

## Migration Plan

1. Alembic `0006` (`batch_alter_table` rebuild of `runs`): add `kind` (default `'scan'`, CHECK
   `IN ('scan','stamp','upgrade')`), `processed` (default 0), `total` (nullable). Backfill existing
   rows to `kind='scan'`. Additive + backward-compatible; older code ignores the new columns.
2. Ship code; `make deploy` then `make migrate` (a revision was added — per CLAUDE.md flow).
3. **Rollback**: the columns are additive and unused by older code; reverting the app needs no data
   change. The Alembic `downgrade` drops the three columns (SQLite batch rebuild) if ever required.

## Open Questions

- Poll cadence (4s) and whether the dashboard polls each card vs. only running ones — default: render
  the polling trigger only on cards/pills with a running op, so idle costs nothing.
- Whether to deep-link the expanded tree prefix in the URL for shareable navigation — deferred
  (nice-to-have; not required by the specs).
