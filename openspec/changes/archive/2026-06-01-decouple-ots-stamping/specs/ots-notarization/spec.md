## ADDED Requirements

### Requirement: Stamp pending files in batches

The system SHALL stamp the files queued in a `perfile` corpus using batched OpenTimestamps
submissions: multiple files MAY be stamped in a single `ots stamp` invocation so their digests are
aggregated into one calendar commitment, amortizing the per-file network cost. Each file in a batch
SHALL still receive its own independent `.ots` proof in the proof store with the same per-file
outcomes as a single stamp (`ots_state` becomes `incomplete`, `ots_path` and `ots_stamped_at`
recorded, counted once in `runs.stamped`). Batching SHALL NOT produce a shared/aggregate proof and
SHALL NOT write anything under the read-only corpus root. The number of files per invocation SHALL
be bounded by a configurable batch size.

#### Scenario: A batch produces one independent proof per file

- **WHEN** a `perfile` corpus has N pending files and the configured batch size is at least N
- **THEN** they SHALL be stamped in a single `ots stamp` invocation
- **AND** each file SHALL get its own `.ots` under the proof store with `ots_state` `incomplete`
- **AND** each file SHALL be counted once in the run's `stamped` total

#### Scenario: Pending exceeds the batch size

- **WHEN** the number of pending files is greater than the configured batch size
- **THEN** the files SHALL be stamped across multiple invocations, each covering at most the
  configured batch size

### Requirement: A failed batch member does not drop the batch's proofs

A stamp failure SHALL never fail the scan, and a failure affecting one file in a batch SHALL NOT
prevent the other files in that batch from being stamped. If a batch invocation does not produce a
proof for some of its members, the system SHALL fall back to stamping those members individually;
members that still fail SHALL be left `pending` and logged for retry on the next pass, while members
that succeeded retain their stored proofs.

#### Scenario: One unstampable file, the rest still stamped

- **WHEN** a batch is stamped and one of its files yields no proof
- **THEN** the remaining files in the batch SHALL keep their stored `.ots` proofs and `incomplete`
  state
- **AND** the unstamped file SHALL be retried individually, and if it still fails it SHALL be left
  `pending` and logged
- **AND** the scan SHALL complete without error

### Requirement: Automatic stamping is scoped to new and changed files; baselines are stamped on demand

Automatic stamping at the end of a scan SHALL stamp only the files that scan newly added or whose
content changed (the files it queued `pending`); it SHALL NOT stamp the pre-existing unstamped
baseline (files with `ots_state = none`). The system SHALL additionally provide an on-demand
operation that stamps every currently-unstamped file in a corpus — those with `ots_state = none`
and `status != missing` — by queueing them and stamping via the batched path. That operation SHALL
NOT re-stamp files that already hold a proof (`incomplete` or `complete`) and SHALL NOT require
re-hashing the files through a scan.

#### Scenario: A scan leaves the unstamped baseline alone

- **WHEN** a `perfile` corpus has an existing baseline of `ok` files with `ots_state = none` and a
  normal scan finds no new or changed files
- **THEN** no file SHALL be stamped and every baseline file SHALL remain `ots_state = none`

#### Scenario: A new file in that corpus is still stamped automatically

- **WHEN** a file first appears in that corpus on a later scan
- **THEN** that file SHALL be stamped automatically while the baseline files remain `none`

#### Scenario: Stamp-all backfills only unstamped files

- **WHEN** the on-demand stamp-all operation is run for a corpus
- **THEN** every file with `ots_state = none` and `status != missing` SHALL be stamped
- **AND** files that already have a proof (`incomplete` or `complete`) SHALL NOT be re-stamped
