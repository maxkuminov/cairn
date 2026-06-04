## ADDED Requirements

### Requirement: SQLite runs in WAL mode with foreign keys enforced

Every database connection the system opens SHALL have `journal_mode=WAL` and `foreign_keys=ON`
set, along with a non-zero `busy_timeout`. WAL allows panel reads to proceed concurrently with
the scanner's writes; enforced foreign keys guarantee referential integrity across `corpora`,
`files`, `runs`, and `events`.

#### Scenario: Pragmas are active on a fresh connection

- **WHEN** the application opens a new database connection and queries `PRAGMA journal_mode` and
  `PRAGMA foreign_keys`
- **THEN** `journal_mode` SHALL be `wal` and `foreign_keys` SHALL be `1`

#### Scenario: Foreign key violation is rejected

- **WHEN** code attempts to insert a `corpora` row whose `user_id` does not exist
- **THEN** the database SHALL reject the insert with an integrity error

### Requirement: The five locked tables exist with the specified shape

The datastore SHALL define `users`, `corpora`, `files`, `runs`, and `events` per DESIGN.md §5.
`corpora.ots_mode` SHALL be constrained to `none` or `perfile`. `files` SHALL be unique on
`(corpus_id, relpath)` and carry `status` ∈ {ok,new,modified,missing} and `ots_state` ∈
{none,pending,incomplete,complete}. JSON-valued columns (`exclude_globs_json`, `alert_json`)
SHALL be stored as TEXT. Deleting a corpus SHALL cascade to its `files`, `runs`, and `events`.

#### Scenario: Initial migration creates the full schema

- **WHEN** `alembic upgrade head` is run against a fresh database file
- **THEN** all five tables SHALL be created with their foreign keys, the `(corpus_id, relpath)`
  uniqueness on `files`, and the `ots_mode`/`status`/`ots_state` constraints
- **AND** `alembic downgrade base` SHALL drop them cleanly

#### Scenario: Cascade on corpus delete

- **WHEN** a `corpora` row is deleted
- **THEN** its `files`, `runs`, and `events` rows SHALL be deleted by cascade

### Requirement: Implicit single-user bootstrap

In `single` auth mode the system SHALL ensure exactly one implicit user row exists so that every
corpus has an owner. This bootstrap SHALL be idempotent across restarts.

#### Scenario: Implicit user created once

- **WHEN** the application starts in `single` mode against a database with no users
- **THEN** it SHALL create one user row marked admin and active
- **AND** a subsequent restart SHALL NOT create a duplicate
