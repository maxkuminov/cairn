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

The guard SHALL detect aliased slots, not just equal spellings: a member whose output path
differs from a recorded `ots_path` only in a way the store's filesystem may treat as the same
entry (a case-insensitive store) SHALL be deferred when filesystem identity confirms the alias —
candidate rows MAY be found by case-insensitive comparison, but the deferral SHALL be confirmed
by comparing the on-disk identity of the member's output path against the recorded pointer's
entry, so a case-sensitive store (where the two spellings are genuinely distinct slots) is never
falsely deferred, and a case-insensitive store can never stamp over a referenced proof through a
respelled path. *Accepted limitation:* filesystem identity (device and inode) cannot
distinguish one directory entry from two hard links to one file — which Cairn's own relocation
can leave behind across a crash — so the alias check MAY over-defer a member whose slot is
genuinely distinct on a case-sensitive store while such a leftover link exists. The failure
direction is conservative and loud (a warned, retried deferral; nothing displaced, nothing
lost), and `cairn upgrade` converges the blocker or clears the leftover, after which the member
stamps normally.

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

#### Scenario: A respelled path cannot evade the guard on a case-insensitive store

- **WHEN** the proof store's filesystem is case-insensitive, a moved row records `A.ots` as its
  `ots_path`, and a newcomer's canonical output path is `a.ots`
- **THEN** the guard SHALL defer the newcomer (the alias confirmed by filesystem identity), and
  on a case-sensitive store the same spellings SHALL be treated as distinct slots and not
  deferred

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
existence SHALL be an independent admission reason** for the upgrade operation — and so SHALL
the restore leg's shape, a row whose recorded `ots_path` entry is absent on disk: a collection
with no incomplete proofs — including a tripwire-mode collection carrying historical proofs —
still claims its run slot, sweeps, and counts the sweep's work in the run's progress.

The sweep SHALL run under the collection's single-operation claim with the standard lease
discipline, SHALL take the per-collection proof-store lock for each relocation, SHALL re-confirm
the claim after acquiring the lock, and SHALL hold the lock across all relocation phases. This is
the ONLY code path that relocates proofs; scans never do.

**Corroboration before belief.** Before acting, the sweep SHALL parse the proof at the recorded
`ots_path`. Where the row records proof provenance (`ots_digest`), the source proof's committed
digest SHALL equal it; where provenance is absent (a legacy row), the source proof's committed
digest SHALL equal the row's recorded `sha256`. On any other outcome the sweep SHALL touch
nothing (no relocation, no archiving, no pointer or state change) and SHALL warn — and the
warning SHALL state what the evidence supports, no more: a **provenance mismatch** (`ots_digest`
recorded and disagreeing) is a detected misfiled pointer and SHALL be reported as such, naming
both digests and superseded-store recovery; a **legacy disagreement** (`ots_digest` absent and
the source not matching `sha256`) is AMBIGUOUS — a legitimately modified-then-moved file whose
old proof was never re-placed produces exactly this shape — so the warning SHALL say the proof
cannot be corroborated and the row cannot be healed safely without historical provenance, and
SHALL NOT claim the proof was swapped or misfiled. The sweep SHALL NOT write `ots_digest` or any
row field other than `ots_path`.

**The pointer invariant.** At every moment, including across a crash at any phase boundary,
`ots_path` SHALL name a location actually holding this row's proof. The sweep SHALL therefore
make the proof durably exist at the destination before the pointer is updated — **every**
completion path, including one that adopts an occupant published by an earlier interrupted
attempt, SHALL sync the destination's directory chain to the store root before the pointer
commit, because the occupant's own publication may never have been made durable. The source
entry SHALL be removed only after the pointer update is committed, and removal SHALL be
loss-proof: the sweep SHALL first copy the source into the superseded archive, and after
removal SHALL re-verify the destination still holds the proof, restoring from that archive copy
if it does not (the defense against a filesystem whose identity reporting lies). Where source
and destination resolve to the **same directory entry** (a case-only rename on a
case-insensitive filesystem, detected by filesystem identity AND confirmed by byte comparison —
identity alone SHALL NOT be trusted), the sweep SHALL update the pointer to the canonical
spelling and SHALL NOT remove anything.

**The pointer commit SHALL be a fenced compare-and-set**, not a blind write: one guarded UPDATE
requiring the row's `relpath`, current `ots_path`, and (where recorded) `ots_digest` to still
equal the values the sweep corroborated, with the operation's run still live. A zero-row result
means the row changed under the sweep (or the claim was reclaimed): the sweep SHALL roll back,
treat its claim as lost, and stop — the published destination copy is inert and a later sweep
re-evaluates from scratch. A single post-lock lease check SHALL NOT be treated as covering the
whole relocation.

**Never-destroy destination rules, evaluated in this order:**

1. A destination recorded as a different row's `ots_path` SHALL cause the relocation to defer —
   keep the old truthful pointer, warn, retry later. Chain moves converge as earlier relocations
   vacate slots (the sweep MAY iterate within one pass); no branch may ever create or break a
   second row's pointer. A **cyclic** dependency (two or more rows each blocking another's
   destination, e.g. two files whose paths were swapped) cannot converge by deferral alone: when
   a full pass over the stale set makes no progress, the sweep SHALL break the cycle by
   selecting ONE row from those deferred **solely by this reference rule** — a row whose source
   passed corroboration and which no other rule (a permanent destination refusal included)
   refuses to move; a row any other rule refuses SHALL never be selected — and relocating that
   row's proof to a durable, unreferenced **holding location**
   inside the proof store (outside every canonical slot), committing that truthful pointer, and
   continuing — the vacated canonical slot lets the rest of the cycle converge, and the held
   proof relocates to its own canonical slot on a later iteration or sweep. The holding location
   is subject to the same pointer invariant and never-destroy rules as any other.
2. A destination occupied by a proof **byte-identical** to the corroborated source SHALL be
   adopted as the already-published half of an interrupted relocation: the pointer is committed
   and the redundant source entry removed. Committed-digest equality alone SHALL NOT justify
   adoption — two distinct proof artifacts can commit to the same file digest while differing in
   attestation value, and neither may be discarded for the other.
3. Any other occupant SHALL be archived to the superseded store (never discarded) before the
   proof is published.

Publication SHALL be atomic and non-replacing: a hard link where supported, else a **link-free
no-replace create** (exclusive-create of the destination, full write, fsync — the same primitive
the superseded archive already uses), so a proof store on a filesystem without hard links can
still publish; a destination appearing between inspection and publication (exclusive-create
failing because the path exists) SHALL restart the destination rules, never overwrite. Any
intermediate temp file SHALL use an exclusive non-colliding name, be fsynced before publication,
and be removed on every handled failure; a temp file left by a crash is recoverable debris the
store ignores. Durability SHALL use the proof store's existing directory-sync chain.

**The restore leg.** The sweep SHALL also select rows whose recorded `ots_path` names an
entry absent on disk — the shape a crash inside loss-proof removal can leave (the pointer
committed to the canonical path, the aliased unlink took the destination with it, the process
died before restoration), and generally the shape of a proof file lost to the store. Where the
superseded archive holds a copy whose committed digest passes the row's corroboration rules
(`ots_digest`, or `sha256` for a legacy row), the sweep SHALL republish it at the recorded path
(same durability chain) and warn that a restore occurred; where no corroborated copy exists,
the sweep SHALL warn loudly naming the row and change nothing. An absent recorded proof is
thereby found and repaired or reported on every sweep — never discovered only when an operator
happens to verify.

**Failure classification** splits at the pointer commit. **Before** the commit, filesystem and
precondition failures are per-row: the row is left unchanged (its pointer still truthful), a
warning names it, and the sweep continues; they re-warn on every later sweep. A destination the
filesystem refuses permanently SHALL be the same per-row warning and SHALL NOT drop the proof's
state, provenance, or pointer. **After** the commit, a failure in source removal or its
directory sync SHALL keep the committed destination pointer (which is truthful), warn, and fall
under the leftover-copy rules — the row is not rolled back to a pointer the proof may be about
to leave. A datastore failure at the pointer commit itself SHALL follow the operation's normal
error handling (roll back, finalize the run accordingly) — never a silent per-row skip on a
broken session. The sweep makes NO garbage-collection promise for a redundant source copy left
by a crash after the pointer commit: such a copy sits in an unreferenced slot and a future stamp
there archives it under the never-destroy rules.

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
  A's proof to B's canonical location, and complete C's relocation once the slot is vacated —
  with no branch placing over, unlinking, or re-pointing another row's proof

#### Scenario: A path swap converges via the holding location

- **WHEN** two files' paths are swapped in one scan, so each row's destination is the other
  row's recorded `ots_path` and plain deferral can never free either slot
- **THEN** the sweep SHALL relocate one member's proof — chosen only from rows deferred solely
  by the reference rule, corroborated, and refused by no other rule — to the durable holding
  location with a truthful committed pointer, converge the other member into its vacated
  canonical slot, and bring the held proof to its own canonical slot on a later iteration or
  sweep — no proof displaced, both pointers truthful throughout

#### Scenario: A cycle member another rule refuses is never the one moved

- **WHEN** a path-swap cycle's members include a row whose canonical destination is permanently
  refused by the filesystem (an over-limit component) or whose source fails corroboration
- **THEN** cycle breaking SHALL NOT select that row: its proof, pointer, and provenance stay
  intact per the refusing rule, and the sweep either selects an eligible member or leaves the
  cycle deferred with warnings

#### Scenario: A modified-then-moved legacy row is called ambiguous, not swapped

- **WHEN** a legacy row (`ots_digest` NULL) carries an old proof committing to a digest that no
  longer equals the row's re-scanned `sha256`, and the file is later moved
- **THEN** the sweep SHALL not heal it and SHALL warn that the proof cannot be corroborated
  without historical provenance — it SHALL NOT report the proof as swapped or misfiled

#### Scenario: The sweep survives a row changing beneath it

- **WHEN** a row is re-classified or re-reconciled (under a reclaimed claim) between the sweep's
  corroboration and its pointer commit
- **THEN** the fenced compare-and-set SHALL update zero rows, the sweep SHALL stop as
  claim-lost without committing anything, and the published destination copy SHALL remain inert
  until a later sweep re-evaluates from scratch

#### Scenario: A case-only rename does not unlink the only copy

- **WHEN** a file is move-reconciled to a path differing only in letter case and the proof store's
  filesystem treats the two proof paths as one directory entry
- **THEN** the sweep SHALL update `ots_path` to the canonical spelling and SHALL NOT remove the
  entry

#### Scenario: A crash inside loss-proof removal is repaired by the next sweep

- **WHEN** a relocation crashed after the pointer committed to the canonical path and an
  aliased unlink removed the destination entry, before restoration ran — leaving `ots_path`
  naming an absent entry while the superseded archive holds the corroborated copy
- **THEN** the next sweep SHALL select the row (absent recorded entry is an admission shape),
  republish the archived copy at the recorded path, and warn that a restore occurred

#### Scenario: An absent proof with no corroborated copy is loud, not silent

- **WHEN** a row's recorded `ots_path` names an absent entry and no archive copy passes the
  row's corroboration rules
- **THEN** every sweep SHALL warn naming the row and SHALL change nothing

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

## MODIFIED Requirements

### Requirement: Stamp and upgrade operations are recorded as typed runs with progress

The on-demand stamp backfill and the OTS upgrade pass SHALL each be recorded as a `runs` row with a
`kind` distinguishing it from an integrity scan — `kind = 'stamp'` for the stamp backfill and
`kind = 'upgrade'` for the upgrade pass. Each such run SHALL set `total` to the number of items it
will process, known at the start — for a stamp run, the count of files queued for stamping; for an
upgrade run, one work item per operation the pass will perform: one per incomplete proof to
upgrade, plus one per row the healing sweep confirmed stale, plus one per row selected by the
restore leg (recorded `ots_path` entry absent on disk) — a row receiving several of these
operations contributes one item per operation — and
SHALL update `processed` by exactly one per completed work item (a swept row's item counts
whether it was relocated, deferred, or refused), so a concurrent reader can observe exact
progress and `processed` can equal `total` without double- or under-counting. The run's result SHALL
be `running` while in progress and SHALL transition to a terminal value with `finished` set when
it ends. Within one upgrade run the healing sweep SHALL run before the proof upgrades, so a
relocated proof is upgraded at its new canonical location in the same pass.

These `stamp` and `upgrade` runs SHALL NOT affect scan-freshness reporting (the dead-man's switch),
which is derived from `kind = 'scan'` runs only. The upgrade pass SHALL record a run only for a
corpus that actually has work — incomplete proofs to upgrade, stale pointers to sweep, or rows
whose recorded proof entry is absent on disk (the restore leg) — and
SHALL NOT create an empty run when there is none of the three. Recording these runs SHALL NOT change the
batched stamping or upgrade mechanics or their per-file outcomes.

#### Scenario: Stamp backfill records a stamp run with exact progress

- **WHEN** the on-demand stamp backfill runs over a `perfile` corpus with N files queued
- **THEN** a `runs` row with `kind = 'stamp'` SHALL be created with `total` = N, `processed`
  advancing as batches are stamped, and a terminal result with `finished` set when it completes

#### Scenario: Upgrade pass records an upgrade run that does not affect freshness

- **WHEN** the upgrade pass processes a corpus that has incomplete proofs
- **THEN** a `runs` row with `kind = 'upgrade'` SHALL be created with `total` covering the
  incomplete proofs plus any stale-pointer sweep work and `processed` advancing as items complete
- **AND** that run SHALL NOT count toward the corpus's scan freshness

#### Scenario: Stale pointers alone are work

- **WHEN** the upgrade pass processes a corpus with no incomplete proofs but at least one row
  whose recorded proof location is not canonical for its current relpath — including a
  tripwire-mode corpus carrying historical proofs
- **THEN** a `kind = 'upgrade'` run SHALL be created whose `total` counts the stale rows, and the
  sweep's outcomes SHALL advance `processed`

#### Scenario: An absent recorded entry alone admits the collection

- **WHEN** a corpus has no incomplete proofs and no stale pointers, but one row whose recorded
  `ots_path` names an entry absent on disk (the phase-5 crash shape)
- **THEN** the upgrade pass SHALL claim the corpus, create a `kind = 'upgrade'` run counting
  that row's restore work, and run the restore leg

#### Scenario: Upgrade pass with no work of any kind records nothing

- **WHEN** the upgrade pass processes a corpus that has no incomplete proofs, no stale
  pointers, and no absent recorded proof entries
- **THEN** no `kind = 'upgrade'` run SHALL be created for that corpus
