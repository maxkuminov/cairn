## 1. OTS CLI wrapper (`src/services/ots.py`)

- [x] 1.1 `_run_ots(args, timeout=...)` helper: `subprocess.run(["ots", *args])`, capture stdout+stderr, apply timeout; return `(returncode, stdout, stderr)`. Define `OtsError(Exception)`. Treat exit 1 with "Pending"/"incomplete" text as a valid non-error state (do not raise).
- [x] 1.2 `stamp_via_symlink(real_path, out_ots_path, calendars, staging_dir)`: mkdir staging + out parent; symlink `staging/<uuid>` → `real_path`; `ots stamp -c ... <symlink>`; move `<symlink>.ots` → `out_ots_path` (atomic `os.replace`); always remove the symlink. Raise `OtsError` if no `.ots` produced.
- [x] 1.3 `upgrade(ots_path) -> bool`: `ots upgrade <ots_path>`; remove the `.bak` on success; return True iff the proof is now complete (re-check via `info`). Pending stays False, no raise.
- [x] 1.4 `verify(ots_path, digest) -> VerifyResult`: `ots verify -d <digest> <ots_path>`; parse verdict/block height + hash/date from output; combine with `info`. `VerifyResult` dataclass: `verified, state, block_height, block_hash, existed_by, calendars, message`.
- [x] 1.5 `info(ots_path) -> ProofInfo`: parse `ots info` OFFLINE → `state` (`none`/`incomplete`/`complete` by attestation type), `calendars`, `block_height`. No network.

## 2. Proof store + lifecycle (`src/services/proofs.py`)

- [x] 2.1 Path helpers: `proof_path(settings, corpus_id, relpath) -> Path` = `<proof_store>/<corpus_id>/<relpath>.ots`; `staging_dir(settings)` = `<proof_store>/.staging`.
- [x] 2.2 `stamp_pending(session, corpus) -> int`: for files with `ots_state='pending'` in this (perfile) corpus, resolve the real path under `corpus.root`, call `stamp_via_symlink`, set `ots_path`, `ots_state='incomplete'`, `ots_stamped_at=now`; on stamp failure leave `pending` and log. Return count stamped.
- [x] 2.3 `upgrade_incomplete(session, corpus=None) -> dict`: for `ots_state='incomplete'` files (optionally one corpus), run `upgrade`; set `complete` when done. Return `{upgraded, still_incomplete}`.
- [x] 2.4 `export_bundle(session, file_entry, dest_dir) -> Path`: copy the file bytes + its `.ots` into dest (`<basename>` + `<basename>.ots`). Error clearly if no proof exists.
- [x] 2.5 `stale_incomplete(session, days) -> list[FileEntry]`: files `incomplete` with `ots_stamped_at` older than `days` (config `incomplete_proof_alarm_days`).

## 3. Scanner integration (modifies integrity-scanning)

- [x] 3.1 In `scan_corpus`, when `corpus.ots_mode == 'perfile'`, set `ots_state='pending'` on files classified `added` and on content-`modified` files (both worm and churn). `none` corpora never touched.
- [x] 3.2 At the end of `scan_corpus`, if `corpus.ots_mode == 'perfile'`, call `proofs.stamp_pending(session, corpus)` and record the count in `run.stamped`. Stamp failures must not fail the scan.

## 4. CLI (`verify` / `export` / `upgrade`)

- [x] 4.1 `cairn verify <relpath> [--corpus NAME]`: locate the file (by corpus + relpath), re-hash from the read-only store, `ots.verify` against the stored `.ots`; print verdict, Bitcoin block, and existed-by date (or "pending/ not stamped"). Exit non-zero if not verified.
- [x] 4.2 `cairn export <relpath> [--corpus NAME] [--out DIR]`: write the portable bundle; print the destination paths.
- [x] 4.3 `cairn upgrade`: run `upgrade_incomplete` across all corpora; print upgraded/still-incomplete counts and warn about any `stale_incomplete`.

## 5. Verification

- [x] 5.1 `tests/test_ots.py` with the `ots` subprocess MOCKED (monkeypatch `ots._run_ots`): proof-state parsing from canned `ots info` (pending vs Bitcoin attestation); `stamp_via_symlink` writes the `.ots` to the proof-store path and never under the corpus root; scanner marks new/modified files `pending` in a perfile corpus and leaves `none` corpora unstamped; `export_bundle` writes file + `.ots`; `stale_incomplete` honors the threshold.
- [x] 5.2 A network-gated live smoke (skipped by default, e.g. `CAIRN_OTS_LIVE=1`): real `ots stamp` of a temp file via symlink → `.ots` in the store, `ots verify -d` reports pending. (Document; do not run in CI.)
- [x] 5.3 `openspec validate add-ots-notary --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier`; resolve drift. Update `CLAUDE.md` (mark verify/export/upgrade implemented). Archive.
