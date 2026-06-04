## MODIFIED Requirements

### Requirement: Orphaned running runs are reconciled on startup

On application startup the system SHALL mark any leftover run still recorded as `result` =
`running` with no `finished` as terminated (result `interrupted`, `finished` set), since a restarted
process cannot have an operation still running. A run interrupted by process termination would
otherwise stay stuck at `running`. The `interrupted` terminal state SHALL be distinct from `error`
so that a benign restart-induced interruption is not conflated with a genuine scan failure.
`interrupted` SHALL be an allowed value of `runs.result` but SHALL be produced only by this
reconciliation — a scan/stamp/upgrade that runs to completion SHALL still finish `ok`, `partial`,
or `error`. This reconciliation SHALL clear any stale in-progress indicator and SHALL NOT block
starting a new operation on the affected corpus. Like `error`, an `interrupted` run SHALL NOT
refresh scan freshness (the dead-man's switch derives from `ok`/`partial` runs only).

#### Scenario: Leftover running run is cleared at startup

- **WHEN** the application starts and finds a `runs` row with `result` = `running` and no `finished`
- **THEN** that run SHALL be marked `interrupted` with `finished` set, so no corpus is shown as
  perpetually scanning and a new scan can be started

#### Scenario: Interruption is distinguished from failure

- **WHEN** a run is reconciled by startup reconciliation versus a scan that finishes with errors
- **THEN** the reconciled run SHALL carry `result` = `interrupted` while the failed scan SHALL carry
  `result` = `error`, so the two are distinguishable in the run record
