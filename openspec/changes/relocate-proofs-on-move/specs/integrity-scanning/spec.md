# Delta: integrity-scanning (relocate-proofs-on-move)

## MODIFIED Requirements

### Requirement: Content-addressed move/rename reconciliation

A scan SHALL reconcile moved/renamed files before it routes alerts, stamps proofs, or finalizes the
run. After the missing-sweep, a file newly classified `missing` whose stored SHA-256 **and** size
match **exactly one** file newly classified `added` in the same scan — where that content key is
shared by no other `missing` or `added` file in the run — SHALL be treated as a single moved file
rather than an independent deletion plus addition. Reconciliation SHALL preserve the original file's
identity: one surviving `files` row SHALL carry the new `relpath` with status `ok` while retaining
its `first_seen`, `sha256`, and OpenTimestamps proof state (`ots_state`, `ots_stamped_at`). A
reconciled move SHALL emit a single informational `moved` event recording the old and new paths,
SHALL NOT raise a `missing` or `added` event, SHALL NOT be counted as missing or added on the run,
and SHALL increment the run's `moved` count. Reconciliation SHALL be conservative: empty
(zero-byte) files, and any content key matching more than one candidate on either side, SHALL NOT be
reconciled and SHALL retain the existing `missing` + `added` behavior (logged for visibility).
Reconciliation SHALL NOT rewrite corpus bytes and SHALL NOT re-queue the moved file for OTS
stamping.

Where the surviving row has a stored proof (`ots_path` set), reconciliation SHALL relocate that
proof on disk to the new relpath's canonical proof-store location and update `ots_path` to match,
following the notarization capability's relocation rules (never-destroy placement, the pointer
invariant, single-writer claim + proof-store lock). Relocation SHALL complete before the same
scan's stamp pass runs, so a new file appearing at the vacated old path in the same scan cannot be
stamped into a slot the moved proof still occupies. A relocation failure SHALL be per-file: the
reconciliation itself (repointed `relpath`, `moved` event, run counting) SHALL still complete, the
row SHALL keep its previous `ots_path` — which still names where the proof actually is — a warning
SHALL be logged, and the scan run's result SHALL NOT be degraded by it. Rows with no stored proof
(`ots_state` `none` or `pending`, or `ots_path` unset) SHALL be reconciled exactly as before with
no filesystem action.

#### Scenario: A 1:1 move is reconciled to a single event

- **WHEN** a tracked file is moved/renamed to a previously-unseen path within the same corpus, its
  content unchanged, and no other file in the run shares that content
- **THEN** the scan SHALL produce one `moved` event (old → new path) and no `missing` or `added`
  event, and a single surviving `files` row SHALL hold the new `relpath` with status `ok`

#### Scenario: Moved file keeps its proof and is not re-stamped

- **WHEN** an already-stamped file in a `perfile` corpus is reconciled as a move
- **THEN** the surviving row SHALL retain its `ots_state`/`ots_stamped_at` and its proof (now at
  the new relpath's canonical location, with `ots_path` updated), and the scan SHALL NOT mark it
  `pending` or stamp a new proof for it

#### Scenario: The proof follows the file

- **WHEN** a stamped file is reconciled as a move and the new relpath's canonical proof location is
  writable and unoccupied
- **THEN** after the scan the proof file SHALL exist at the new relpath's canonical location,
  `ots_path` SHALL name it, and the old relpath's canonical location SHALL no longer hold it

#### Scenario: A same-scan newcomer at the vacated path stamps cleanly

- **WHEN** one scan reconciles a move from path A to path B while a different new file appears at
  path A and is stamped in the same scan's stamp pass
- **THEN** the newcomer's proof SHALL be placed at A's canonical location without displacing or
  archiving the moved file's proof, which has already been relocated to B's canonical location

#### Scenario: Relocation failure does not lose the proof or fail the reconciliation

- **WHEN** a move is reconciled but the proof cannot be relocated (a transient filesystem error, or
  a destination the filesystem refuses)
- **THEN** the surviving row SHALL carry the new `relpath`, status `ok`, and its previous
  `ots_path` (still naming the proof's actual location), a warning SHALL be logged, the scan SHALL
  NOT fail or degrade its result over it, and the proof SHALL remain readable at the recorded path

#### Scenario: Ambiguous content does not reconcile

- **WHEN** a `missing` file's content (sha256 + size) matches more than one `added` file, or is a
  zero-byte file
- **THEN** the scan SHALL NOT reconcile it and SHALL retain the `missing` + `added` classification,
  logging the fallback

#### Scenario: A real deletion is unaffected

- **WHEN** a tracked file is deleted and no `added` file in the run matches its content
- **THEN** the scan SHALL still classify it `missing` and write a `missing` event
