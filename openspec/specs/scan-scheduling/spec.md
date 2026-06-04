# scan-scheduling Specification

## Purpose
TBD - created by archiving change add-scheduler. Update Purpose after archive.
## Requirements
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

The scheduler SHALL NOT start a scan for a corpus that already has an operation in progress (a run
with result `running`, such as a manually triggered scan or stamp backfill); it SHALL skip that
corpus for the tick rather than start a second concurrent writer on the same corpus.

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

#### Scenario: A corpus with an operation in progress is skipped

- **WHEN** a corpus is due but already has a run in progress (e.g. a manual scan or stamp backfill)
- **THEN** the scheduler SHALL skip it for that tick rather than start a second concurrent operation,
  and SHALL consider it again on a later tick

#### Scenario: One failing corpus does not stop scheduling

- **WHEN** scanning a corpus raises an error during a tick
- **THEN** the error SHALL be logged and the remaining due corpora SHALL still be scanned

#### Scenario: Scheduler disabled

- **WHEN** the scheduler is disabled by configuration
- **THEN** no background scan loop SHALL be started, while `/healthz` freshness SHALL still
  reflect runs produced by external `cairn scan` invocations

### Requirement: Daily OTS upgrade pass

The scheduler SHALL run an OTS upgrade pass across all corpora at a configured interval (default
daily), completing proofs that Bitcoin has confirmed and recording the number upgraded. For a corpus
that has incomplete proofs to process, the pass SHALL record the work as a `kind = 'upgrade'` run
(with progress) rather than amending a scan run; because freshness is derived from `kind = 'scan'`
runs only, this `upgrade` run SHALL NOT refresh the corpus's scan-freshness dead-man's switch.

#### Scenario: Upgrade pass completes confirmed proofs

- **WHEN** the upgrade interval has elapsed and incomplete proofs have been confirmed by Bitcoin
- **THEN** the scheduler SHALL upgrade them to complete and record the upgraded count in a
  `kind = 'upgrade'` run

#### Scenario: Upgrade run does not refresh scan freshness

- **WHEN** the upgrade pass records a `kind = 'upgrade'` run for a corpus
- **THEN** that run SHALL NOT count toward the corpus's scan freshness, which keys on `kind = 'scan'`
  runs only

### Requirement: Periodic deep verify on a per-corpus cadence

The scheduler SHALL run a deep verify pass for a corpus when its `verify_cadence_seconds` has
elapsed since `last_full_scan_at` (measured by wall-clock so an overdue deep pass survives a
restart), where `verify_cadence_seconds` of `0` disables deep verify and a corpus that has never
been deep-scanned SHALL be treated as owed. Because a deep pass is a superset of a quick scan,
when a deep pass is owed on a corpus's due tick it SHALL replace — not run in addition to — the
quick pass for that tick. The scheduler SHALL run at most one deep pass per tick so a long
re-hash does not starve other corpora; remaining owed corpora SHALL fall back to a quick pass and
get their deep pass on a later tick. `last_full_scan_at` SHALL be updated only after a deep pass
completes successfully.

#### Scenario: Deep pass is owed and replaces the quick pass

- **WHEN** a corpus is due and its deep cadence has elapsed since its last full scan
- **THEN** the scheduler SHALL scan it in deep mode, record `last_full_scan_at`, and SHALL NOT
  additionally run a quick pass for that corpus that tick

#### Scenario: Deep verify disabled

- **WHEN** a corpus has `verify_cadence_seconds` of `0`
- **THEN** the scheduler SHALL never run a deep pass for it, regardless of `last_full_scan_at`

#### Scenario: One deep pass per tick

- **WHEN** more than one corpus is owed a deep pass on the same tick
- **THEN** the scheduler SHALL run a deep pass for only one of them that tick and SHALL run the
  others as quick passes, deferring their deep passes to later ticks

#### Scenario: Failed deep pass is retried

- **WHEN** a deep pass raises an error before completing
- **THEN** `last_full_scan_at` SHALL NOT be advanced, so the deep pass is retried on a later tick

