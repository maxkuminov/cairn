# Delta: ots-notarization (relocate-proofs-on-move)

## ADDED Requirements

### Requirement: A stored proof's location follows its file, without a moment of untruth

The system SHALL keep a file's stored proof at the canonical proof-store location for the file's
**current** relpath, relocating the proof when move reconciliation changes that relpath. All
canonical locations SHALL be computed by the single proof-path helper — never assembled at a call
site.

Relocation SHALL uphold the **pointer invariant**: at every moment, including across a crash at
any point of the relocation, the row's recorded `ots_path` names a location where that row's
proof actually exists. The implementation SHALL therefore make the proof exist at the new
location (hard-link, or an equivalent copy-then-rename where linking is unsupported, made
durable with the store's existing directory-sync chain) **before** the pointer is updated, and
SHALL remove the old copy only **after** the pointer update is committed. A relocation
interrupted between phases SHALL be completable later with no state outside the row and the
store itself.

Relocation SHALL never destroy a proof, mirroring the placement rules: an occupied destination
SHALL be handled by parsing both proofs — an occupant committing to the **same digest** as the
source proof SHALL be adopted (the relocation completes against it and the now-redundant source
copy MAY be removed); a destination recorded as another row's `ots_path` SHALL cause the
relocation to be deferred with the old pointer kept; any other occupant SHALL be archived to the
superseded store before placement. No branch may discard proof bytes.

Relocation SHALL run only under the collection's single-operation claim and the per-collection
proof-store lock — the same single-writer discipline as every other proof mutation.

A **daily healing sweep** (part of the scheduled upgrade pass, and the `cairn upgrade` command)
SHALL find rows whose `ots_path` is not the canonical location for their current relpath and
re-attempt relocation under the same rules. The sweep is the convergence mechanism for deferred
and failed relocations, for crashes between relocation phases, and for rows moved before this
capability existed — which SHALL converge with no migration. A relocation the filesystem refuses
permanently SHALL be re-warned on each sweep and SHALL NOT cause the proof's state or provenance
to be discarded.

#### Scenario: Crash after the proof exists at both locations

- **WHEN** relocation is interrupted after the proof exists at the new location but before the
  pointer update is committed
- **THEN** the recorded `ots_path` still names the old location, which still holds the proof, and
  the next healing sweep SHALL complete the relocation by adopting the same-digest occupant at the
  destination

#### Scenario: Crash after the pointer update, before old-copy removal

- **WHEN** relocation is interrupted after the pointer update is committed but before the old copy
  is removed
- **THEN** the recorded `ots_path` names the new location, which holds the proof; the leftover old
  copy SHALL be removable by a later sweep (when it matches the mover's recorded digest) or
  archived by a future stamp at that path, and SHALL never be silently overwritten

#### Scenario: Destination occupied by another row's recorded proof

- **WHEN** a relocation's destination is recorded as `ots_path` by a different file row (a chain
  of moves within one collection)
- **THEN** the relocation SHALL defer, keeping the old truthful pointer, and SHALL converge on a
  later sweep once the other row's own relocation has vacated the slot

#### Scenario: Destination occupied by an unreferenced proof

- **WHEN** a relocation's destination holds a proof no row references, committing to a different
  digest
- **THEN** the occupant SHALL be archived to the superseded store (never discarded) and the
  relocation SHALL proceed

#### Scenario: The healing sweep converges a pre-existing moved row

- **WHEN** a row was move-reconciled before this capability existed, so its `ots_path` names the
  old relpath's canonical location
- **THEN** the next upgrade pass SHALL relocate its proof to the current relpath's canonical
  location and update `ots_path`, with no migration or operator action

#### Scenario: A permanently refused destination never degrades proof state

- **WHEN** the canonical location for a moved file's new relpath is refused permanently by the
  filesystem (an over-limit name component)
- **THEN** the proof SHALL remain at its recorded location with `ots_state`, `ots_digest`, and
  `ots_path` intact, and each healing sweep SHALL warn rather than drop the proof or its
  provenance
