## 1. Config

- [x] 1.1 Add settings: `scan_interval_seconds` (default 30), `upgrade_interval_seconds` (default 86400), `health_freshness_floor_seconds` (default 900), `scheduler_enabled` (default True). Document in `.env.example`/`config.example.yaml`.

## 2. Scheduler service (`src/services/scheduler.py`)

- [x] 2.1 Pure helper `due_corpora(corpora, next_due: dict[int,float], now: float) -> list` returning corpora whose `next_due` (default 0 = due now) ≤ now, in a stable order.
- [x] 2.2 `compute_health(session, settings) -> HealthReport`: per corpus, newest successful (`ok`/`partial`) run age + state (`fresh`/`pending`/`stale`) using threshold `max(2*cadence, floor)`; overall `ok`/`degraded` (datastore reachability handled by the caller → `error`). `HealthReport` carries overall status + per-corpus rows (name, last_scan_age_seconds, state).
- [x] 2.3 `run_due_scans(session, next_due, now)`: scan due corpora sequentially via `scanner.scan_corpus` (which already handles perfile stamping), update `next_due[id]=now+cadence`; catch & log per-corpus errors.
- [x] 2.4 `run_daily_upgrade(session)`: call `proofs.upgrade_incomplete()` across corpora; create/append a per-corpus or summary record so `runs.upgraded` reflects the pass (or set `upgraded` on the corpus's latest run / a dedicated upgrade run row — pick one and document).
- [x] 2.5 `scheduler_loop(app, stop_event)`: startup scan-all + daily upgrade, then tick every `scan_interval_seconds` doing due scans + (when interval elapsed) the upgrade pass; honor `stop_event`; never crash on a single iteration error.

## 3. Lifespan + `/healthz` wiring (`src/main.py`)

- [x] 3.1 Implement `start_scheduler(app)` / `stop_scheduler(app)`: when `scheduler_enabled`, create the loop task + stop `asyncio.Event` on `app.state`; on stop, signal + await with timeout, then cancel.
- [x] 3.2 Rework `/healthz`: 503 `status:error` if datastore unreachable; else `compute_health` → 200 `status:ok` when no corpus stale, 503 `status:degraded` when any stale; include `mode`, `version`, and the per-corpus freshness list in the body.

## 4. Verification

- [x] 4.1 `tests/test_scheduler.py`: `compute_health` returns fresh (recent run) → ok/200; stale (old/no run past grace) → degraded/503; pending (new corpus, no run, within grace) → ok. `due_corpora` selection honors next_due. The daily upgrade triggers `upgrade_incomplete`. A short `scheduler_loop` smoke (tiny `scan_interval`, temp corpus, `ots` mocked): start → poll until a `runs` row appears → set stop_event → assert clean shutdown.
- [x] 4.2 An integration check via TestClient: with a stale corpus, `GET /healthz` returns 503 `degraded`; with a fresh corpus, 200 `ok`.
- [x] 4.3 `openspec validate add-scheduler --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier`; resolve drift. Update `CLAUDE.md`. Archive.
