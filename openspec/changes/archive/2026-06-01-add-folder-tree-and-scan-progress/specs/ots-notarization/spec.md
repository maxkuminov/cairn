## ADDED Requirements

### Requirement: Stamp and upgrade operations are recorded as typed runs with progress

The on-demand stamp backfill and the OTS upgrade pass SHALL each be recorded as a `runs` row with a
`kind` distinguishing it from an integrity scan — `kind = 'stamp'` for the stamp backfill and
`kind = 'upgrade'` for the upgrade pass. Each such run SHALL set `total` to the number of items it
will process — the count of files queued for stamping, or the count of incomplete proofs to upgrade —
known at the start, and SHALL update `processed` as it advances, so a concurrent reader can observe
exact progress. The run's result SHALL be `running` while in progress and SHALL transition to a
terminal value with `finished` set when it ends.

These `stamp` and `upgrade` runs SHALL NOT affect scan-freshness reporting (the dead-man's switch),
which is derived from `kind = 'scan'` runs only. The upgrade pass SHALL record a run only for a corpus
that actually has incomplete proofs to process (it SHALL NOT create an empty run when there is no
work). Recording these runs SHALL NOT change the batched stamping or upgrade mechanics or their
per-file outcomes.

#### Scenario: Stamp backfill records a stamp run with exact progress

- **WHEN** the on-demand stamp backfill runs over a `perfile` corpus with N files queued
- **THEN** a `runs` row with `kind = 'stamp'` SHALL be created with `total` = N, `processed`
  advancing as batches are stamped, and a terminal result with `finished` set when it completes

#### Scenario: Upgrade pass records an upgrade run that does not affect freshness

- **WHEN** the upgrade pass processes a corpus that has incomplete proofs
- **THEN** a `runs` row with `kind = 'upgrade'` SHALL be created with `total` = the count of
  incomplete proofs and `processed` advancing as they are upgraded
- **AND** that run SHALL NOT count toward the corpus's scan freshness

#### Scenario: Upgrade pass with no incomplete proofs records nothing

- **WHEN** the upgrade pass processes a corpus that has no incomplete proofs
- **THEN** no `kind = 'upgrade'` run SHALL be created for that corpus
