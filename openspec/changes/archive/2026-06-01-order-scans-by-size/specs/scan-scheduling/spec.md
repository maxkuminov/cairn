## MODIFIED Requirements

### Requirement: Background scheduler scans corpora on a staggered cadence

When enabled, the system SHALL run a single background loop that scans every corpus once on
startup and then re-scans each corpus on its own `hash_cadence_seconds`. Due corpora SHALL be
scanned sequentially (the scanner is the single writer) in ascending order of estimated scan cost
(cheapest first), so that quick corpora complete promptly and a long-running large-corpus scan is
deferred to the end of the pass rather than blocking the corpora behind it. Estimated cost SHALL be
derived from the corpus's tracked files — primarily total tracked bytes, since a full re-hash is
byte-bound — with a deterministic tie-break (e.g. tracked file count, then corpus `id`) so the
order is stable across ticks. Their first runs SHALL be offset so a fleet of corpora does not all
scan at once. A failure scanning one corpus SHALL be logged and SHALL NOT stop the loop or prevent
other corpora from scanning. The loop SHALL stop cleanly on application shutdown. The scheduler
SHALL be disableable (for cron-only deployments) without affecting freshness reporting.

#### Scenario: Corpora become due on their cadence

- **WHEN** a corpus's last scan is older than its `hash_cadence_seconds`
- **THEN** the scheduler SHALL select it as due and scan it, then defer its next scan by its
  cadence

#### Scenario: Due corpora are scanned cheapest-first

- **WHEN** more than one corpus is due on the same tick and they differ in estimated scan cost
- **THEN** the scheduler SHALL scan them in ascending cost order (smallest total tracked bytes
  first), so a large corpus is scanned after the smaller ones in that pass

#### Scenario: Scan order is deterministic when costs tie

- **WHEN** two due corpora have equal estimated scan cost
- **THEN** the scheduler SHALL order them by a stable tie-break (tracked file count, then corpus
  `id`) so the scan order does not vary between ticks

#### Scenario: One failing corpus does not stop scheduling

- **WHEN** scanning a corpus raises an error during a tick
- **THEN** the error SHALL be logged and the remaining due corpora SHALL still be scanned

#### Scenario: Scheduler disabled

- **WHEN** the scheduler is disabled by configuration
- **THEN** no background scan loop SHALL be started, while `/healthz` freshness SHALL still
  reflect runs produced by external `cairn scan` invocations
