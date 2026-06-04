## 1. Schema & migration

- [x] 1.1 Add `Corpus.verify_cadence_seconds` (Integer, default 604800, not null), `Corpus.last_full_scan_at` (DateTime(timezone=True), nullable), and `Run.deep` (Boolean, default False, not null) to `src/models/db.py`.
- [x] 1.2 New Alembic revision `0002_deep_verify` off `0001_initial`: `add_column` for all three with `server_default` on the two NOT NULL columns (`"604800"`, `sa.false()`); `last_full_scan_at` nullable (backfills NULL). Downgrade drops them.
- [x] 1.3 Thread `verify_cadence_seconds` through `services/corpora.create_corpus` and `update_corpus` (default 604800).

## 2. Scanner deep mode (`src/services/scanner.py`)

- [x] 2.1 Add keyword-only `deep: bool = False` to `scan_corpus`; widen the fast-path guard to `deep or row.size != size or row.mtime != mtime or row.sha256 is None`.
- [x] 2.2 Set `run.deep = deep` on the run row. Confirm the OTS re-queue + alarm only fire on an actual byte change (so intact files on a deep pass are not re-stamped). Update the module docstring.

## 3. Scheduler deep gating (`src/services/scheduler.py`)

- [x] 3.1 Pure helper `_deep_owed(corpus, now_wall) -> bool`: `verify_cadence_seconds <= 0` → False; `last_full_scan_at is None` → True; else age ≥ cadence (wall-clock via `_as_aware`).
- [x] 3.2 `run_due_scans`: compute `now_wall` once; for each due corpus decide `deep = _deep_owed(...)` capped to one deep pass per tick; call `scan_corpus(..., deep=deep)`; on success set `corpus.last_full_scan_at = now_wall` and commit. Quick `next_due` gating unchanged.

## 4. Benchmark CLI + corpus form field (`src/cli.py`, panel)

- [x] 4.1 `cairn bench [--path DIR] [--bytes N] [--estimate]`: in-memory SHA-256 throughput probe (default), real-file throughput under `--path`, and per-corpus `size/throughput` estimate under `--estimate`. Reuse `scanner.sha256_file`/`scanner.CHUNK`; read-only.
- [x] 4.2 `--verify-cadence` flag on `cairn add-corpus` (default 604800) → `create_corpus`.
- [x] 4.3 Add a deep-verify cadence selector to `corpus_form.html` (+ "Disabled" / 0) and handle it in `corpus_create`/`corpus_update`; show current value on edit.

## 5. Verification

- [x] 5.1 `tests/test_scanner.py`: deep re-hashes every unchanged file (counter == file count, `modified == 0`); deep detects same-size+same-mtime bit-rot in worm (`modified == 1` + event) while a quick scan misses it; churn deep silently re-baselines; intact perfile deep re-stamps nothing; `Run.deep` True after deep / False after quick.
- [x] 5.2 `tests/test_scheduler.py`: `_deep_owed` truth table (NULL→owed, recent→no, old→yes, 0→never); `run_due_scans` picks deep when owed and persists `last_full_scan_at`; quick when not owed; not persisted when `scan_corpus` raises; one-deep-per-tick cap.
- [x] 5.3 `openspec validate add-deep-verify --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Update `CLAUDE.md`. Archive.
