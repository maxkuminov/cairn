# app-runtime Specification

## Purpose
TBD - created by archiving change add-foundation. Update Purpose after archive.
## Requirements
### Requirement: Health endpoint for external polling

The system SHALL expose `GET /healthz` returning JSON describing liveness and per-corpus scan
freshness, suitable for an external monitor to poll as a dead-man's switch. Freshness SHALL be
derived from a corpus's newest successful **scan** run (`kind = 'scan'`) only — `stamp` and `upgrade`
runs SHALL NOT count toward freshness. A corpus is *fresh* when it has a successful scan run within
`max(2 × hash_cadence_seconds, freshness_floor)`, *pending* when it has no successful scan run yet but
is still within that startup grace, and *stale* otherwise.

The endpoint SHALL return:

- HTTP 200 with `status:"ok"` when the datastore is reachable and no corpus is stale;
- HTTP 503 with `status:"degraded"` when the datastore is reachable but at least one corpus is
  stale;
- HTTP 503 with `status:"error"` when the datastore is unreachable.

The body SHALL include the active auth `mode`, the version, and a per-corpus freshness list
(name, last-scan age, state).

#### Scenario: Healthy and fresh

- **WHEN** the datastore is reachable and every corpus has a successful scan run within its freshness
  window (or has none configured yet)
- **THEN** `/healthz` SHALL return HTTP 200 with `status:"ok"`

#### Scenario: A stale corpus trips the switch

- **WHEN** the datastore is reachable but at least one corpus has had no successful scan run within
  its freshness window
- **THEN** `/healthz` SHALL return HTTP 503 with `status:"degraded"` and the body SHALL flag the
  stale corpus

#### Scenario: A stamp or upgrade run does not refresh freshness

- **WHEN** a corpus is stale on its scan cadence but has a recent successful `stamp` or `upgrade` run
- **THEN** the corpus SHALL still be reported stale, because freshness counts `kind = 'scan'` runs
  only

#### Scenario: Datastore unreachable

- **WHEN** the datastore cannot be opened
- **THEN** `/healthz` SHALL return HTTP 503 with `status:"error"`

### Requirement: Application lifespan manages the datastore and a scheduler hook

On startup the application SHALL open the datastore, ensure the schema is migrated (when
auto-migrate is enabled), perform single-user bootstrap, and invoke a scheduler start hook. On
shutdown it SHALL stop the scheduler hook and close datastore resources cleanly.

#### Scenario: Clean startup and shutdown

- **WHEN** the application starts and then receives a shutdown signal
- **THEN** startup SHALL complete with the datastore open and the scheduler hook invoked
- **AND** shutdown SHALL close the datastore without unhandled errors

### Requirement: CLI exposes the full command surface

The `cairn` CLI SHALL expose the subcommands `init`, `serve`, `scan`, `accept`, `verify`,
`export`, `status`, `upgrade`, and `add-corpus`. In this change `init` and `serve` SHALL be
functional; the remaining subcommands SHALL exist, print a "not yet implemented" notice, and
exit with a non-zero status so later changes can implement them without altering the surface.

#### Scenario: init then serve

- **WHEN** an operator runs `cairn init` followed by `cairn serve`
- **THEN** `init` SHALL create the data and proof directories and migrate the database
- **AND** `serve` SHALL start the web application listening on the configured host and port

#### Scenario: Unimplemented subcommand signals clearly

- **WHEN** an operator runs a not-yet-implemented subcommand such as `cairn scan`
- **THEN** the CLI SHALL print a clear "not yet implemented" message and exit non-zero

