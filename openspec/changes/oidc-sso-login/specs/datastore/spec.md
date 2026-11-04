# Delta: datastore (oidc-sso-login)

## MODIFIED Requirements

### Requirement: Implicit single-user bootstrap

In `single` and `oidc` auth modes the system SHALL ensure exactly one implicit user row exists so
that every collection has an owner. This bootstrap SHALL be idempotent across restarts. In `oidc`
mode the bootstrap SHALL NOT link the implicit user to any identity; linking is done only through
the CLI.

#### Scenario: Implicit user created once

- **WHEN** the application starts in `single` mode against a database with no users
- **THEN** it SHALL create one user row marked admin and active
- **AND** a subsequent restart SHALL NOT create a duplicate

#### Scenario: OIDC mode keeps the existing owner

- **WHEN** an installation that ran in `single` mode is restarted in `oidc` mode
- **THEN** no user row SHALL be created, and every collection SHALL keep its owner

## ADDED Requirements

### Requirement: Users SHALL carry an optional pinned OIDC identity and a session epoch

Migration `0013` SHALL add to `users` the nullable columns `oidc_issuer`, `oidc_subject` and
`oidc_linked_at` (timestamp with time zone), and a non-null integer `session_epoch` defaulting to
0. The pair (`oidc_issuer`, `oidc_subject`) SHALL be unique through a unique index. CHECK
constraints SHALL limit `oidc_issuer` to at most 512 characters and `oidc_subject` to at most 255
characters (by `length()`, since SQLite does not enforce declared string lengths) and SHALL
require the three OIDC columns to be all null or all non-null. The migration SHALL alter `users`
in place with `ADD COLUMN` and `DROP COLUMN` and SHALL NOT rebuild, copy or drop the `users`
table in either direction; it SHALL refuse to run on a SQLite older than 3.35 rather than fall
back to a rebuild. It SHALL be reversible and SHALL leave existing rows unlinked with
`session_epoch` 0.

#### Scenario: Upgrade an existing database

- **WHEN** a database at revision `0012` with one user and several collections is upgraded to
  `0013`
- **THEN** the user SHALL be unlinked with `session_epoch` 0 and every collection SHALL keep its
  owner

#### Scenario: No row is lost in either direction

- **WHEN** a populated `0012` database (two users, collections owned by each, files, runs, and
  events with `acknowledged_by` set) is upgraded to `0013` with foreign keys enforced and then
  downgraded to `0012`
- **THEN** after each step the row counts and ID sets of `users`, `collections`, `files`, `runs`
  and `events`, every `collections.user_id` and every `events.acknowledged_by` SHALL equal the
  values recorded before the upgrade
- **AND** `PRAGMA foreign_key_check` SHALL report nothing
- **AND** neither step SHALL execute `CREATE TABLE`, `DROP TABLE` or a table rename on `users`

#### Scenario: Length bounds enforced by the schema

- **WHEN** a linked row is written directly with an `oidc_issuer` of 513 characters or an
  `oidc_subject` of 256 characters
- **THEN** the write SHALL fail on a CHECK constraint
- **AND** values of exactly 512 and 255 characters SHALL be accepted

#### Scenario: Duplicate link refused by the schema

- **WHEN** two users are given the same (`oidc_issuer`, `oidc_subject`) pair
- **THEN** the second write SHALL fail on the UNIQUE constraint

#### Scenario: Half-linked row refused by the schema

- **WHEN** a row is written with `oidc_subject` set and `oidc_issuer` null
- **THEN** the write SHALL fail on the CHECK constraint
