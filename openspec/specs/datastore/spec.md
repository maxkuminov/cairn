# datastore Specification

## Purpose
TBD - created by archiving change add-foundation. Update Purpose after archive.
## Requirements
### Requirement: SQLite runs in WAL mode with foreign keys enforced

Every database connection the system opens SHALL have `journal_mode=WAL` and `foreign_keys=ON`
set, along with a non-zero `busy_timeout`. WAL allows panel reads to proceed concurrently with
the scanner's writes; enforced foreign keys guarantee referential integrity across `collections`,
`files`, `runs`, and `events`.

#### Scenario: Pragmas are active on a fresh connection

- **WHEN** the application opens a new database connection and queries `PRAGMA journal_mode` and
  `PRAGMA foreign_keys`
- **THEN** `journal_mode` SHALL be `wal` and `foreign_keys` SHALL be `1`

#### Scenario: Foreign key violation is rejected

- **WHEN** code attempts to insert a `collections` row whose `user_id` does not exist
- **THEN** the database SHALL reject the insert with an integrity error

### Requirement: The five locked tables exist with the specified shape

The datastore SHALL define `users`, `collections`, `files`, `runs`, and `events` per DESIGN.md §5.
`collections.ots_mode` SHALL be constrained to `none` or `perfile`, and `collections` SHALL carry a
boolean `auto_baseline_new` (default false) controlling whether the deep-verify pass promotes intact
`new` files to `ok`. `files` SHALL be unique on `(collection_id, relpath)` and carry `status` ∈
{ok,new,modified,missing} and `ots_state` ∈ {none,pending,incomplete,complete}. `files` SHALL also
carry a nullable `ots_digest` TEXT column recording the digest the proof stored at `ots_path`
commits to. `events.kind` SHALL
be constrained to {added,modified,missing,restored,moved,restored_changed}, and `events` SHALL carry
a nullable `detail` TEXT column (used to record the old → new path of a `moved` file, and both
digests of a `restored_changed` file). `runs` SHALL carry an
integer `moved` count and a nullable `heartbeat_at` timestamp recording when the run last reported
progress, so that a claim held by a live process is distinguishable from one orphaned by a crash. The `files`, `runs`, and `events` tables SHALL reference their owning
collection through a `collection_id` foreign key. JSON-valued columns (`exclude_globs_json`,
`alert_json`) SHALL be stored as TEXT. Deleting a collection SHALL cascade to its `files`, `runs`,
and `events`.

#### Scenario: Initial migration creates the full schema

- **WHEN** `alembic upgrade head` is run against a fresh database file
- **THEN** all five tables SHALL be created with their foreign keys, the `(collection_id, relpath)`
  uniqueness on `files`, and the `ots_mode`/`status`/`ots_state`/`kind` constraints
- **AND** `alembic downgrade base` SHALL drop them cleanly

#### Scenario: Cascade on collection delete

- **WHEN** a `collections` row is deleted
- **THEN** its `files`, `runs`, and `events` rows SHALL be deleted by cascade

#### Scenario: Move-detection migration adds the moved kind and counters

- **WHEN** the rename-detection Alembic revision is applied with `alembic upgrade head`
- **THEN** `events.kind` SHALL accept `moved`, `events.detail` SHALL exist as a nullable TEXT
  column, and `runs.moved` SHALL exist defaulting to 0, without altering existing rows
- **AND** `alembic downgrade` SHALL reverse the constraint change and drop the added columns

#### Scenario: Rename migration renames the table and columns preserving rows

- **WHEN** the corpus→collection rename Alembic revision is applied with `alembic upgrade head`
  against a database holding existing data
- **THEN** the `corpora` table SHALL be renamed to `collections`, every `corpus_id` column on
  `files`/`runs`/`events` SHALL be renamed to `collection_id` with its foreign key repointed to
  `collections.id`, and all existing rows SHALL be preserved (row counts unchanged)
- **AND** `alembic downgrade` SHALL reverse the rename back to `corpora` / `corpus_id`

#### Scenario: Auto-baseline migration adds the column defaulting off

- **WHEN** the auto-baseline Alembic revision is applied with `alembic upgrade head`
- **THEN** `collections.auto_baseline_new` SHALL exist as a NOT NULL boolean defaulting to false,
  without altering existing rows
- **AND** `alembic downgrade` SHALL drop the column

#### Scenario: Proof-provenance migration adds the digest column without rewriting rows

- **WHEN** the proof-provenance Alembic revision is applied with `alembic upgrade head` against a
  database holding existing files
- **THEN** `files.ots_digest` SHALL exist as a nullable TEXT column, every existing row's value
  SHALL be NULL, and no existing row SHALL be otherwise altered

#### Scenario: The same migration adds the run liveness column without rewriting rows

- **WHEN** that revision is applied against a database holding existing runs
- **THEN** `runs.heartbeat_at` SHALL exist as a nullable timestamp column, every existing row's value
  SHALL be NULL — a run that reports no liveness falls back to its start time — and no existing row
  SHALL be otherwise altered

A migration that widens an event-kind constraint SHALL NOT reverse itself by discarding or
reinterpreting the rows that constraint now admits. Where a downgrade would narrow `events.kind`
while rows of the removed kind exist, it SHALL **refuse**, raising an error naming the kind and how
many rows carry it, and SHALL leave the database untouched. Deleting those rows would destroy the
audit record of the incidents the kind was added to record; rewriting them to an older kind would
assert something the system never detected — and in a `churn` collection would convert an alarm into
a silently re-baselined change. The migration cannot reverse itself without a judgement about
evidence, so it hands that judgement to the operator rather than making it silently. Where no such
rows exist the downgrade SHALL proceed normally.

#### Scenario: The restored-changed migration widens the event-kind constraint

- **WHEN** the same revision is applied
- **THEN** `events.kind` SHALL accept `restored_changed` in addition to the existing kinds, existing
  event rows SHALL be preserved unchanged

#### Scenario: Downgrading past the widened constraint is refused while such rows exist

- **WHEN** `alembic downgrade` is run past that revision against a database holding at least one
  `restored_changed` event
- **THEN** the downgrade SHALL fail with an error naming the kind and the number of rows carrying it,
  and every table SHALL be left exactly as it was — no event row deleted, rewritten to another kind,
  or otherwise altered

#### Scenario: Downgrading past the widened constraint succeeds with no such rows

- **WHEN** `alembic downgrade` is run past that revision against a database holding events but no
  `restored_changed` event
- **THEN** the downgrade SHALL restore the previous `events.kind` constraint, drop `files.ots_digest`
  and `runs.heartbeat_at`, and preserve every existing row

### Requirement: Implicit single-user bootstrap

In `single` auth mode the system SHALL ensure exactly one implicit user row exists so that every
collection has an owner. This bootstrap SHALL be idempotent across restarts.

#### Scenario: Implicit user created once

- **WHEN** the application starts in `single` mode against a database with no users
- **THEN** it SHALL create one user row marked admin and active
- **AND** a subsequent restart SHALL NOT create a duplicate

