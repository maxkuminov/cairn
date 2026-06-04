## Why

The scanner and notary work, but today they only run when invoked by hand (`cairn scan` /
`cairn upgrade`). For Cairn to actually *watch* files it needs a background scheduler: scan each
corpus on its own cadence (staggered — you cannot full-rescan 186k files every 5 min for every
corpus at once), run a daily pass to upgrade incomplete OTS proofs once Bitcoin confirms, and
expose scan freshness at `/healthz` so an external monitor (Uptime Kuma) acts as a dead-man's
switch.

References: DESIGN.md §5 (scheduler: per-corpus staggered cadence + daily `ots upgrade` +
heartbeat), design handoff (the heartbeat is now a **poll** model — monitors poll `/healthz`,
which returns scan freshness; the push-heartbeat card was removed).

## What Changes

- **Scheduler service** (`src/services/scheduler.py`): an async background loop started in the
  FastAPI lifespan (replacing the foundation's no-op `start_scheduler` hook). It scans every
  corpus once on startup, then re-scans each on its own `hash_cadence_seconds`, processing due
  corpora sequentially (the scanner is the single writer) and offsetting their first runs so a
  fleet of corpora doesn't all fire at once. Once every ~24h it runs the OTS upgrade pass
  (`proofs.upgrade_incomplete`) across all corpora and records `runs.upgraded`. The loop catches
  per-corpus errors so one bad corpus never kills scheduling, and cancels cleanly on shutdown.
- **Health freshness** (`/healthz`): the endpoint now reports per-corpus scan freshness. A corpus
  is *fresh* if it has a successful run within `max(2 × cadence, floor)`; a corpus with no
  successful run yet is *pending* within a startup grace, then *stale*. `/healthz` returns 200
  `status:ok` when the datastore is reachable and no corpus is stale, **503** `status:degraded`
  when any corpus is stale (the dead-man's switch trips), and 503 `status:error` when the
  datastore is unreachable. The JSON lists each corpus's last-scan age and stale flag.
- **Config**: `scan_interval_seconds` (scheduler tick, default 30), `upgrade_interval_seconds`
  (default 86400), `health_freshness_floor_seconds` (default 900), `scheduler_enabled`
  (default true; lets the CLI/cron-only deployments disable the in-process loop).

### Out of scope (deferred)

- Alerting/notifying when `/healthz` degrades or a proof goes stale — `add-notifiers` (this change
  exposes the freshness query; routing alerts is separate).
- The dashboard's health pill and per-corpus status cards — `add-web-panel` (reads the same data).
- Distributed/multi-process scheduling — single in-process loop is correct for a single-writer
  SQLite tool.

## Capabilities

### New Capabilities

- `scan-scheduling`: a background loop that scans each corpus on its staggered cadence, runs a
  daily OTS upgrade pass, and reports scan freshness for external dead-man's-switch monitoring.

### Modified Capabilities

- `app-runtime`: the `/healthz` requirement is tightened from a liveness stub to also reflect scan
  freshness (503 `degraded` when a corpus is stale), so polling it is a real dead-man's switch.

## Impact

- **Code**: `src/services/scheduler.py` (new), `src/main.py` (lifespan starts/stops the loop;
  `/healthz` calls the freshness snapshot), `src/config.py` (new interval/enable settings).
- **Database**: reads `runs` for freshness; writes `runs.upgraded` during the daily pass. No
  schema change.
- **Tests**: `tests/test_scheduler.py` — freshness classification (fresh/pending/stale → 200/503),
  due-corpus selection + stagger, the daily-upgrade trigger, and a short loop smoke (start →
  observe a run row → stop) with the `ots` subprocess mocked.
