# datastore Specification (delta)

## MODIFIED Requirements

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
`collections.ots_mode` SHALL be constrained to `none` or `perfile`. `files` SHALL be unique on
`(collection_id, relpath)` and carry `status` ∈ {ok,new,modified,missing} and `ots_state` ∈
{none,pending,incomplete,complete}. `events.kind` SHALL be constrained to
{added,modified,missing,restored,moved}, and `events` SHALL carry a nullable `detail` TEXT column
(used to record the old → new path of a `moved` file). `runs` SHALL carry an integer `moved` count.
The `files`, `runs`, and `events` tables SHALL reference their owning collection through a
`collection_id` foreign key. JSON-valued columns (`exclude_globs_json`, `alert_json`) SHALL be
stored as TEXT. Deleting a collection SHALL cascade to its `files`, `runs`, and `events`.

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

### Requirement: Implicit single-user bootstrap

In `single` auth mode the system SHALL ensure exactly one implicit user row exists so that every
collection has an owner. This bootstrap SHALL be idempotent across restarts.

#### Scenario: Implicit user created once

- **WHEN** the application starts in `single` mode against a database with no users
- **THEN** it SHALL create one user row marked admin and active
- **AND** a subsequent restart SHALL NOT create a duplicate
