# datastore Specification (delta)

## MODIFIED Requirements

### Requirement: The five locked tables exist with the specified shape

The datastore SHALL define `users`, `collections`, `files`, `runs`, and `events` per DESIGN.md §5.
`collections.ots_mode` SHALL be constrained to `none` or `perfile`, and `collections` SHALL carry a
boolean `auto_baseline_new` (default false) controlling whether the deep-verify pass promotes intact
`new` files to `ok`. `files` SHALL be unique on `(collection_id, relpath)` and carry `status` ∈
{ok,new,modified,missing} and `ots_state` ∈ {none,pending,incomplete,complete}. `events.kind` SHALL
be constrained to {added,modified,missing,restored,moved}, and `events` SHALL carry a nullable
`detail` TEXT column (used to record the old → new path of a `moved` file). `runs` SHALL carry an
integer `moved` count. The `files`, `runs`, and `events` tables SHALL reference their owning
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
