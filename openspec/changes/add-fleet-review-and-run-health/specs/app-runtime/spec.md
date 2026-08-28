# app-runtime Specification (delta)

## MODIFIED Requirements

### Requirement: Health endpoint for external polling

The system SHALL expose `GET /healthz` returning JSON describing liveness and per-corpus scan
freshness, suitable for an external monitor to poll as a dead-man's switch. `/healthz` SHALL report
**every** corpus in the deployment regardless of owner: it is the machine-facing monitor of the
installation, not a per-user view, and a monitor that silently omitted one user's corpora would be a
dead-man's switch with a hole in it.

Freshness SHALL be derived from a corpus's **scan** runs (`kind = 'scan'`) only — `stamp` and
`upgrade` runs SHALL NOT count toward freshness. A corpus is **fresh** when either

- **(a) a completed scan is recent** — its newest `kind = 'scan'` run with a result of `ok` or
  `partial` finished within `max(2 × hash_cadence_seconds, freshness_floor)`; **or**
- **(b) a scan is in flight and demonstrably alive** — a `kind = 'scan'` run is currently `running`
  and its claim is live, i.e. its last reported progress (its heartbeat, or its start time when it
  has not yet reported one) is within the operation-claim abandonment interval used to reclaim stale
  claims.

Leg (b) exists because a scan that legitimately takes longer than its own freshness window must not
age out its own freshness while it is still working — that is a false alarm manufactured by the
switch itself. Leg (b) SHALL be gated on liveness, not merely on the run being `running`: a run
whose claim has been abandoned (no progress within the abandonment interval) SHALL confer **no**
freshness, because a process that died mid-scan is exactly the condition the dead-man's switch
exists to report. The two legs SHALL use the same abandonment interval as claim reclamation, so the
switch and the reaper cannot disagree about which claims are alive.

A corpus is **pending** when it has no scan run at all but was created within the freshness window
(startup grace), and **stale** otherwise — including when its only scan run is `running` with an
abandoned claim, and when its only scan runs never completed.

The reported last-scan age SHALL describe the newest **completed** scan (`ok`/`partial`), and SHALL
be absent when the corpus has never completed one — a run that has not finished has no "last scan"
age to report, and reporting its elapsed time under that name would state a completion that has not
happened.

The endpoint SHALL return:

- HTTP 200 with `status:"ok"` when the datastore is reachable and no corpus is stale;
- HTTP 503 with `status:"degraded"` when the datastore is reachable but at least one corpus is
  stale;
- HTTP 503 with `status:"error"` when the datastore is unreachable.

The body SHALL include the active auth `mode`, the version, and a per-corpus freshness list
(identifier, name, last-scan age, state). The identifier is what lets a caller — the panel included
— match a freshness record to the corpus it describes without matching on a name, which no
constraint makes unique.

#### Scenario: Healthy and fresh

- **WHEN** the datastore is reachable and every corpus has a completed scan run within its freshness
  window (or has none configured yet)
- **THEN** `/healthz` SHALL return HTTP 200 with `status:"ok"`

#### Scenario: A stale corpus trips the switch

- **WHEN** the datastore is reachable but at least one corpus has had no completed scan run within
  its freshness window and no live in-flight scan
- **THEN** `/healthz` SHALL return HTTP 503 with `status:"degraded"` and the body SHALL flag the
  stale corpus

#### Scenario: A long scan that is still alive keeps its corpus fresh

- **WHEN** a corpus has no completed scan within its freshness window but a `kind = 'scan'` run is
  `running` and reported progress within the claim-abandonment interval
- **THEN** the corpus SHALL be reported fresh, so a scan that outlives its own cadence does not trip
  the switch against itself

#### Scenario: An abandoned in-flight scan confers no freshness

- **WHEN** a corpus's only recent scan run is `running` but has reported no progress for longer than
  the claim-abandonment interval, and it has no completed scan within its freshness window
- **THEN** the corpus SHALL be reported stale, because a run whose claim has been abandoned is the
  failure the switch exists to report, not evidence of a working scan

#### Scenario: No completed scan and no running scan is stale

- **WHEN** a corpus past its startup grace has no completed scan run and no `running` scan run
- **THEN** the corpus SHALL be reported stale

#### Scenario: The reported age describes a completed scan

- **WHEN** a corpus is fresh only because a scan is in flight, and it has never completed a scan
- **THEN** its last-scan age SHALL be absent rather than reporting the in-flight run's elapsed time

#### Scenario: A stamp or upgrade run does not refresh freshness

- **WHEN** a corpus is stale on its scan cadence but has a recent successful `stamp` or `upgrade` run
- **THEN** the corpus SHALL still be reported stale, because freshness counts `kind = 'scan'` runs
  only

#### Scenario: Datastore unreachable

- **WHEN** the datastore cannot be opened
- **THEN** `/healthz` SHALL return HTTP 503 with `status:"error"`

#### Scenario: Each freshness record identifies its corpus

- **WHEN** `/healthz` reports per-corpus freshness
- **THEN** each entry SHALL carry the corpus's identifier alongside its name, state and last-scan age

#### Scenario: The endpoint reports every owner's corpora

- **WHEN** `/healthz` is polled in multi-user mode with corpora belonging to more than one owner
- **THEN** every corpus SHALL appear in the freshness list and any stale corpus SHALL set
  `status:"degraded"`, regardless of which user owns it
