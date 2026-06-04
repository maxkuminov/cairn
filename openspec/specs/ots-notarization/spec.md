# ots-notarization Specification

## Purpose
TBD - created by archiving change add-ots-notary. Update Purpose after archive.
## Requirements
### Requirement: Stamp a file's hash into a parallel proof store

The system SHALL stamp a file's SHA-256 to the OpenTimestamps calendars and store the resulting
`.ots` proof in a writable proof store laid out parallel to the corpus, WITHOUT writing anything
under the read-only corpus root. After a successful stamp the file's `ots_state` SHALL be
`incomplete`, with `ots_path` and `ots_stamped_at` recorded. Files in a `none` (tripwire) corpus
SHALL never be stamped.

#### Scenario: Stamp writes only to the proof store

- **WHEN** a file in a `perfile` corpus is stamped
- **THEN** a `.ots` proof SHALL be written under the proof store at a path derived from the
  corpus id and the file's relative path
- **AND** no file SHALL be created or modified under the corpus root
- **AND** the file's `ots_state` SHALL become `incomplete`

#### Scenario: Tripwire corpus is never stamped

- **WHEN** a scan processes a corpus whose `ots_mode` is `none`
- **THEN** no proof SHALL be created and every file's `ots_state` SHALL remain `none`

### Requirement: Upgrade incomplete proofs after Bitcoin confirms

The system SHALL upgrade `incomplete` proofs by contacting the calendars; when the Bitcoin
attestation is available the proof SHALL be rewritten complete and the file's `ots_state` set to
`complete`. A proof that has not yet been confirmed SHALL remain `incomplete` and SHALL NOT be
treated as an error.

#### Scenario: Confirmed proof becomes complete

- **WHEN** `upgrade` runs against an incomplete proof that Bitcoin has now confirmed
- **THEN** the proof SHALL be rewritten with the Bitcoin attestation and the file's `ots_state`
  SHALL become `complete`

#### Scenario: Unconfirmed proof stays incomplete

- **WHEN** `upgrade` runs against a proof the calendars have not yet anchored
- **THEN** the file SHALL remain `incomplete` and the operation SHALL NOT raise an error

### Requirement: Verify a proof by digest

The system SHALL verify a stored proof against a file's SHA-256 digest without requiring the
original file to be shipped anywhere. The result SHALL state whether the proof is verified and,
when complete, the Bitcoin block and the "existed by" date.

#### Scenario: Verify a complete proof

- **WHEN** a complete proof is verified against the matching digest
- **THEN** the result SHALL be verified, naming the Bitcoin block and an "existed by" UTC date

#### Scenario: Digest mismatch fails verification

- **WHEN** a proof is verified against a digest that does not match it
- **THEN** the result SHALL be not-verified

### Requirement: Export a portable proof bundle

The system SHALL export a file together with its `.ots` proof to a chosen destination so a third
party can verify independently. Export SHALL fail clearly if the file has no stored proof.

#### Scenario: Export writes file and proof

- **WHEN** export is requested for a stamped file
- **THEN** both the file's bytes and its `.ots` proof SHALL be written to the destination

### Requirement: Flag proofs stuck incomplete

The system SHALL be able to list proofs that have remained `incomplete` longer than a configured
number of days, so a never-confirmed proof can be surfaced and re-stamped.

#### Scenario: Stale incomplete proof is listed

- **WHEN** a proof has been `incomplete` for longer than the configured alarm threshold
- **THEN** it SHALL appear in the stale-incomplete list

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

### Requirement: Notarization operations do not block the application event loop

The application's asyncio event loop SHALL NOT be blocked by OpenTimestamps subprocess work or its
accompanying file IO. Every operation that shells out to the `ots` CLI — stamping (including the
batched stamp and its per-file fallback), upgrading incomplete proofs, and verifying a proof — and
any file-content work performed alongside them in a request handler (re-hashing a file for
verification, copying bytes for an export bundle) SHALL be executed off the event loop (for example,
via a worker thread) when invoked from asynchronous code, so that a single blocking subprocess or
file-IO call does not stall concurrent panel requests for the duration of a process spawn or a
calendar/explorer network round-trip. These operations MAY remain sequential (one `ots` subprocess
at a time); the requirement is only that the event loop stays free to service other work while a
call is in flight.

#### Scenario: An upgrade pass does not freeze the panel

- **WHEN** the daily pass upgrades a large number of `incomplete` proofs (each a blocking `ots
  upgrade` subprocess) while a user loads a panel page
- **THEN** the panel request SHALL be served without waiting for the upgrade subprocesses, because
  each `ots upgrade` runs off the event loop

#### Scenario: On-demand verify does not freeze the panel

- **WHEN** a user triggers a proof verification that re-hashes the file and runs `ots verify`
  (a network round-trip)
- **THEN** the re-hash and the verify SHALL run off the event loop, so other concurrent panel
  requests are not blocked for their duration

#### Scenario: Stamping runs off the loop

- **WHEN** a scan or an on-demand backfill stamps pending files via the `ots` CLI
- **THEN** each stamp subprocess (batched call and any per-file fallback) SHALL run off the event
  loop, leaving the panel responsive while stamping proceeds

