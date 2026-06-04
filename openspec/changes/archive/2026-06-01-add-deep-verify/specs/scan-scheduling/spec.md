## ADDED Requirements

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
