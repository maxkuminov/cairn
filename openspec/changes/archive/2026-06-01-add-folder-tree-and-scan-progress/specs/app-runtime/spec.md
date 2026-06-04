## MODIFIED Requirements

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
