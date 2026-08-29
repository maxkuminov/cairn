# Proposal: relocate-proofs-on-move

## Why

GitHub #39 (found while designing guard-proof-and-restore-integrity, design D8): move
reconciliation repoints a moved file's `relpath` but leaves `ots_path` at the **old** relpath's
canonical proof location. The pointer is truthful (the proof really is there), but the old
canonical slot is claimable: if a different file later appears at the old path and is stamped,
`_place_proof` archives the moved file's proof into `.superseded/` and the moved row's pointer
then resolves to the stranger's proof. Since guard-proof-and-restore-integrity the proof is
never *destroyed* and `files.ots_digest` makes the misfiling *detectable* at verify time — but
detection reads as a proof mismatch on a perfectly healthy moved file: a false alarm on the
product's core signal (DESIGN.md §1, §6).

## What Changes

Two mechanisms, deliberately split (spec-audit round 1 established that relocating from inside
the scanner adds a second proof-mutation path with its own crash windows):

- **The referenced-slot stamp guard** — every stamp path (batched, per-file fallback, backfill)
  defers any member whose canonical output path is currently another row's recorded `ots_path`.
  The deferred member stays `pending` with a warning naming the blocker and retries on later
  passes. This closes the #39 loss path at every entry point immediately and by construction —
  nothing can ever stamp over a proof any row still points at.
- **The healing sweep** — the daily upgrade pass (and `cairn upgrade`) gains the ONE code path
  that relocates proofs: rows whose `ots_path` is not the canonical location for their current
  relpath are converged, under the collection's operation claim + per-collection proof-store
  lock, upholding a pointer invariant (`ots_path` always names a location actually holding this
  row's proof, across any crash) and never-destroy destination rules (defer if the slot is
  referenced by another row; adopt only a byte-identical occupant; archive anything else).
  Sources are **corroborated before belief** (parsed digest vs `ots_digest`, or vs `sha256` for
  legacy rows) so an already-misfiled pointer is detected and warned, never laundered. Stale
  pointers converge for every pre-fix moved row on the live deployment with **no migration**.
- Scan-side, `_reconcile_moves` stays index-only; its spec is tightened to say `ots_digest` is
  retained unchanged and that the scan never touches the proof store.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `integrity-scanning`: the move/rename reconciliation requirement — retained identity
  explicitly includes `ots_digest`; the scan explicitly never moves proof files; the retained
  pointer's protection and convergence are delegated to the notarization capability.
- `ots-notarization`: two new requirements — the referenced-slot stamp guard, and the healing
  sweep with its corroboration, pointer-invariant, never-destroy, lease-fencing, and
  independent-admission rules.

## Impact

- `src/services/proofs.py` — the guard check in `stamp_pending` / per-file fallback /
  `run_stamp_backfill`; the healing sweep in the upgrade pass (independent admission, progress
  counting); `proof_path` as the single canonical-location oracle.
- `src/services/ots.py` — the relocation primitive (aliasing check, ordered destination rules,
  no-replace publication, durability chain, failure classification).
- `src/services/scanner.py` — spec-side only (docstring/contract tightened); no behavior
  change.
- Tests: stamp-guard tests (`tests/test_ots.py` / proof-preservation style), sweep tests
  (crash-window fixtures, chain moves, corroboration refusals, legacy rows, admission), CLI +
  scheduler reach.
- **No schema change, no migration.**

## Non-goals

- Not deriving `ots_path` from `relpath` (cannot represent deferred/mid-heal states; 18 read
  sites).
- Not repairing already-misfiled pointers: the sweep detects and warns (with `.superseded/`
  recovery named); un-misfiling stays manual, per guard-proof-and-restore-integrity's scope.
- No change to move-matching rules or to `_place_proof`'s stamping-time occupied-path rules.
- No garbage-collection promise for redundant source copies left by a crash after the pointer
  commit (a future stamp at that slot archives them under the never-destroy rules).

DESIGN.md references: §5 (scanner architecture), §6 (OTS handling / proof store).
