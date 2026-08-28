# Tasks: relocate-proofs-on-move

## 1. The relocation primitive (`src/services/ots.py`)

- [ ] 1.1 Implement `relocate_proof(old_path, new_path, *, store_root, expected_digest)` with the
  four-phase order from design D1: pre-checks (source exists; `_NAME_MAX_BYTES` pre-check on
  components below the store root), link phase (mkdir parents; `os.link`, falling back to
  copy-to-temp + fsync + `os.rename` on EPERM/EXDEV/ENOTSUP), returning control to the caller
  for the pointer commit, then an unlink-old phase (`suppress(OSError)` + directory fsync).
  Reuse the existing durability helpers (directory-sync chain to the store root) and failure
  classification (module's governing rule: only the final output path may raise permanent).
- [ ] 1.2 Occupied-destination handling per design D2: parse occupant + source with
  `read_proof_facts`; same-digest occupant → adopt (relocation completes; source becomes the
  redundant copy); different digest → archive the occupant via the existing preservation helper
  before placing. (The referenced-by-another-row check is the caller's, task 2.2 — `ots.py`
  stays DB-free.)
- [ ] 1.3 Unit tests (`tests/test_proof_preservation.py` style): plain relocation moves the
  proof + both-exist window is durable; same-digest occupant adopted; different-digest occupant
  archived not destroyed; link-refusing filesystem falls back to copy; over-limit destination
  raises the permanent classification; every failure leaves the source proof readable.

## 2. Scanner integration (`src/services/scanner.py`)

- [ ] 2.1 In `_reconcile_moves`, after the added-row delete/flush and the surviving-row repoint:
  for each reconciled row with `ots_path` set, run the relocation with the pointer-invariant
  commit order (proof exists at new location → update `ots_path` + commit → unlink old). Rows
  with `ots_state` in (`none`,`pending`) or `ots_path` NULL skip untouched. Compute both
  locations via `proofs.proof_path` (design D5); thread settings/store root in from the caller.
- [ ] 2.2 The referenced-by-another-row defer check (design D2): one bounded query for another
  `files` row in the collection whose `ots_path` equals the destination; on hit, defer (keep old
  pointer, warn).
- [ ] 2.3 Per-file failure discipline (design D3): any relocation failure logs one WARNING,
  keeps the old pointer, and never fails/degrades the reconciliation or the scan. Update the
  `_reconcile_moves` docstring ("mutates the index only" is no longer true; state the new
  contract and the lease/flock discipline it rides).
- [ ] 2.4 Ordering: assert (test, not comment) that reconciliation-with-relocation completes
  before the scan's stamp pass, so a same-scan newcomer at the vacated path stamps cleanly.
- [ ] 2.5 Tests (`tests/test_scanner.py`): proof follows a reconciled move (file at new
  canonical location, `ots_path` updated, old location vacated); relocation failure →
  reconciliation still completes with old truthful pointer + warning; move of an unstamped
  (`pending`/`none`) row does no filesystem work; same-scan move + newcomer-at-old-path
  integration test (newcomer stamps into the vacated slot; mover's proof intact at new slot).

## 3. Healing sweep (`src/services/proofs.py` + scheduler/CLI reach)

- [ ] 3.1 Implement the stale-pointer sweep in the upgrade pass's per-collection work: select
  rows where `ots_path IS NOT NULL` and `ots_path` differs from the canonical location for the
  current relpath (SQL prefix comparison derived from `proof_path` — design D5), and re-run
  relocation for each under the claim + flock the pass already holds.
- [ ] 3.2 The sweep converges every deferred/failed/crashed case: same-digest destination
  adoption completes half-done relocations; chain-move defers resolve over successive passes;
  permanently refused destinations re-warn without touching `ots_state`/`ots_digest`/`ots_path`.
- [ ] 3.3 Ensure both entry points reach it: the scheduler's daily upgrade and `cairn upgrade`.
- [ ] 3.4 Tests: a pre-existing moved row (pointer at old canonical, proof at old canonical —
  the live-deployment shape) is healed by one upgrade pass; a crash-between-phases fixture
  (proof at both locations, pointer at old) completes via adoption; a chain move (A→B, C→A in
  one scan with one defer) converges after the sweep; the permanent-refusal case warns and
  preserves state on every field.

## 4. Verification

- [ ] 4.1 Full test suite green (`.venv/bin/pytest -q`).
- [ ] 4.2 `openspec validate relocate-proofs-on-move --strict` passes.
- [ ] 4.3 Grep check: no call site assembles a proof path by string concatenation; every
  relocation/heal path goes through `proof_path` + `relocate_proof`.
