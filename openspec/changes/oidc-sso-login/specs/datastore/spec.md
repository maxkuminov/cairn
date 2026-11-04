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

Migration `0013` SHALL add to `users` the nullable columns `oidc_issuer` (at most 512
characters), `oidc_subject` (at most 255 characters) and `oidc_linked_at` (timestamp with time
zone), and a non-null integer `session_epoch` defaulting to 0. The pair (`oidc_issuer`,
`oidc_subject`) SHALL be UNIQUE, and a CHECK constraint SHALL require the three OIDC columns to
be all null or all non-null. The migration SHALL be reversible and SHALL leave existing rows
unlinked with `session_epoch` 0.

#### Scenario: Upgrade an existing database

- **WHEN** a database at revision `0012` with one user and several collections is upgraded to
  `0013`
- **THEN** the user SHALL be unlinked with `session_epoch` 0 and every collection SHALL keep its
  owner

#### Scenario: Duplicate link refused by the schema

- **WHEN** two users are given the same (`oidc_issuer`, `oidc_subject`) pair
- **THEN** the second write SHALL fail on the UNIQUE constraint

#### Scenario: Half-linked row refused by the schema

- **WHEN** a row is written with `oidc_subject` set and `oidc_issuer` null
- **THEN** the write SHALL fail on the CHECK constraint
