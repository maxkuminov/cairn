# ots-notarization Specification (delta)

## ADDED Requirements

### Requirement: Notarization operations do not block the application event loop

The application's asyncio event loop SHALL NOT be blocked by OpenTimestamps subprocess work or its
accompanying file IO. Every operation that shells out to the `ots` CLI — stamping (including the
batched stamp and its per-file fallback), upgrading incomplete proofs, and verifying a proof — and
any file-content work performed alongside them in a request handler (re-hashing a file for
verification, copying bytes for an export bundle) SHALL be executed off the event loop (for example,
via a worker thread) when invoked from asynchronous code, so that a single blocking subprocess or
file-IO call does not stall concurrent panel requests for the duration of a process spawn or a
calendar/explorer network round-trip. These operations MAY remain sequential (one `ots` subprocess
at a time); the requirement is only that the event loop stays free to service other work while a
call is in flight.

#### Scenario: An upgrade pass does not freeze the panel

- **WHEN** the daily pass upgrades a large number of `incomplete` proofs (each a blocking `ots
  upgrade` subprocess) while a user loads a panel page
- **THEN** the panel request SHALL be served without waiting for the upgrade subprocesses, because
  each `ots upgrade` runs off the event loop

#### Scenario: On-demand verify does not freeze the panel

- **WHEN** a user triggers a proof verification that re-hashes the file and runs `ots verify`
  (a network round-trip)
- **THEN** the re-hash and the verify SHALL run off the event loop, so other concurrent panel
  requests are not blocked for their duration

#### Scenario: Stamping runs off the loop

- **WHEN** a scan or an on-demand backfill stamps pending files via the `ots` CLI
- **THEN** each stamp subprocess (batched call and any per-file fallback) SHALL run off the event
  loop, leaving the panel responsive while stamping proceeds
