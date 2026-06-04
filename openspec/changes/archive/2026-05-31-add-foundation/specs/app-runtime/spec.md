## ADDED Requirements

### Requirement: Health endpoint for external polling

The system SHALL expose `GET /healthz` returning JSON describing liveness and scan freshness.
It SHALL return HTTP 200 when the datastore is reachable and HTTP 503 when it is not. The
freshness block MAY be a stub until the scheduler exists, but the endpoint and its JSON shape
SHALL be stable so external monitors can poll it as a dead-man's switch.

#### Scenario: Healthy liveness

- **WHEN** a client requests `GET /healthz` and the datastore is reachable
- **THEN** the response SHALL be HTTP 200 with a JSON body containing `status` and the active
  auth `mode`

#### Scenario: Datastore unreachable

- **WHEN** the datastore cannot be opened
- **THEN** `GET /healthz` SHALL return HTTP 503

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
