# integrity-scanning Specification (delta)

## ADDED Requirements

### Requirement: Scan tolerates paths the datastore cannot store

A scan SHALL NOT abort, and SHALL NOT leave any file's relative path partially written, when a file
under the corpus root has a name the datastore cannot store. A relative path is *un-storable* when it
does not round-trip through UTF-8 (a non-UTF-8 on-disk name, which the OS surfaces as lone surrogate
characters that SQLite TEXT cannot bind). For each such file the scan SHALL skip it without creating
or updating a `files` row, SHALL count it among the run's errors so the run finishes `partial` (or
`error`), and SHALL log the skipped path(s) so an operator can locate them. Because no `files` row is
created, a skipped file SHALL NOT subsequently be reported as `missing` or `added` on any scan, and
SHALL NOT churn alerts across scans. Storable files in the same corpus SHALL be classified and
tracked exactly as if the un-storable file were absent.

#### Scenario: A non-UTF-8 filename is skipped, not fatal

- **WHEN** a corpus contains a file whose name is not valid UTF-8 (un-storable) alongside files with
  storable names
- **THEN** the scan SHALL classify and track every storable file normally, SHALL skip the
  un-storable file without creating a `files` row for it, and SHALL finish with `result` = `partial`

#### Scenario: A skipped file does not churn across scans

- **WHEN** a corpus with an un-storable filename is scanned repeatedly with no other changes
- **THEN** each scan SHALL skip that file again with no `missing` or `added` event for it, and the
  storable files SHALL remain `ok`

#### Scenario: Skipped paths are surfaced

- **WHEN** a scan skips one or more un-storable filenames
- **THEN** the scan SHALL emit a log record identifying the count and the skipped path(s), and the
  run's non-zero error count SHALL be reflected in its `partial`/`error` result

### Requirement: A scan always reaches a terminal run state

A scan SHALL always finalize its run to a terminal `result` (`ok`, `partial`, or `error`) with
`finished` set, even if the scan body raises an unexpected exception after the run row was recorded
`running`. The scan SHALL NOT leave its run at `result` = `running`, because a run stuck `running`
blocks the corpus from any further operation (the concurrency guard refuses a second run and the
scheduler skips an in-flight corpus) until the next process restart. This in-process guarantee
complements the startup reconciliation of orphaned `running` runs: a failure during a scan SHALL
self-heal immediately, and a failure that kills the process SHALL be reconciled on the next startup.

#### Scenario: A failure mid-scan still finalizes the run

- **WHEN** the scan body raises an unexpected exception after its run row was committed `running`
- **THEN** the scan SHALL move that run to `result` = `error` with `finished` set, so the corpus is
  not left perpetually `running` and a new scan can be started without restarting the process

## MODIFIED Requirements

### Requirement: Scan classifies files with fast-path hashing

A scan SHALL walk a corpus root (honoring its exclude globs), diff the filesystem against the
`files` table by relative path, and classify each file as `added`, `modified`, `missing`, `ok`,
or `restored`. A file whose relative path the datastore cannot store (see "Scan tolerates paths the
datastore cannot store") SHALL be skipped before classification and SHALL NOT receive a `files` row.
To avoid re-hashing unchanged data at scale, the scan SHALL compare size and mtime first and SHALL
compute the SHA-256 only when size/mtime differ or no prior hash exists. SHA-256 SHALL be computed by
streaming the file in chunks (never loading it wholly into memory). Files classified `missing` and
`added` within a single scan SHALL then be subject to move/rename reconciliation (see
"Content-addressed move/rename reconciliation") before alerts are routed and the run is finalized.

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

#### Scenario: A move is not reported as missing-plus-added

- **WHEN** a tracked file is moved/renamed to a new path within the corpus with unchanged content
- **THEN** the scan SHALL NOT report it as a `missing` file plus an `added` file, but SHALL
  reconcile it into a single moved file per the reconciliation requirement
