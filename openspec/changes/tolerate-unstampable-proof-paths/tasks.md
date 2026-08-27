# Tasks — tolerate un-writable proof output paths and never abort a stamp batch

## 1. Guard the proof write (ots.py)
- [x] 1.1 Add `OtsPathError(OtsError)` — the proof output path cannot be written (ENAMETOOLONG /
  un-writable destination); a *permanent* condition, distinct from a transient calendar failure.
- [x] 1.2 Add `_proof_output_writable(out_ots_path) -> bool` — *(scope narrowed in 5.3: only the
  components below the proof-store root)* — **every** such component's `os.fsencode`
  byte length must be `<= _NAME_MAX_BYTES` (255), not just the final `.ots` name: an overlong parent
  directory is equally un-writable. A cheap pre-check so an un-writable proof is skipped before a
  symlink or calendar round-trip is spent.
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
  **and `entry.ots_stamped_at`** (no stamp time may survive without a proof) so a normal scan does
  not re-queue and re-fail it every pass, and the panel claims nothing it cannot back. The monitored
  `status` is untouched. A generic `OtsError` still leaves the file `pending`.
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
- [x] 3.6 `PYTHONPATH=. .venv/bin/pytest tests/test_ots.py -q` — 47 passed, 2 skipped (round 3).
- [x] 3.7 Full suite `PYTHONPATH=. .venv/bin/pytest -q` — 266 passed, 2 skipped (round 3);
  `ruff check src tests` clean.
- [x] 3.8 `openspec validate tolerate-unstampable-proof-paths --strict` passes.

## 4. Post-audit hardening (adversarial review of the implementation)
- [x] 4.1 `ots.py`: guard the staging-dir `mkdir` (`_prepare_staging_dir`) and the staging-symlink
  creation in **both** `stamp_via_symlink` and `stamp_batch_via_symlink`. A raw `PermissionError` /
  `OSError` from an un-writable or full proof store escaped `stamp_pending`'s per-file handling and
  aborted the whole pass, later chunks included. A staging-dir failure is always transient
  (`OtsError`, everything stays `pending` — it says nothing about any one file); a symlink failure is
  transient too except `ENAMETOOLONG`, which is permanent for that one file (`OtsPathError`)
  *(reversed in 5.1: a staging failure is transient whatever its errno)*. In the
  batch API a per-member symlink failure leaves that member `False` and the rest proceed.
- [x] 4.2 `proofs.py`: wrap the batched `stamp_batch_via_symlink` call so a batch-level `OtsError`
  degrades to the per-file fallback loop (`outcomes = [False] * len(chunk)`) instead of aborting the
  remaining chunks or propagating to the scan.
- [x] 4.3 `proofs.py`: the permanent-skip branch also clears `entry.ots_stamped_at` — a file renamed
  onto an un-writable path after an earlier stamp kept the old content's stamp time with no proof,
  which the panel renders as trust metadata.
- [x] 4.4 `ots.py`: `_proof_output_writable` checks every component of the output path *(bounded in
  5.3 to the components below the store root)*, so an
  overlong **parent directory** is skipped up front instead of burning a batch calendar round-trip
  plus a single-file retry before `_place_proof` classifies it. `_place_proof` stays the backstop.
- [x] 4.5 Regression: `test_stamp_pending_symlink_failure_leaves_files_pending` — `os.symlink`
  raising `PermissionError` never escapes `stamp_pending`; every file stays `pending` (never `none`),
  nothing is submitted to a calendar.
- [x] 4.6 Regression: `test_stamp_pending_staging_dir_failure_does_not_abort_later_chunks` — an
  un-creatable staging dir over 3 chunks still walks all 5 files (one single-file fallback each) and
  leaves them all `pending`, without raising.
- [x] 4.7 Regression: `test_permanent_skip_clears_stamp_time_and_keeps_status` — a row with a
  populated `ots_stamped_at` that takes the permanent skip ends `ots_state='none'`, `ots_path=None`,
  `ots_stamped_at=None`, and its monitored `status` unchanged.
- [x] 4.8 Regression: `test_proof_output_writable_rejects_overlong_parent_component` and
  `test_stamp_batch_skips_overlong_parent_dir_before_any_symlink` — an overlong parent component
  (short final name) is skipped before any symlink or calendar submission; peers still stamp.
- [x] 4.9 All five new tests verified failing against the pre-fix code, passing after.

## 5. Round-2 audit: fix the permanent-vs-transient boundary
The classification rule, now recorded in the `ots.py` module docstring so it survives review:
**only a failure on the FINAL proof output path may be permanent (`OtsPathError`); every
staging-side failure is transient (`OtsError`); the pre-check measures only the components Cairn
creates below the proof-store root.**

- [x] 5.1 `ots.py`: delete `_staging_link_error`. A staging-symlink `OSError` is now **always** a
  transient `OtsError`, `ENAMETOOLONG` included — the overlong operand can be the *staging* pathname
  (`<store>/.staging/<uuid>` on a store path near `PATH_MAX`), which is an environment property, not
  a property of the file. Classifying it permanent abandoned that file's notarization forever.
- [x] 5.2 `ots.py`: `stamp_via_symlink` tracks what it actually created and cleans up only that,
  under `contextlib.suppress(OSError)`; `stamp_batch_via_symlink`'s cleanup is suppressed the same
  way. The cleanup `unlink` of an overlong staging path used to raise a **raw** `OSError` out of
  `finally`, replacing the classified exception and aborting `stamp_pending` entirely.
- [x] 5.3 `ots.py`: `_proof_output_writable(out, *, below)` measures only `out.relative_to(below)` —
  `<collection_id>/<relpath>.ots`. Applying the hard 255-byte limit to the **existing store root's**
  components made every descendant proof "permanently unwritable" → a whole collection silently
  dropped to `ots_state='none'` (a mass false negative). Without `below` only the final `.ots` name
  is measured. `_place_proof` remains the runtime backstop; a final-path `ENAMETOOLONG` there stays
  permanent (the output location is fully determined by the relpath).
- [x] 5.4 `proofs.py`: `stamp_pending` passes `store_root=Path(settings.proof_store_path)` to both
  the batched and the single-file stamp calls.
- [x] 5.5 Test: `test_staging_symlink_enametoolong_is_transient` — a staging `ENAMETOOLONG` (with the
  cleanup `unlink` failing the same way) raises a plain `OtsError`, never `OtsPathError`.
- [x] 5.6 Test: `test_stamp_pending_staging_link_enametoolong_stays_pending` — end-to-end, every
  member stays `pending` (never `none`), the pass is not aborted, nothing reaches a calendar, and no
  raw `OSError` escapes (pre-fix it escaped from the cleanup path).
- [x] 5.7 Test: `test_proof_output_writable_ignores_store_root_components` +
  `test_stamp_ignores_overlong_components_of_the_store_root` — an over-limit component in the store
  root does not block stamping through either the batch or the single-file entry point, while an
  over-limit component below the root still does.
- [x] 5.8 Test: `test_proof_output_writable_rejects_overlong_parent_component` and
  `test_stamp_batch_skips_overlong_parent_dir_before_any_symlink` reworked so the overlong component
  is one Cairn creates under the store root — they still assert the permanent skip.
- [x] 5.9 All four new/reworked tests verified failing against the pre-round-2 code (the staging one
  failing both ways: `OtsPathError` classification *and* a raw `OSError` escaping `stamp_pending`).
