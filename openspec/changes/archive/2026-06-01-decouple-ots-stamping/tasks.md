## 1. Config

- [x] 1.1 Add `ots_stamp_batch_size: int = 256` to `src/config.py` (Settings) and document
  `CAIRN_OTS_STAMP_BATCH_SIZE` in `.env.example`.

## 2. Batched stamping in `ots.py`

- [x] 2.1 Add a batch stamp function (e.g. `stamp_batch_via_symlink(items, calendars, staging,
  timeout)`) that takes a list of `(real_path, out_ots_path)`, creates one staging symlink per
  item, runs a single `ots stamp <link1> … <linkN>`, then moves each produced `<linkI>.ots` to its
  `out_ots_path`. Always clean up links and stray `.ots` in `finally`.
- [x] 2.2 Return per-item success based on whether each `<linkI>.ots` was actually produced
  (filesystem truth), not the process exit code, so partial success is handled.

## 3. `proofs.stamp_pending` chunking + stamp-all selection

- [x] 3.1 Chunk the `pending` rows into batches of `ots_stamp_batch_size` and stamp each batch via
  `stamp_batch_via_symlink`; advance succeeded rows to `incomplete` (set `ots_path`,
  `ots_stamped_at`) and keep the returned `stamped` count semantics.
- [x] 3.2 For any batch member that produced no proof, fall back to the existing single-file
  `stamp_via_symlink`; leave still-failing files `pending` and log them. Skip files that vanished
  between scan and stamp (current behavior).
- [x] 3.3 Add a `mark_unstamped_pending(session, corpus)` helper that sets `ots_state='pending'`
  for every file with `ots_state='none'` and `status != 'missing'`; return the count marked.

## 4. CLI `stamp` command

- [x] 4.1 Add `cairn stamp --corpus X [--all]` in `src/cli.py`: without `--all` stamp the
  already-`pending` set via `stamp_pending` (decoupled from scan); with `--all` call
  `mark_unstamped_pending` first, then `stamp_pending`. Print the number stamped.
- [x] 4.2 Update the CLI command list/status notes in `CLAUDE.md` to include `stamp`.

## 5. Panel stamp-all control

- [x] 5.1 Add a POST endpoint in `src/api/routes.py` (e.g. `/corpora/{id}/stamp-all`) that enforces
  owner/admin scoping (single + multi), calls `mark_unstamped_pending` + `stamp_pending`, and
  returns the stamped count (htmx fragment). _(Routes live in `src/control_panel/routes.py`; added
  `POST /corpus/{id}/stamp-all`.)_
- [x] 5.2 Add the "Stamp all" control to the corpus view template, shown only for `perfile`
  corpora, with the resulting count surfaced to the user.

## 6. Tests + smoke verification

- [x] 6.1 Unit-test `stamp_pending` batching: N pending files yield N proofs and a correct count;
  pending > batch size spans multiple invocations (mock/fake the `ots` call).
- [x] 6.2 Unit-test failure isolation: a member with no produced `.ots` falls back and does not drop
  the others; the scan still completes.
- [x] 6.3 Unit-test scope: a scan with no new/changed files leaves `none` baseline untouched;
  `mark_unstamped_pending` + stamp backfills only `none`/non-missing files and never re-stamps
  `incomplete`/`complete`.
- [x] 6.4 Smoke-test the real `ots` CLI on Python 3.12: stamp a small batch (e.g. 3 temp files) in
  one invocation and assert three independent, individually-verifiable `.ots` proofs are produced
  (de-risk the external-process integration). _(Gated behind `CAIRN_OTS_LIVE=1`; run live in 7.x.)_

## 7. Deploy + real-corpus verification

- [x] 7.1 `make deploy`, re-enable the scheduler (`CAIRN_SCHEDULER_ENABLED=1`), and run a scan of a
  small `perfile` corpus (e.g. Bob Tax Services); confirm all pending files stamp in minutes and
  `runs.stamped` matches the proof count on disk. _(Deployed `cairn:latest`; scheduler left paused —
  used the controlled `cairn stamp --corpus "Bob Tax Services"` instead of a global scan-all so the
  186k Photos archive wasn't touched. All 4672 pending files stamped in one invocation (≈19 batches);
  DB advanced pending→incomplete=4672 and exactly 4672 `.ots` on disk.)_
- [x] 7.2 Verify one stamped file independently (`cairn verify` / `ots verify`) and confirm a `none`
  baseline corpus (Photos) gained no proofs; spot-check `cairn stamp --corpus X --all` backfills.
  _(`cairn verify ATX-codes.pdf` re-hashed + matched the proof → "pending, not yet anchored" (correct
  for a same-day stamp). Photos stayed at 0 proofs / 186427 `none`. The `--all` backfill was NOT run
  live — the only large `none` baseline is Photos (must stay unstamped); it is covered by unit +
  panel tests and rides the now-live-proven batched path.)_
