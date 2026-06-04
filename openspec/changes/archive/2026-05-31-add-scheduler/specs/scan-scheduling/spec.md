## ADDED Requirements

### Requirement: Background scheduler scans corpora on a staggered cadence

When enabled, the system SHALL run a single background loop that scans every corpus once on
startup and then re-scans each corpus on its own `hash_cadence_seconds`. Due corpora SHALL be
scanned sequentially (the scanner is the single writer), and their first runs SHALL be offset so
a fleet of corpora does not all scan at once. A failure scanning one corpus SHALL be logged and
SHALL NOT stop the loop or prevent other corpora from scanning. The loop SHALL stop cleanly on
application shutdown. The scheduler SHALL be disableable (for cron-only deployments) without
affecting freshness reporting.

#### Scenario: Corpora become due on their cadence

- **WHEN** a corpus's last scan is older than its `hash_cadence_seconds`
- **THEN** the scheduler SHALL select it as due and scan it, then defer its next scan by its
  cadence

#### Scenario: One failing corpus does not stop scheduling

- **WHEN** scanning a corpus raises an error during a tick
- **THEN** the error SHALL be logged and the remaining due corpora SHALL still be scanned

#### Scenario: Scheduler disabled

- **WHEN** the scheduler is disabled by configuration
- **THEN** no background scan loop SHALL be started, while `/healthz` freshness SHALL still
  reflect runs produced by external `cairn scan` invocations

### Requirement: Daily OTS upgrade pass

The scheduler SHALL run an OTS upgrade pass across all corpora at a configured interval (default
daily), completing proofs that Bitcoin has confirmed and recording the number upgraded.

#### Scenario: Upgrade pass completes confirmed proofs

- **WHEN** the upgrade interval has elapsed and incomplete proofs have been confirmed by Bitcoin
- **THEN** the scheduler SHALL upgrade them to complete and record the upgraded count
