# ots-notarization Specification (delta)

## ADDED Requirements

### Requirement: Notarization tolerates un-writable proof output paths

Stamping SHALL NOT abort a batch, fail a scan, or crash the process when a file's proof output path
cannot be written by the filesystem. A proof output path is *un-writable* (a **permanent** condition)
when a component of it exceeds the filesystem's per-name limit — `ENAMETOOLONG`; `NAME_MAX` is measured
in **bytes**, so a multi-byte name such as a Cyrillic filename plus its extension plus `.ots` can
exceed it while looking short. For each such file the system SHALL skip writing its proof, SHALL count
it, and SHALL log the skipped path so an operator can locate it. A skipped file SHALL be left unstamped
with `ots_state = none` and no `ots_path` (no proof recorded, no stale pointer), so it is not re-queued
and re-attempted by every subsequent scan; the other files in the same batch SHALL be stamped normally.
A skip SHALL NOT change the file's monitored `status`, and SHALL NOT suppress `missing`/`modified`
alerting for that file.

The system SHALL treat only the permanent `ENAMETOOLONG` condition as a `none` skip. **Every other**
write failure — a full or read-only proof store, a cross-device staging dir, an I/O error — SHALL be
treated as **transient**: the file SHALL be left `pending` for retry on the next pass, exactly like an
unreachable calendar or a timeout (see "A failed batch member does not drop the batch's proofs"). A
transient error SHALL NEVER drop a file to `none`, because the proof could succeed once the condition
clears and a normal scan would not re-queue a `none` file.

#### Scenario: An overlong proof name is skipped, not fatal

- **WHEN** a `perfile` collection stamps a pending set that includes a file whose `.ots` output name
  exceeds the filesystem's per-name byte limit, alongside files with writable proof names
- **THEN** the system SHALL stamp every writable-name file to `ots_state = incomplete` with its proof
  stored, SHALL skip the overlong file without writing a proof, and SHALL complete without raising

#### Scenario: A skipped file is not retried every scan

- **WHEN** a file's proof output path is un-writable and it is skipped during stamping
- **THEN** the system SHALL set that file's `ots_state` to `none` so a later normal scan (which
  queues only newly added or changed files) does not re-queue it, and SHALL record it in the run's
  stamped count as not-stamped

#### Scenario: A transient failure is not treated as a permanent skip

- **WHEN** a file with a writable proof name fails to stamp because the calendar is unreachable, the
  call times out (no proof produced), or its produced proof cannot be placed because of a non-fatal
  filesystem error (a full or read-only proof store)
- **THEN** the system SHALL leave that file `pending` for retry on the next pass, and SHALL NOT drop
  it to `none`

## MODIFIED Requirements

### Requirement: A failed batch member does not drop the batch's proofs

A stamp failure SHALL never fail the scan, and a failure affecting one file in a batch SHALL NOT
prevent the other files in that batch from being stamped. A member fails either because the batch
invocation produced no proof for it (an unreachable calendar, a timeout, one bad input aborting the
run) or because its produced proof cannot be written to its output path (an un-writable path — see
"Notarization tolerates un-writable proof output paths"). In every case the system SHALL fall back to
stamping that member individually; a member that still fails with a **transient** error SHALL be left
`pending` and logged for retry on the next pass, a member that fails because its output path is
**un-writable** SHALL be skipped and left `ots_state = none` (a permanent skip, not re-attempted every
scan), and members that succeeded SHALL retain their stored proofs. An un-writable member SHALL be
skipped before a staging symlink or a calendar submission is spent on it.

#### Scenario: One unstampable file, the rest still stamped

- **WHEN** a batch is stamped and one of its files yields no proof
- **THEN** the remaining files in the batch SHALL keep their stored `.ots` proofs and `incomplete`
  state
- **AND** the unstamped file SHALL be retried individually, and if it still fails transiently it SHALL
  be left `pending` and logged
- **AND** the scan SHALL complete without error

#### Scenario: One file with an un-writable proof path, the rest still stamped

- **WHEN** a batch is stamped and one of its files has an output proof path the filesystem refuses
  (e.g. its `.ots` name exceeds `NAME_MAX` bytes)
- **THEN** the remaining files in the batch SHALL be stamped and keep their proofs
- **AND** the un-writable file SHALL be skipped, counted, logged, and left `ots_state = none`
- **AND** the batch SHALL complete without raising
