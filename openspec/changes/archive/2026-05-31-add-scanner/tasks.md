## 1. Corpus creation helper

- [x] 1.1 `src/services/corpora.py`: `async create_corpus(session, *, user_id, name, root, mode='worm', ots_mode='none', hash_cadence_seconds=900, exclude_globs=None)` — resolve `root` to an absolute realpath, require it exists and is a directory (else raise a clear error), serialize `exclude_globs` to JSON, insert and return the `Corpus`. Add `list_corpora(session, user_id=None)` and `get_corpus_by_name(session, name, user_id=None)`.
- [x] 1.2 Wire `cairn add-corpus --name --root [--mode worm|churn] [--ots-mode none|perfile] [--cadence SECONDS] [--exclude GLOB ...]` to create a corpus owned by the implicit single-user; print the new corpus id/name/root. (Multi-user scoping + jailing deferred.)

## 2. Scanner core

- [x] 2.1 `src/services/scanner.py`: `sha256_file(path, chunk=1<<20)` streaming hash (run via `asyncio.to_thread`); `iter_relpaths(root, exclude_globs)` yielding POSIX relpaths, skipping excluded globs and not following symlinks outside root.
- [x] 2.2 `async scan_corpus(session, corpus) -> RunSummary`: build FS set + DB set; classify added / present(fast-path size+mtime → hash on diff) / missing / restored per design D1–D5; batch-commit (~500 rows); update `files` (status, size, mtime, sha256, last_checked, last_changed) and append `events`.
- [x] 2.3 Open a `runs` row at start (`result='running'`), finalize with counts + `result` (`ok`/`partial`/`error`). Catch per-file IO/permission errors → count as scan errors, set `partial`/`error`, never crash the loop.
- [x] 2.4 WORM vs churn: worm → `modified` status + unacknowledged `modified` event; churn → silent re-baseline (update hash, status `ok`, no event). `missing` → unacknowledged `missing` event in both modes; `added` → `added` event; reappearing `missing` → `restored` event + status `ok`.

## 3. Accept / re-baseline

- [x] 3.1 `async accept_corpus(session, corpus, user_id)`: in one transaction set `new`/`modified` files → `ok`; delete `missing` file rows; acknowledge all unacknowledged events (`acknowledged_at`, `acknowledged_by`). Return counts. Idempotent.
- [x] 3.2 Wire `cairn accept [--corpus NAME]` (all corpora if omitted) printing what was accepted.

## 4. CLI scan wiring

- [x] 4.1 Implement `cairn scan [--corpus NAME] [--once]`: scan one named corpus or all; print per-corpus summary (added/modified/missing, result). Exit 0 on clean, non-zero only on scan error. (`--once` is the cron-friendly single-pass; continuous mode arrives with the scheduler.)

## 5. Verification

- [x] 5.1 `tests/test_scanner.py` (pytest, temp dir + temp DB): create a corpus over a temp tree; assert first scan classifies files as `added` with a run row; modify a file → `modified` + event (worm); delete a file → `missing` + event; recreate it → `restored`; touch mtime only (same bytes) → stays `ok`, not re-flagged; unchanged file is not re-hashed (assert via a hash-call spy or by leaving sha256 stable with bumped last_checked).
- [x] 5.2 churn-mode test: a modified file silently re-baselines (status `ok`, no unacknowledged event), but a missing file still nags.
- [x] 5.3 accept test: after modifications + a deletion, `accept_corpus` sets files `ok`, removes the missing row, and acknowledges events; a second accept is a no-op.
- [x] 5.4 `openspec validate add-scanner --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier` agent; resolve drift. Update `CLAUDE.md`. Archive.
