# Tasks: relocate-proofs-on-move

## 1. The referenced-slot stamp guard (`src/services/proofs.py`)

- [ ] 1.1 In `stamp_pending` (batched path + per-file fallback) and `run_stamp_backfill`:
  the referenced-slot guard as the FIRST canonical-slot decision — evaluated under the
  proof-store lock after claim re-confirmation, BEFORE the adoption pass, writability
  classification, staging-symlink creation, and calendar submission. One bounded query for
  `files` rows in the collection (other than the member's own row) whose `ots_path` equals the
  member's canonical output path (computed via `proof_path`). On hit: exclude the member from
  the batch (stays `pending`, no staging entry, no calendar traffic), warn naming the blocking
  row, never fail the batch/operation.
- [ ] 1.2 No placement-time re-query: rely on (and test) the existing lease fence for the
  guard-to-placement window — a reconciliation referencing a batch slot implies the stamp's
  claim was reclaimed, and the fence refuses the whole batch's placements, members `pending`.
- [ ] 1.3 Tests: a newcomer at a moved row's former path defers at every entry point (batched
  stamp, per-file fallback, backfill); ordering asserted with barriers/mocks — a deferred
  member triggers NO adoption attempt and NO calendar call, and the guard runs after lock +
  claim re-confirmation; the byte-identical newcomer case defers BEFORE `_adopt_or_verdict`
  can adopt the blocker's proof (newcomer keeps `ots_path` NULL); the warning names the actual
  blocking row; the rest of the batch stamps normally; the deferred member proceeds on the
  pass after the blocking pointer is gone; the guard never matches a member against its own
  row (re-stamp of the same file unaffected); the reclaimed-claim race (pause after guard,
  reclaim, commit a reconciliation, resume) places nothing.

## 2. The relocation primitive (`src/services/ots.py`)

- [ ] 2.1 Implement the per-row relocation used ONLY by the healing sweep, phases per design
  D4: aliasing check first (lstat dev+inode equality CONFIRMED by byte comparison — identity
  alone never decides; alias → pointer-spelling CAS only, nothing removed); ordered
  destination rules (defer-if-referenced is the CALLER's check, threaded in as a
  predicate/result — `ots.py` stays DB-free; byte-identical occupant → adopt, but fsync its
  directory chain before the caller's pointer commit; other occupant → archive via the
  existing preservation helper); no-replace publication (hard link, else link-free
  O_CREAT|O_EXCL create + full write + fsync; EEXIST restarts classification; exclusive
  temp names, fsynced, removed on handled failures, crash-left temps are ignored debris);
  directory-sync durability chain; `_NAME_MAX_BYTES` pre-check on components below the store
  root.
- [ ] 2.2 Loss-proof source removal (post-commit): archive-copy the source first, unlink,
  fsync, then re-verify the destination still holds the proof and restore from the archive
  copy if not. Post-commit failures keep the committed pointer and warn (never roll back the
  row); pre-commit filesystem/precondition failures return a per-row outcome (nothing
  changed, warnable). No branch discards proof bytes; a permanent destination refusal is a
  per-row outcome, never a drop-to-`none`.
- [ ] 2.3 Unit tests (`tests/test_proof_preservation.py` style): plain relocation; both-exist
  crash window completes via byte-identical adoption AND syncs the destination chain; a
  same-identity-but-different-bytes aliasing lie does not commit the pointer;
  same-committed-digest but byte-different occupant is NOT adopted (archived, source
  preserved); link-refusing filesystem publishes via the exclusive-create path; destination
  appearing between inspection and publication restarts classification instead of
  overwriting; case-aliased source/destination updates spelling without removing anything;
  removal re-verification restores from the archive copy when the destination vanished;
  over-limit destination refused per-row; every failure leaves the source proof readable and
  the row's pointer truthful.

## 3. The healing sweep (`src/services/proofs.py` + scheduler/CLI reach)

- [ ] 3.1 Stale-row selection: SQL pre-filter (prefix comparison) + authoritative per-row
  confirmation via `proof_path` (design D6), so the SQL can never disagree with the helper;
  test they agree on names containing `%`, `_`, and quotes.
- [ ] 3.2 Corroboration before belief (design D3): source proof parsed; committed digest must
  equal `ots_digest`, or `sha256` for a legacy (`ots_digest` NULL) row; any other outcome
  (mismatch/unreadable/absent source) touches nothing and warns naming the row, digests, and
  `.superseded/` recovery. The sweep writes `ots_path` and nothing else, ever.
- [ ] 3.3 Defer-if-referenced (design D4 rule 1): one bounded query for another row recording
  the destination as its `ots_path`; on hit, defer with the old pointer kept. Cycle breaking:
  when a full pass makes no progress and stale rows remain, relocate one member's proof to
  the durable holding slot (`<store>/.relocating/<row_id>.ots`), commit that truthful
  pointer, continue; the held proof converges on a later iteration/sweep.
- [ ] 3.4 Pointer commit is a fenced compare-and-set: guarded UPDATE on
  (`relpath`, `ots_path`, `ots_digest` where recorded) + run-still-live; zero rows → roll
  back, claim-lost, stop (published destination copy stays inert). A datastore failure at the
  commit follows the operation's normal error handling (rollback + run finalization), never a
  per-row skip on a broken session.
- [ ] 3.5 Independent admission + typed-run totals (MODIFIED requirement): stale-pointer
  existence alone claims the collection and creates the `kind='upgrade'` run (tripwire
  included); `total` = incompletes + stale rows (each row once), sweep outcomes advance
  `processed`; sweep runs before the proof upgrades within the pass; neither-work → no run.
  Wire both entry points: the scheduler's daily pass and `cairn upgrade`. Lease discipline:
  claim heartbeat, per-collection proof flock around each relocation, claim re-confirmed
  after lock acquisition, lock held across all phases.
- [ ] 3.6 Tests: a pre-existing moved row (live-deployment shape) heals in one sweep with only
  `ots_path` changed; crash-window fixtures (both-exist → completes; pointer-committed +
  leftover source → leftover untouched and never re-selected); chain A→B + C→A converges over
  two sweeps with no cross-row interference; cycle A→B + B→A defers safely; misfiled pointer
  (stranger's proof at recorded path) warns and changes nothing; legacy `ots_digest`-NULL row
  heals only when the source commits to its `sha256`, and a modified-then-moved legacy row is
  warned as ambiguous (never "misfiled"); a path swap (cycle) converges via the holding slot;
  the row-changed-beneath-the-sweep race hits the CAS zero-row path and commits nothing;
  sweep-only admission (no incompletes / tripwire) claims, runs, counts progress with the
  MODIFIED totals; permanent-refusal row re-warns with all fields intact.

## 4. Verification

- [ ] 4.1 Full test suite green (`.venv/bin/pytest -q`).
- [ ] 4.2 `openspec validate relocate-proofs-on-move --strict` passes.
- [ ] 4.3 Grep checks: no call site assembles a proof path by string concatenation; scans
  contain no proof-store mutation; the relocation primitive is reachable only from the sweep.
