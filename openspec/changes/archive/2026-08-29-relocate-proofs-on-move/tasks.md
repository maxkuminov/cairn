# Tasks: relocate-proofs-on-move

## 1. The referenced-slot stamp guard (`src/services/proofs.py`)

- [x] 1.1 In `stamp_pending` (batched path + per-file fallback) and `run_stamp_backfill`:
  the referenced-slot guard as the FIRST canonical-slot decision — evaluated under the
  proof-store lock after claim re-confirmation, BEFORE the adoption pass, writability
  classification, staging-symlink creation, and calendar submission. One bounded query for
  `files` rows in the collection (other than the member's own row) whose `ots_path` equals the
  member's canonical output path (computed via `proof_path`), plus case-insensitive
  candidates confirmed as the same on-disk entry by lstat identity (alias coverage for
  case-insensitive stores; never a false defer on case-sensitive ones). On hit: exclude the member from
  the batch (stays `pending`, no staging entry, no calendar traffic), warn naming the blocking
  row, never fail the batch/operation.
- [x] 1.2 The guard-to-placement window is closed by LOCK DISCIPLINE, not by a point-in-time
  read (implementation audit scope 1, B1/M1). `stamp_pending` (and therefore `run_stamp_backfill`,
  whose critical section it is) takes the collection's proof-store flock ONCE and holds it
  continuously across guard → adoption → staging → calendar submission → placement → every
  post-guard state commit, releasing it in a `finally`. Claim reclamation probes that same lock
  non-blocking FIRST — `collections.reclaim_stale_claim` and the scheduler's fleet reaper
  (`reap_orphaned_runs`, which runs on every tick, so its context guarantees no exclusivity) both
  refuse while it is held and hold it across their guarded UPDATE; a crashed holder's lock is
  released by the OS and reclaims normally; a store that cannot lock degrades to the guarded
  UPDATE alone with the existing one-warning-per-store. The lease fences REMAIN on every
  post-guard state commit — the adoption pass's own commit included, even when no placement chunk
  survives it (M1) — as the guard for crashed holders and degraded stores. **And the fence is
  read AFTER the work, not only before it** (convergence review, B1): the batch's `ots` spawn and
  calendar round-trip are where a lease has time to be lost, so `stamp_pending` re-reads the claim
  when `_stamp_one_batch` returns — immediately before the progress callback's commit — and once
  more before its own final commit (the only commit on the scan path, which passes no callback).
  On a store that cannot `flock` this is the ONLY guard there is. Tests: a live batch holding the
  lock with a stale heartbeat is not reclaimed (and the run row is untouched); a crashed holder's
  stale claim reclaims normally; an adoption-only batch reclaimed before its commit is refused by
  the fence with its members left `pending`; on an ENOTSUP-mocked (degraded) store a claim
  reclaimed DURING the calendar call commits no file-row state at all.
- [x] 1.2b Alias candidate keys use FULL UNICODE case folding (implementation audit scope 1, B2):
  a `casefold` SQL function (Python `str.casefold`, `deterministic=True`) is registered on every
  SQLite connection beside the pragmas in `src/database.py`, and `_slot_references` keys its
  folded leg on `casefold(ots_path)` against `str.casefold()`ed wanted paths — SQLite's `lower()`
  folds ASCII only, so a non-ASCII respelling (`Å.txt.ots` vs `å.txt.ots`) surfaced no candidate
  at all. `same_directory_entry` remains the decider, so a case-sensitive store still never
  defers a genuinely distinct slot. Tests: the non-ASCII respelling is surfaced by the candidate
  key and defers when identity confirms the alias (and does not when identity says otherwise);
  a fresh connection answers `SELECT casefold('Å')`.
- [x] 1.3 Tests: a newcomer at a moved row's former path defers at every entry point (batched
  stamp, per-file fallback, backfill); ordering asserted with barriers/mocks — a deferred
  member triggers NO adoption attempt and NO calendar call, and the guard runs after lock +
  claim re-confirmation; the byte-identical newcomer case defers BEFORE `_adopt_or_verdict`
  can adopt the blocker's proof (newcomer keeps `ots_path` NULL); the warning names the actual
  blocking row; the rest of the batch stamps normally; the deferred member proceeds on the
  pass after the blocking pointer is gone; the guard never matches a member against its own
  row (re-stamp of the same file unaffected); a case-respelled output path defers on a
  case-insensitive store and does not on a case-sensitive one; the reclaimed-claim race
  (pause after guard, reclaim, commit a reconciliation, resume) places nothing.

## 2. The relocation primitive (`src/services/ots.py`)

- [x] 2.1 Implement the per-row relocation used ONLY by the healing sweep, phases per design
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
- [x] 2.2 Loss-proof source removal (post-commit): archive-copy the source first, unlink,
  fsync, then re-verify the destination still holds the proof and restore from the archive
  copy if not. Post-commit failures keep the committed pointer and warn (never roll back the
  row); pre-commit filesystem/precondition failures return a per-row outcome (nothing
  changed, warnable). No branch discards proof bytes; a permanent destination refusal is a
  per-row outcome, never a drop-to-`none`. **Never a silent success** (implementation audit
  scope 2, B1/M4): a failed unlink or source-directory sync is no longer suppressed — the
  destination is verified first, then the removal failure RAISES as the post-commit warning;
  and a failed RESTORATION raises too, leaving exactly the restore leg's admission shape
  (committed pointer naming an absent entry, corroborated copy durable in the archive), which
  the next sweep repairs. `_sweep_relocate`'s post-commit warning READS the destination
  instead of assuming it: "nothing was lost, the pointer is correct" is printed only when the
  pointer actually resolves, and the absent case says so and names the restore leg.
- [x] 2.3 Unit tests (`tests/test_proof_preservation.py` style): plain relocation; both-exist
  crash window completes via byte-identical adoption AND syncs the destination chain; a
  same-identity-but-different-bytes aliasing lie does not commit the pointer;
  same-committed-digest but byte-different occupant is NOT adopted (archived, source
  preserved); link-refusing filesystem publishes via the exclusive-create path; destination
  appearing between inspection and publication restarts classification instead of
  overwriting; case-aliased source/destination updates spelling without removing anything;
  removal re-verification restores from the archive copy when the destination vanished;
  over-limit destination refused per-row; every failure leaves the source proof readable and
  the row's pointer truthful. Plus (scope 2): a failed restoration after the identity-lying
  unlink warns and is repaired by the NEXT sweep through the restore leg; an unlinkable source
  (EACCES) keeps the committed pointer, leaves both copies readable and is WARNED; and the
  sweep-level aliased branch re-spells the pointer without calling the removal phase at all.

## 3. The healing sweep (`src/services/proofs.py` + scheduler/CLI reach)

- [x] 3.1 Stale-row selection: SQL pre-filter (prefix comparison) + authoritative per-row
  confirmation via `proof_path` (design D6), so the SQL can never disagree with the helper;
  test they agree on names containing `%`, `_`, and quotes.
- [x] 3.2 Corroboration before belief (design D3): source proof parsed; committed digest must
  equal `ots_digest`, or `sha256` for a legacy (`ots_digest` NULL) row; any other outcome
  (mismatch/unreadable/absent source) touches nothing and warns naming the row, digests, and
  `.superseded/` recovery. The sweep writes `ots_path` and nothing else, ever.
- [x] 3.3 Defer-if-referenced (design D4 rule 1): one bounded query for another row recording
  the destination as its `ots_path`; on hit, defer with the old pointer kept. Cycle breaking:
  when a full pass makes no progress and reference-rule-deferred rows remain, relocate ONE
  such member's proof — eligible only if corroboration passed and no permanent destination
  refusal; never a row any other rule refuses — to the durable holding slot
  (`<store>/.relocating/<row_id>.ots`), commit that truthful pointer, continue; the held
  proof converges on a later iteration/sweep.
- [x] 3.4 Pointer commit is a fenced compare-and-set: guarded UPDATE on
  (`relpath`, `ots_path`, `ots_digest` where recorded) + run-still-live; zero rows → roll
  back, claim-lost, stop (published destination copy stays inert). A datastore failure at the
  commit follows the operation's normal error handling (rollback + run finalization), never a
  per-row skip on a broken session.
- [x] 3.4b The restore leg (design D4b): admission also selects rows whose recorded
  `ots_path` entry is absent on disk; a corroborated superseded-archive copy is republished
  at the recorded path (durability chain, "restored" warning); no corroborated copy → loud
  warning, nothing changed. **Republication is stage-then-publish** (convergence review, B2):
  the bytes go to an exclusive, fsynced temp in the destination's own directory
  (`ots._stage_proof_bytes`) and are published by `os.link`, so a write that fails partway can
  never leave a PREFIX of a proof at a path a row already records as complete — which, since
  admission only asks whether that entry exists, would never be re-admitted and would corrupt
  the row's proof silently and permanently. Cleanup only ever touches the temp; a link-less
  store keeps the direct exclusive-create fallback, whose refused cleanup now warns by name.
  Tests: the phase-5 crash shape (pointer canonical, entry absent, archive copy present)
  repairs on the next sweep; absent with no corroborated copy warns every sweep and never
  writes; an ENOSPC mid-write whose cleanup is also refused leaves the recorded slot ABSENT,
  the row untouched, and the next sweep restores it.
- [x] 3.5 Independent admission + typed-run totals (MODIFIED requirement): stale-pointer
  existence OR an absent recorded proof entry alone claims the collection and creates the
  `kind='upgrade'` run (tripwire included); `total` = one work item per operation (one per
  incomplete + one per stale row + one per absent-entry row; a row receiving several
  operations contributes one item each), `processed` advances exactly one per completed item;
  no work of any of the three kinds → no run; sweep runs before the proof upgrades within the pass; neither-work → no run.
  Wire both entry points: the scheduler's daily pass and `cairn upgrade`. Lease discipline:
  claim heartbeat, per-collection proof flock around each relocation, claim re-confirmed
  after lock acquisition, lock held across all phases.
  **Per operation, not per row** (implementation audit scope 2, M2): `survey_pointer_work`
  classifies `stale` and `absent` INDEPENDENTLY — they may overlap, and a row that is both is
  restored and THEN relocated inside the same sweep, contributing one work item each. The old
  disjoint ("absent only") classification restored such a row to its obsolete location, called
  the run `ok`, and left the pointer non-canonical (still blocking a newcomer's stamp).
  **Surveyed under the claim** (M3): the pre-claim survey is advisory only — it decides
  whether to try for the slot; the authoritative work set and `total` come from a re-survey
  taken immediately AFTER `claim_run`, and an empty one discards the provisional run row (a
  guarded DELETE, so a reclamation's `interrupted` row stands) rather than recording an empty
  upgrade run.
  **Terminal numbers are what happened** (M1): `processed` is finalized from the shared
  counter, never the admission total, and an `ok` pass that did not reach its total — the
  sweep or the upgrade loop stopping early rather than waiting out a contended proof-store
  lock — finalizes `partial` with the skip warned. No retry loop.
- [x] 3.6 Tests: a pre-existing moved row (live-deployment shape) heals in one sweep with only
  `ots_path` changed; crash-window fixtures (both-exist → completes; pointer-committed +
  leftover source → leftover untouched and never re-selected); chain A→B + C→A converges over
  two sweeps with no cross-row interference; cycle A→B + B→A defers safely; misfiled pointer
  (stranger's proof at recorded path) warns and changes nothing; legacy `ots_digest`-NULL row
  heals only when the source commits to its `sha256`, and a modified-then-moved legacy row is
  warned as ambiguous (never "misfiled"); a path swap (cycle) converges via the holding slot;
  the row-changed-beneath-the-sweep race hits the CAS zero-row path and commits nothing;
  sweep-only admission (no incompletes / tripwire) claims, runs, counts progress with the
  MODIFIED totals; permanent-refusal row re-warns with all fields intact. Plus (scope 2): an
  absent-AND-stale row is restored then relocated in ONE sweep with `total`/`processed` = 2/2;
  a sweep that could not take the proof-store lock finalizes `partial` (0 of 1) and the next
  pass converges it `ok`; work completed by a rival between the pre-check and the claim leaves
  NO run row at all.

## 4. Verification

- [x] 4.1 Full test suite green (`.venv/bin/pytest -q`).
- [x] 4.2 `openspec validate relocate-proofs-on-move --strict` passes.
- [x] 4.3 Grep checks: no call site assembles a proof path by string concatenation; scans
  contain no proof-store mutation; the relocation primitive is reachable only from the sweep.
