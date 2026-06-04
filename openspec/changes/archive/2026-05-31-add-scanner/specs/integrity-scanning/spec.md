## ADDED Requirements

### Requirement: Scan classifies files with fast-path hashing

A scan SHALL walk a corpus root (honoring its exclude globs), diff the filesystem against the
`files` table by relative path, and classify each file as `added`, `modified`, `missing`, `ok`,
or `restored`. To avoid re-hashing unchanged data at scale, the scan SHALL compare size and mtime
first and SHALL compute the SHA-256 only when size/mtime differ or no prior hash exists. SHA-256
SHALL be computed by streaming the file in chunks (never loading it wholly into memory).

#### Scenario: New file is added

- **WHEN** a scan finds a file under the root with no matching `files` row
- **THEN** a `files` row SHALL be created with status `new`, its size/mtime/sha256 recorded, and
  an `added` event SHALL be written

#### Scenario: Unchanged file is not re-hashed

- **WHEN** a tracked file's size and mtime equal the stored values
- **THEN** the scan SHALL mark it `ok` and update `last_checked` without recomputing its SHA-256

#### Scenario: Modified content is detected

- **WHEN** a tracked file's bytes change so its SHA-256 differs from the stored hash
- **THEN** the scan SHALL record the new hash and (in worm mode) set status `modified` and write
  a `modified` event

#### Scenario: mtime moved but content identical

- **WHEN** a tracked file's mtime changes but its recomputed SHA-256 matches the stored hash
- **THEN** the scan SHALL keep status `ok`, refresh the stored mtime, and SHALL NOT write a
  `modified` event

#### Scenario: Missing file is detected

- **WHEN** a tracked file is absent from the filesystem during a scan
- **THEN** the scan SHALL set its status `missing` and write a `missing` event

#### Scenario: Restored file

- **WHEN** a file previously recorded `missing` reappears during a scan
- **THEN** the scan SHALL set its status back to `ok` and write a `restored` event

### Requirement: Each scan records a run

Every scan of a corpus SHALL create a `runs` row capturing start and finish times, the counts of
added/modified/missing/stamped/upgraded, and a result of `ok`, `partial`, or `error`. Per-file IO
or permission errors SHALL be counted and SHALL NOT abort the whole scan.

#### Scenario: Successful scan records counts

- **WHEN** a scan completes without fatal error
- **THEN** its `runs` row SHALL have `finished` set, the added/modified/missing counts populated,
  and `result` = `ok`

#### Scenario: Unreadable file does not abort the scan

- **WHEN** one file under the root cannot be read (permissions/IO)
- **THEN** the scan SHALL continue processing the remaining files and SHALL finish with
  `result` = `partial` or `error`

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode a content modification SHALL silently re-baseline the stored hash/size/mtime with no
nag event. A `missing` file SHALL raise an unacknowledged event in BOTH modes.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content
- **THEN** the scan SHALL update its stored hash and leave status `ok` with no unacknowledged
  `modified` event

#### Scenario: Missing always nags

- **WHEN** a file goes missing in a `churn` corpus
- **THEN** the scan SHALL still write an unacknowledged `missing` event

### Requirement: Accept re-baselines and acknowledges

Accepting a corpus SHALL set its `new` and `modified` files to `ok`, remove the rows for `missing`
files (accepted as gone), and mark every unacknowledged event for that corpus acknowledged
(recording who and when). Accepting again with nothing pending SHALL be a no-op.

#### Scenario: Accept clears pending changes

- **WHEN** a corpus has modified, new, and missing files and `accept` is run
- **THEN** modified/new files SHALL become `ok`, missing rows SHALL be deleted, and all
  unacknowledged events SHALL be marked acknowledged

#### Scenario: Accept is idempotent

- **WHEN** `accept` is run on a corpus with no pending changes or unacknowledged events
- **THEN** it SHALL make no changes
