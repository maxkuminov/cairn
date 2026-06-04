# datastore Specification (delta)

## MODIFIED Requirements

### Requirement: The five locked tables exist with the specified shape

The datastore SHALL define `users`, `corpora`, `files`, `runs`, and `events` per DESIGN.md §5.
`corpora.ots_mode` SHALL be constrained to `none` or `perfile`. `files` SHALL be unique on
`(corpus_id, relpath)` and carry `status` ∈ {ok,new,modified,missing} and `ots_state` ∈
{none,pending,incomplete,complete}. `events.kind` SHALL be constrained to
{added,modified,missing,restored,moved}, and `events` SHALL carry a nullable `detail` TEXT column
(used to record the old → new path of a `moved` file). `runs` SHALL carry an integer `moved` count.
JSON-valued columns (`exclude_globs_json`, `alert_json`) SHALL be stored as TEXT. Deleting a corpus
SHALL cascade to its `files`, `runs`, and `events`.

#### Scenario: Initial migration creates the full schema

- **WHEN** `alembic upgrade head` is run against a fresh database file
- **THEN** all five tables SHALL be created with their foreign keys, the `(corpus_id, relpath)`
  uniqueness on `files`, and the `ots_mode`/`status`/`ots_state`/`kind` constraints
- **AND** `alembic downgrade base` SHALL drop them cleanly

#### Scenario: Cascade on corpus delete

- **WHEN** a `corpora` row is deleted
- **THEN** its `files`, `runs`, and `events` rows SHALL be deleted by cascade

#### Scenario: Move-detection migration adds the moved kind and counters

- **WHEN** the rename-detection Alembic revision is applied with `alembic upgrade head`
- **THEN** `events.kind` SHALL accept `moved`, `events.detail` SHALL exist as a nullable TEXT
  column, and `runs.moved` SHALL exist defaulting to 0, without altering existing rows
- **AND** `alembic downgrade` SHALL reverse the constraint change and drop the added columns
