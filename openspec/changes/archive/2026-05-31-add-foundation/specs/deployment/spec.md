## ADDED Requirements

### Requirement: Watched corpora are mounted read-only, separate from writable state

The container deployment SHALL mount watched corpus directories read-only and SHALL place the
SQLite database and the OpenTimestamps proof store on a separate read-write volume. Cairn
therefore cannot modify or delete the files it watches (the integrity tool cannot become the
threat). The container SHALL run as a non-root user with dropped Linux capabilities and
`no-new-privileges`.

#### Scenario: Corpus mount is read-only

- **WHEN** the deployment is configured per the shipped `docker-compose.yml`
- **THEN** each corpus host path SHALL be bind-mounted with the `:ro` flag
- **AND** the database and proof-store paths SHALL be mounted read-write on a different volume

### Requirement: `make deploy` builds, backs up, and recreates the service

The project SHALL provide a `Makefile` whose `deploy` target builds the image, pushes it,
backs up the SQLite database, and recreates the running container from the compose file. Host-
specific paths SHALL be supplied via a gitignored `Makefile.local` (e.g.
`DEPLOY_DIR=/srv/cairn`), never committed.

#### Scenario: Deploy performs an online SQLite backup before recreate

- **WHEN** an operator runs `make deploy`
- **THEN** the database SHALL be backed up using SQLite's online `.backup` mechanism before the
  container is recreated
- **AND** the container SHALL then be recreated from the compose file in `DEPLOY_DIR`

#### Scenario: Host paths are not committed

- **WHEN** the repository is inspected
- **THEN** `Makefile.local` SHALL be gitignored and absent from version control, while the
  tracked `Makefile` SHALL default `DEPLOY_DIR` to the repo root
