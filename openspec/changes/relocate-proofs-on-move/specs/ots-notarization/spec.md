# Delta: ots-notarization (relocate-proofs-on-move)

## ADDED Requirements

### Requirement: A stamp never displaces another row's recorded proof

Every stamp path SHALL defer, before placing any proof — in the batched stamp, its per-file
fallback, and the on-demand backfill alike — any member whose canonical output path is currently recorded as
`ots_path` by a **different** file row in the collection. A deferred member SHALL stay `pending`,
SHALL be warned with the blocking row named, SHALL NOT fail the batch or the operation, and SHALL
be retried by later passes, proceeding once the blocking row's proof has been relocated away.

The guard SHALL be the **first canonical-slot decision** for every member: it SHALL be evaluated
before the adoption pass, before output-writability classification, before any staging entry is
created, and before any calendar submission — a deferred member is excluded from the batch
entirely, so no proof is ever produced for it and no produced proof needs disposing of. The
check SHALL be evaluated under the collection's proof-store lock after the operation's claim has
been re-confirmed (against current rows, not a stale snapshot), and SHALL compute canonical
paths through the single proof-path helper.

The window between the guard and placement is closed by the existing lease fencing, and the
delta SHALL be implemented so that linkage holds: a move reconciliation that would newly
reference one of the batch's output slots can only commit under the collection's operation
claim, which the stamping operation holds — so such a reconciliation implies the stamp's claim
was reclaimed, and the existing fence (no placement under a reclaimed claim) SHALL refuse the
batch's placements entirely, leaving the members `pending`. No placement-time re-query is
required or permitted to substitute for that fence.

This guard — not any relocation winning a race — is what makes it impossible for a new file
appearing at a moved file's former path to displace the moved file's proof, at every entry
point (scheduler, panel, CLI) and in the same scan that reconciled the move.

#### Scenario: A newcomer at a moved file's former path defers

- **WHEN** a file is move-reconciled from path A to path B while its proof still resides at A's
  canonical location, and a different new file at path A reaches any stamp pass
- **THEN** the newcomer's stamp SHALL be deferred with a warning naming the blocking row, its row
  SHALL remain `pending`, and the moved file's proof SHALL remain untouched at A's canonical
  location with its pointer intact

#### Scenario: The deferred stamp proceeds after relocation

- **WHEN** a stamp was deferred because its output path was another row's recorded `ots_path`,
  and the healing sweep has since relocated that row's proof to its current relpath's canonical
  location
- **THEN** the next stamp pass SHALL stamp the deferred member normally

#### Scenario: A deferral never degrades the batch

- **WHEN** one member of a stamp batch is deferred by the referenced-slot guard
- **THEN** every other member SHALL stamp normally and the operation SHALL NOT report failure
  because of the deferral

#### Scenario: A deferred member is never adopted, staged, or submitted

- **WHEN** a newcomer at a moved file's former path has bytes identical to the moved file, so
  the confirmed proof at that slot would pass the adoption pass's checks
- **THEN** the guard SHALL defer the newcomer before adoption is attempted: the newcomer SHALL
  remain `pending` with no `ots_path`, no staging entry SHALL be created for it, no calendar
  traffic SHALL occur for it, and only the moved row SHALL record the slot

#### Scenario: A reclaimed claim mid-batch places nothing

- **WHEN** a stamping operation's claim is reclaimed after the guard's evaluation (a scan under
  the replacement claim may have committed a move reconciliation referencing one of the batch's
  output slots) and the batch reaches its placement fence
- **THEN** the fence SHALL refuse every placement, the members SHALL stay `pending`, and no
  recorded proof — including one newly referencing a batch output slot — SHALL be displaced

### Requirement: A stored proof's location follows its file, without a moment of untruth

A healing sweep SHALL relocate each stored proof, as part of the scheduled daily upgrade pass
and of the `cairn upgrade` command, to the canonical proof-store location for its file's
**current** relpath. Rows are stale when `ots_path` is set and does not equal the canonical
location computed by the single proof-path helper; a SQL pre-filter MAY select candidates, but
the staleness decision for each row SHALL be confirmed through the helper itself. **Stale-pointer
existence SHALL be an independent admission reason** for the upgrade operation: a collection with
no incomplete proofs — including a tripwire-mode collection carrying historical proofs — still
claims its run slot, sweeps, and counts the sweep's work in the run's progress.

The sweep SHALL run under the collection's single-operation claim with the standard lease
discipline, SHALL take the per-collection proof-store lock for each relocation, SHALL re-confirm
the claim after acquiring the lock, and SHALL hold the lock across all relocation phases. This is
the ONLY code path that relocates proofs; scans never do.

**Corroboration before belief.** Before acting, the sweep SHALL parse the proof at the recorded
`ots_path`. Where the row records proof provenance (`ots_digest`), the source proof's committed
digest SHALL equal it; where provenance is absent (a legacy row), the source proof's committed
digest SHALL equal the row's recorded `sha256`. On any other outcome — mismatch, unreadable
source, absent source — the sweep SHALL touch nothing (no relocation, no archiving, no pointer or
state change) and SHALL warn naming the row, the digests involved, and superseded-store recovery.
A misfiled pointer (the pre-fix hazard already realized) is thereby detected and reported, never
laundered into the canonical slot. The sweep SHALL NOT write `ots_digest` or any row field other
than `ots_path`.

**The pointer invariant.** At every moment, including across a crash at any phase boundary,
`ots_path` SHALL name a location actually holding this row's proof. The sweep SHALL therefore
make the proof durably exist at the destination before the pointer is updated, and SHALL remove
the source entry only after the pointer update is committed. Where source and destination resolve
to the **same directory entry** (a case-only rename on a case-insensitive filesystem, detected by
filesystem identity, not path comparison), the sweep SHALL update the pointer to the canonical
spelling and SHALL NOT unlink anything.

**Never-destroy destination rules, evaluated in this order:**

1. A destination recorded as a different row's `ots_path` SHALL cause the relocation to defer —
   keep the old truthful pointer, warn, retry on a later sweep. Chain and cyclic moves converge
   over successive sweeps; no branch may ever create or break a second row's pointer.
2. A destination occupied by a proof **byte-identical** to the corroborated source SHALL be
   adopted as the already-published half of an interrupted relocation: the pointer is committed
   and the redundant source entry removed. Committed-digest equality alone SHALL NOT justify
   adoption — two distinct proof artifacts can commit to the same file digest while differing in
   attestation value, and neither may be discarded for the other.
3. Any other occupant SHALL be archived to the superseded store (never discarded) before the
   proof is published.

Publication SHALL be atomic and non-replacing: a hard link where supported, else a same-directory
temp copy published by a no-replace primitive; a destination appearing between inspection and
publication SHALL restart the destination rules, never overwrite. Durability SHALL use the proof
store's existing directory-sync chain.

**Failure classification.** Filesystem and precondition failures are per-row: the row is left
unchanged (its pointer still truthful), a warning names it, and the sweep continues; they re-warn
on every later sweep. A destination the filesystem refuses permanently SHALL be the same per-row
warning and SHALL NOT drop the proof's state, provenance, or pointer. A datastore failure at the
pointer commit SHALL follow the operation's normal error handling (roll back, finalize the run
accordingly) — never a silent per-row skip on a broken session. The sweep makes NO
garbage-collection promise for a redundant source copy left by a crash after the pointer commit:
such a copy sits in an unreferenced slot and a future stamp there archives it under the
never-destroy rules.

#### Scenario: The sweep converges a moved row

- **WHEN** a row's `ots_path` names its former relpath's canonical location (a reconciled move —
  including one that happened before this capability existed) and the corroboration check passes
- **THEN** one sweep SHALL leave the proof at the current relpath's canonical location with
  `ots_path` naming it, the former location vacated, and no other row field changed

#### Scenario: Crash after publication, before the pointer commit

- **WHEN** a relocation is interrupted after the proof exists at the destination but before the
  pointer update is committed
- **THEN** `ots_path` still names the source, which still holds the proof, and the next sweep
  SHALL complete the relocation by adopting the byte-identical destination

#### Scenario: Crash after the pointer commit, before source removal

- **WHEN** a relocation is interrupted after the pointer update is committed but before the
  source entry is removed
- **THEN** `ots_path` names the destination, which holds the proof; the leftover source copy
  SHALL never be silently overwritten (a future stamp at that path archives it), and no sweep
  guarantee of removing it is claimed

#### Scenario: A misfiled pointer is detected, not propagated

- **WHEN** a row's `ots_path` resolves to a proof whose committed digest matches neither the
  row's recorded provenance nor (for a legacy row) its recorded baseline digest
- **THEN** the sweep SHALL relocate nothing, archive nothing, change nothing on the row, and
  SHALL warn naming the row, both digests, and superseded-store recovery

#### Scenario: Chain moves converge without ever touching a referenced slot

- **WHEN** one scan reconciles A→B and C→A, so C's proof must move into the slot A's proof still
  occupies
- **THEN** the sweep SHALL defer C's relocation while A's row still records that slot, relocate
  A's proof to B's canonical location, and complete C's relocation on a sweep after the slot is
  vacated — with no branch placing over, unlinking, or re-pointing another row's proof

#### Scenario: A case-only rename does not unlink the only copy

- **WHEN** a file is move-reconciled to a path differing only in letter case and the proof store's
  filesystem treats the two proof paths as one directory entry
- **THEN** the sweep SHALL update `ots_path` to the canonical spelling and SHALL NOT remove the
  entry

#### Scenario: A permanently refused destination never degrades proof state

- **WHEN** the canonical location for a moved file's current relpath is refused permanently by the
  filesystem (an over-limit name component)
- **THEN** the proof SHALL remain at its recorded location with `ots_state`, `ots_digest`, and
  `ots_path` intact, and each sweep SHALL warn rather than drop, archive, or re-point anything

#### Scenario: The sweep runs where the old admission would have gone idle

- **WHEN** a collection's only outstanding work is stale pointers — it has no incomplete proofs,
  or is tripwire-mode with historical proofs
- **THEN** the upgrade pass SHALL still claim the collection, run the sweep, and record its work
  in the operation's progress
