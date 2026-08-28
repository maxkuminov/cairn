# Tasks: relocate-proofs-on-move

## 1. The referenced-slot stamp guard (`src/services/proofs.py`)

- [ ] 1.1 In `stamp_pending` (batched path + per-file fallback) and `run_stamp_backfill`:
  before placement, one bounded query for `files` rows in the collection (other than the
  member's own row) whose `ots_path` equals the member's canonical output path (computed via
  `proof_path`). On hit: defer the member (stays `pending`), warn naming the blocking row,
  never fail the batch/operation. Evaluate inside the operation's claim against current rows.
- [ ] 1.2 Tests: a newcomer at a moved row's former path defers at every entry point (batched
  stamp, per-file fallback, backfill); the rest of the batch stamps normally; the deferred
  member proceeds on the pass after the blocking pointer is gone; the guard never matches a
  member against its own row (re-stamp of the same file unaffected).

## 2. The relocation primitive (`src/services/ots.py`)

- [ ] 2.1 Implement the per-row relocation used ONLY by the healing sweep, phases per design
  D4: aliasing check first (lstat dev+inode equality → pointer-spelling update only, no
  unlink); ordered destination rules (defer-if-referenced is the CALLER's check, threaded in
  as a predicate/result — `ots.py` stays DB-free; byte-identical occupant → adopt; other
  occupant → archive via the existing preservation helper); no-replace publication (hard
  link, else temp copy + `os.link` publish, EEXIST restarts classification); directory-sync
  durability chain; `_NAME_MAX_BYTES` pre-check on components below the store root.
- [ ] 2.2 Failure classification per design D5: filesystem/precondition failures return a
  per-row outcome (nothing changed, warnable); no branch discards proof bytes; a permanent
  destination refusal is the same per-row outcome, never a drop-to-`none`.
- [ ] 2.3 Unit tests (`tests/test_proof_preservation.py` style): plain relocation; both-exist
  crash window completes via byte-identical adoption; same-committed-digest but
  byte-different occupant is NOT adopted (archived, source preserved); link-refusing
  filesystem uses the copy + no-replace path; destination appearing between inspection and
  publication restarts classification instead of overwriting; case-aliased source/destination
  updates spelling without unlinking; over-limit destination refused per-row; every failure
  leaves the source proof readable and the row's pointer truthful.

## 3. The healing sweep (`src/services/proofs.py` + scheduler/CLI reach)

- [ ] 3.1 Stale-row selection: SQL pre-filter (prefix comparison) + authoritative per-row
  confirmation via `proof_path` (design D6), so the SQL can never disagree with the helper;
  test they agree on names containing `%`, `_`, and quotes.
- [ ] 3.2 Corroboration before belief (design D3): source proof parsed; committed digest must
  equal `ots_digest`, or `sha256` for a legacy (`ots_digest` NULL) row; any other outcome
  (mismatch/unreadable/absent source) touches nothing and warns naming the row, digests, and
  `.superseded/` recovery. The sweep writes `ots_path` and nothing else, ever.
- [ ] 3.3 Defer-if-referenced (design D4 rule 1): one bounded query for another row recording
  the destination as its `ots_path`; on hit, defer with the old pointer kept.
- [ ] 3.4 Pointer-commit ordering: publish durably → commit `ots_path` → unlink source. A
  datastore failure at the commit follows the operation's normal error handling (rollback +
  run finalization), never a per-row skip on a broken session.
- [ ] 3.5 Independent admission: stale-pointer existence alone claims the collection and runs
  the sweep (no `incomplete` proofs required; tripwire collections with historical proofs
  included); sweep work is counted in the upgrade run's progress totals. Wire both entry
  points: the scheduler's daily pass and `cairn upgrade`. Lease discipline: claim heartbeat,
  per-collection proof flock around each relocation, claim re-confirmed after lock
  acquisition, lock held across all phases.
- [ ] 3.6 Tests: a pre-existing moved row (live-deployment shape) heals in one sweep with only
  `ots_path` changed; crash-window fixtures (both-exist → completes; pointer-committed +
  leftover source → leftover untouched and never re-selected); chain A→B + C→A converges over
  two sweeps with no cross-row interference; cycle A→B + B→A defers safely; misfiled pointer
  (stranger's proof at recorded path) warns and changes nothing; legacy `ots_digest`-NULL row
  heals only when the source commits to its `sha256`; sweep-only admission (no incompletes /
  tripwire) claims, runs, counts progress; permanent-refusal row re-warns with all fields
  intact.

## 4. Verification

- [ ] 4.1 Full test suite green (`.venv/bin/pytest -q`).
- [ ] 4.2 `openspec validate relocate-proofs-on-move --strict` passes.
- [ ] 4.3 Grep checks: no call site assembles a proof path by string concatenation; scans
  contain no proof-store mutation; the relocation primitive is reachable only from the sweep.
