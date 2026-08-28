# Proposal: relocate-proofs-on-move

## Why

GitHub #39 (found while designing guard-proof-and-restore-integrity, design D8): move
reconciliation repoints a moved file's `relpath` but leaves `ots_path` at the **old** relpath's
canonical proof location. The pointer is momentarily truthful (the proof really is there), but
the old canonical slot is now claimable by a stranger: if a different file later appears at the
old path and is stamped, `_place_proof` archives the moved file's proof into `.superseded/` and
the moved row's pointer then resolves to the stranger's proof. Since
guard-proof-and-restore-integrity the proof is never *destroyed* (the archive keeps it) and
`files.ots_digest` makes the misfiling *detectable* at verify time — but the pointer is still
wrong, and the operator's "verify" on a perfectly healthy moved file reads as a proof mismatch.
This voids the evidentiary value of a proof over pure bookkeeping (DESIGN.md §1, §6).

## What Changes

- **A moved file's proof follows it.** During move reconciliation, the surviving row's stored
  proof is relocated on disk from the old relpath's canonical location to the new relpath's
  canonical location, and `ots_path` is updated to match. Relocation rides the existing
  machinery: it happens inside the scan's operation claim and the per-collection proof-store
  lock (proof mutation stays single-writer by construction), and it goes through the same
  never-destroy placement discipline as stamping — an occupied destination displaces nothing
  silently, and no failure path can discard a proof.
- **Relocation failure is per-file and never lossy.** If the proof cannot be moved (transient
  filesystem error, destination refuses the name permanently), the row keeps its OLD `ots_path`
  — a pointer that still names where the proof actually is — with a warning; the move
  reconciliation itself still completes. The vulnerable-pointer window persists only for that
  file and is self-healing (below).
- **Self-healing for stale pointers.** The daily upgrade pass, which already walks every
  incomplete proof and backfills provenance, additionally notices a row whose `ots_path` is not
  the canonical location for its current `relpath` and retries the relocation. This heals both
  failed relocations and every moved file from before this change — the live deployment's
  existing moved rows converge without a migration.
- **Ordering guarantee inside one scan:** relocation happens during reconciliation, which
  already runs before the scan's stamp pass — so a new file appearing at the vacated old path
  in the *same* scan stamps into a slot the moved proof has already left.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `integrity-scanning`: the move/rename reconciliation requirement changes — reconciliation no
  longer "rewrites only the index"; it also relocates the stored proof and updates `ots_path`,
  with per-file, never-lossy failure handling.
- `ots-notarization`: a new requirement — a stored proof's canonical location follows its file's
  current relpath; relocation obeys the never-destroy placement rules, runs under the
  collection's single-writer claim + proof-store lock, and stale pointers are healed by the
  upgrade pass.

## Impact

- `src/services/scanner.py` — `_reconcile_moves` gains the relocation step (and therefore
  filesystem side effects it deliberately did not have; its docstring contract changes).
- `src/services/ots.py` — a `relocate_proof(old, new, store_root)` primitive implementing the
  never-destroy move (occupied-destination rules, durability/fsync chain, failure
  classification per the module's governing rule).
- `src/services/proofs.py` — the upgrade pass's healing hook (`ots_path` ≠ canonical →
  relocate); `proof_path` is the single canonical-location oracle.
- Tests: `tests/test_scanner.py` (move + relocation, failure fallback),
  `tests/test_proof_preservation.py`-style placement tests, upgrade-pass healing test.
- **No schema change, no migration** (`ots_path` remains stored; healing is behavioral).

## Non-goals

- Not deriving `ots_path` from `relpath` (the issue's alternative fix): a derived path cannot
  describe the failure states (proof stuck at the old location, legacy rows mid-heal) that the
  stored pointer plus healing handles; and 18 call sites read `ots_path` today.
- No retroactive repair of *misfiled* pointers beyond relocation — a row whose pointer already
  resolves to a stranger's proof (the pre-fix hazard realized) is out of scope here; verify's
  provenance ladder already reports it, and recovery from `.superseded/` stays manual.
- No change to move-reconciliation matching rules (strict 1:1, zero-byte exclusion, ambiguity
  fallback — all unchanged).
- No change to `_place_proof`'s occupied-canonical-path rules for stamping.

DESIGN.md references: §5 (scanner architecture), §6 (OTS handling / proof store).
