# Tasks — tolerate un-writable proof output paths and never abort a stamp batch

## 1. Guard the proof write (ots.py)
- [x] 1.1 Add `OtsPathError(OtsError)` — the proof output path cannot be written (ENAMETOOLONG /
  un-writable destination); a *permanent* condition, distinct from a transient calendar failure.
- [x] 1.2 Add `_proof_output_writable(out_ots_path) -> bool` — the final component's `os.fsencode`
  byte length must be `<= _NAME_MAX_BYTES` (255). A cheap pre-check so an un-writable proof is
  skipped before a symlink or calendar round-trip is spent.
- [x] 1.3 Add `_place_proof(staged_ots, out_ots_path)` — `mkdir(parents=True)` + `os.replace`,
  raising `OtsPathError` **only** for `errno.ENAMETOOLONG` (permanent) and a generic `OtsError` for
  every other `OSError` (full/read-only store, cross-device, I/O — transient, left `pending`).
- [x] 1.4 `stamp_via_symlink`: pre-check the output name up front (raise `OtsPathError` before any
  work) and move the produced proof through `_place_proof`.
- [x] 1.5 `stamp_batch_via_symlink`: skip an un-writable member up front (do not symlink it, do not
  submit it to the calendar; leave its result `False`) and move each produced proof through
  `_place_proof`; a member whose write still fails stays `False` for the single-file fallback.

## 2. Permanent skip, not perpetual retry (proofs.py)
- [x] 2.1 In `stamp_pending`, catch `ots.OtsPathError` from the single-file fallback *before* the
  generic `ots.OtsError`: warn, count it, set `entry.ots_state = 'none'` and clear `entry.ots_path`
  so a normal scan does not re-queue and re-fail it every pass. A generic `OtsError` still leaves the
  file `pending`.
- [x] 2.2 Emit one summary `WARNING` naming the count of skipped-unwritable files per collection.

## 3. Tests & verification
- [x] 3.1 Unit: `stamp_via_symlink` with a > 255-byte (multi-byte Cyrillic) output name raises
  `OtsPathError` (an `OtsError`) before invoking `ots`.
- [x] 3.2 Unit: `_place_proof` converts a real filesystem `ENAMETOOLONG` into `OtsPathError` even
  when the byte pre-check is made permissive (backstop for a smaller/other real NAME_MAX).
- [x] 3.3 Unit: `stamp_batch_via_symlink` skips the overlong member (never symlinked/submitted),
  stamps the rest, and returns without raising.
- [x] 3.4 Integration: `stamp_pending` over a pending set containing one overlong-`.ots` file stamps
  the rest, drops the overlong one to `ots_state='none'`, counts only the stamped files, and never
  raises. **(the crash-loop regression)**
- [x] 3.5 Unit: `_place_proof` on a non-ENAMETOOLONG `OSError` (EROFS) raises a plain `OtsError`,
  NOT `OtsPathError`; and `stamp_pending` under a read-only store leaves every file `pending`, never
  `none` (guards against silently dropping recoverable proofs).
- [x] 3.6 `PYTHONPATH=. .venv/bin/pytest tests/test_ots.py -q` — 38 passed, 2 skipped.
- [x] 3.7 Full suite `PYTHONPATH=. .venv/bin/pytest -q` — 143 passed, 2 skipped; `ruff check` clean.
- [x] 3.8 `openspec validate tolerate-unstampable-proof-paths --strict` passes.
