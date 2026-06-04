## Why

Two gaps in the corpus detail page (DESIGN.md §5 — "corpus detail · file list with status") hurt
exactly the corpora Cairn exists for: large ones. (1) The file browser is a **flat, paginated
list**. For a photo corpus of 100k+ files (DESIGN.md §1, §5 cite ~186k) finding one file or folder
by paging 50 rows at a time is hopeless — there is no way to drill into a directory. (2) Long-running
per-corpus work is **invisible** in the panel: the status pill reflects only the last *completed*
scan, so the user cannot tell a corpus is being re-indexed right now or how far along it is. And
"per-corpus work" is not one thing — Cairn runs **distinct background operations** (DESIGN.md §5
per-run flow + scheduler): an integrity **scan** (walk → hash → verify), OTS **stamping** (the
on-demand backfill of a baseline), and the daily OTS **upgrade** pass. Today the scan and stamp-all
both run **synchronously inside the HTTP request** (a 100k-file scan or backfill blocks it, risking
an OAuth-proxy/Traefik timeout) and none of the three surface any live status.

## What Changes

- **Folder-tree browser with a tree ⇄ list toggle.** The corpus detail page gains a lazy,
  drill-down **folder tree** (default) alongside the existing flat list (one click away). The tree
  derives directory structure from `relpath` entirely server-side — one directory level per request,
  with per-folder file counts and a roll-up issue indicator. It never materializes the full set
  (DESIGN.md §3, §5 — server-side paging is mandatory; the DB is the index). The existing flat list —
  with its sort / pagination / notarization-date columns — is preserved as the "List" view.
- **A run is a typed, progress-bearing operation record.** The `runs` row gains `kind`
  (`scan` | `stamp` | `upgrade`), a `processed` counter, and a nullable `total` estimate.
  (**Migration**: adds `runs.kind`, `runs.processed`, `runs.total`; existing rows backfill to
  `kind='scan'`.) An integrity scan is `kind='scan'`; the on-demand stamp backfill is `kind='stamp'`;
  the daily upgrade pass records `kind='upgrade'`. Stamp and upgrade know their total up front
  (queued / incomplete counts) → **exact** progress; a scan's total is an estimate from the prior
  scan (first-ever scan → indeterminate).
- **Live, labelled operation status, auto-polled.** The dashboard corpus card and the corpus detail
  status pill detect an in-flight run and render a badge **labelled by kind** — "Scanning… 42,310 /
  118,540 (36%)", "Stamping… 8,000 / 23,114", "Upgrading proofs… 120 / 540" — with a progress bar,
  started-ago, and (for scans) a deep-pass marker. While a run is in flight the badge htmx-polls
  (every few seconds) and **stops on its own** when it finishes; an idle corpus does no polling.
- **"Scan now" and "Stamp all" become asynchronous. (BREAKING — request contract)** Both kick off in
  the background with their own DB session and return immediately, so the UI shows the live badge
  instead of blocking. **No two operations run on the same corpus at once** (SQLite is single-writer):
  a scan/stamp is refused if any run is in progress for that corpus, and the scheduler skips a corpus
  that already has an in-flight operation.
- **Freshness keys on scans only.** `/healthz` and the panel's "last scan" derive freshness from the
  newest **`kind='scan'`** run, so a stamp/upgrade run can now be recorded without falsely refreshing
  the dead-man's switch (today the upgrade pass avoids creating a run for exactly this reason).
- **Orphaned-run reaper.** On startup any run left `running` (a crash mid-operation) is marked
  `error`, so a stale badge never sticks and a new operation is never blocked.

## Capabilities

### New Capabilities
<!-- none — every feature extends an existing capability -->

### Modified Capabilities
- `web-panel`: the corpus file-list requirement gains a server-side folder-tree view + tree/list
  toggle; the scan-action and stamp-all requirements make those actions asynchronous; a new
  requirement surfaces live, auto-polling operation status (labelled scan/stamp/upgrade) on the
  dashboard card and corpus view.
- `integrity-scanning`: "Each scan records a run" is extended — a run carries `kind`, a live
  `processed` count, and a `total` estimate; plus an orphaned-running-run reaper on startup.
- `ots-notarization`: stamping and upgrading record their own typed runs (`stamp` / `upgrade`) with
  exact progress, without affecting scan freshness.
- `scan-scheduling`: the daily upgrade pass records an `upgrade` run (replacing the "update the
  latest run" workaround); the scheduler skips a corpus with an operation already in flight.
- `app-runtime`: `/healthz` freshness is defined against the newest **scan** run, not any run.

## Impact

- **Code**: `src/models/db.py` (`Run.kind` / `Run.processed` / `Run.total` + CHECK);
  `alembic/versions/0006_*` (add the three columns, batch rebuild for the `kind` CHECK, backfill
  `kind='scan'`); `src/services/scanner.py` (write `processed`/`total` as it walks; set the scan
  estimate); `src/services/proofs.py` + the stamp/upgrade callers (create typed runs with progress);
  `src/services/scheduler.py` (upgrade-pass runs; skip in-flight corpora; freshness keyed on
  `kind='scan'`); `src/services/corpora.py` (`browse_tree(corpus_id, prefix)`; `active_run` /
  progress helper); `src/control_panel/routes.py` (async + guarded `POST .../scan` and
  `.../stamp-all`; new `GET .../tree` and `GET .../op-status` poll fragments; `_corpus_view` reports
  any running run + filters "last scan" to `kind='scan'`); `src/control_panel/templates/` (new
  `partials/file_tree.html`, `partials/op_status.html`; tree/list toggle; card badge); `panel.css`
  (tree rows, progress bar, scanning pulse); app lifespan (startup reaper).
- **Data**: three additive `runs` columns → **Alembic `0006`**, so `make migrate` runs after deploy.
  No change to `files` / `corpora` / `events`.
- **Dependencies / config**: none. Reuses the existing htmx idiom; no new libs.
- **Behaviour**: "Scan now" and "Stamp all" no longer block the request (run in background); a second
  concurrent operation on one corpus is refused; the daily upgrade pass now leaves an `upgrade` run
  record (scan freshness unchanged). Tree/list browsing and the badge are read-only.

## Non-goals

- **No client-side tree or full-set load.** The tree is fetched one directory level at a time
  server-side; the full file set is never shipped to the browser (DESIGN.md §3, §5).
- **No per-file real-time streaming.** Progress is the batch-granular `processed` counter already
  committed per batch — not a websocket/SSE per-file feed.
- **No exact percentage on a first-ever scan.** With no prior baseline the denominator is unknown, so
  a first scan shows an indeterminate badge (no bar) until a completed scan exists. (Stamp/upgrade
  always know their total, so they are always exact.)
- **No separate run for the auto-stamp tail of a scan** — that stays part of the `kind='scan'` run;
  the `stamp` kind is the on-demand backfill.
- **No new sort/filter semantics, no schema change to `files`,** and no change to scan
  classification, accept, the scheduler's cadence/cost ordering, or stamping/upgrade mechanics.
- **No file operations from the tree** (no move/delete/download) — watched roots are read-only
  (DESIGN.md §3). The tree is browse-only.
