## MODIFIED Requirements

### Requirement: Each scan records a run

Every scan of a corpus SHALL create a `runs` row capturing start and finish times, the counts of
added/modified/missing/stamped/upgraded, and a result of `ok`, `partial`, or `error`. Per-file IO
or permission errors SHALL be counted and SHALL NOT abort the whole scan.

A run SHALL carry a `kind`; an integrity scan SHALL have `kind = 'scan'`. The run SHALL record a
`processed` count of files handled so far, updated as the scan progresses (not only at the end), so
an in-flight scan's progress is observable by a concurrent reader. The run MAY carry a `total`
estimate of the files the scan will cover (e.g. the prior scan's processed count) for a progress
figure; when no estimate is available the `total` SHALL be absent (the scan reports indeterminate
progress). The result SHALL be `running` while the scan is in progress and SHALL transition to its
terminal value (`ok`, `partial`, or `error`) with `finished` set when the scan ends.

#### Scenario: Successful scan records counts

- **WHEN** a scan completes without fatal error
- **THEN** its `runs` row SHALL have `kind` = `scan`, `finished` set, the added/modified/missing
  counts populated, and `result` = `ok`

#### Scenario: In-progress scan exposes a growing processed count

- **WHEN** a scan is in progress over a corpus with many files
- **THEN** its `runs` row SHALL have `result` = `running` and a `processed` count that reflects files
  handled so far, observable by a concurrent reader before the scan finishes

#### Scenario: First-ever scan has no progress estimate

- **WHEN** a corpus is scanned for the first time with no prior completed scan to estimate from
- **THEN** the run SHALL carry no `total` estimate, so its progress is reported as indeterminate

#### Scenario: Unreadable file does not abort the scan

- **WHEN** one file under the root cannot be read (permissions/IO)
- **THEN** the scan SHALL continue processing the remaining files and SHALL finish with
  `result` = `partial` or `error`

## ADDED Requirements

### Requirement: Orphaned running runs are reconciled on startup

On application startup the system SHALL mark any leftover run still recorded as `result` =
`running` with no `finished` as terminated (result `error`, `finished` set), since a restarted
process cannot have an operation still running. A run interrupted by process termination would
otherwise stay stuck at `running`. This reconciliation SHALL clear any stale in-progress indicator
and SHALL NOT block starting a new operation on the affected corpus.

#### Scenario: Leftover running run is cleared at startup

- **WHEN** the application starts and finds a `runs` row with `result` = `running` and no `finished`
- **THEN** that run SHALL be marked `error` with `finished` set, so no corpus is shown as
  perpetually scanning and a new scan can be started
