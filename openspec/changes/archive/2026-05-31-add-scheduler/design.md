## Context

The obsidian_mcp indexer pattern — "run on startup, then on a cadence" as a lifespan background
task — is exactly our scheduler shape, but per-corpus rather than one global vault, and with a
second daily job (OTS upgrade). The scanner is the single writer, so the loop must serialize
scans (never two at once).

## Decisions

### D1 — One async loop, sequential scans, per-corpus next-due
A single `asyncio.Task` started in the lifespan runs a tick loop (`scan_interval_seconds`, default
30 s). It keeps an in-memory `next_due[corpus_id]` (monotonic). Each tick: load corpora, scan
those whose `next_due` has passed **sequentially**, then set `next_due = now + cadence`. On
startup every corpus is due immediately but processed one at a time; their first runs are offset
by a small per-index stagger so a large fleet doesn't thunder. Pure helper `due_corpora(corpora,
next_due, now)` is unit-tested without the loop.

### D2 — Daily upgrade pass
The loop tracks `last_upgrade` (monotonic). When `now - last_upgrade ≥ upgrade_interval_seconds`
(default 86400) it runs `proofs.upgrade_incomplete()` across all corpora, records the count, and
resets the timer. Runs on the first tick too (so a freshly-started instance upgrades any backlog).

### D3 — Freshness model and `/healthz` status codes
`compute_health(session, settings)` returns, per corpus: the age of its newest successful
(`ok`/`partial`) run, and a state — `fresh` (run within `max(2×cadence, floor)`), `pending` (no
successful run yet but created within the same window — startup grace), or `stale` (otherwise).
Overall: `error` if the datastore is unreachable, else `degraded` if any corpus is `stale`, else
`ok`. `/healthz` maps `ok → 200`, `degraded → 503`, `error → 503`. Returning 503 on staleness is
deliberate: a plain HTTP monitor then works as a dead-man's switch without keyword inspection.
`floor` = `health_freshness_floor_seconds` (default 900) keeps fast-cadence corpora from flapping.

### D4 — `app-runtime` `/healthz` requirement is tightened (MODIFIED)
The foundation allowed `/healthz` freshness to be a stub. This change replaces that requirement so
200 now means "reachable AND fresh" and 503 covers both "unreachable" and "stale". Liveness alone
is no longer "healthy" — the endpoint's whole purpose is scan freshness.

### D5 — Disable switch for cron-only deployments
`scheduler_enabled` (default true). When false the lifespan does not start the loop — for
deployments that drive `cairn scan --once` from system cron instead of the in-process scheduler.
`/healthz` freshness still works (it reads `runs` regardless of who writes them).

### D6 — Clean lifecycle
`start(app)` creates the task and stores it on `app.state`; `stop(app)` sets a stop `asyncio.Event`
and awaits the task with a timeout, then cancels if needed. Each scan/upgrade is wrapped so an
exception is logged and the loop continues (the dead-man's switch surfaces a corpus that stops
producing successful runs).

## Risks / Trade-offs

- **A long scan blocks the tick**: sequential scanning means a multi-hour 1.4 TiB scan delays
  other corpora's scans. Acceptable for the staggered cadences in scope (photos nightly, docs
  every 15 min); a future refinement could bound scan time or run a low-priority deep-scan lane.
- **In-memory schedule resets on restart**: `next_due` is not persisted, so a restart re-scans
  everything once. Cheap fast-path (size+mtime) makes a redundant scan inexpensive; freshness is
  derived from the persisted `runs` table, not the in-memory state.
- **503 on staleness can be noisy** if cadences are mis-set; the `floor` and the 2× multiplier
  give headroom, and `scheduler_enabled=false` deployments can rely on the JSON body instead.
