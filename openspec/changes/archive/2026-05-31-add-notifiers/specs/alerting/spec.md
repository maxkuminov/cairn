## ADDED Requirements

### Requirement: Alert on newly-detected alarming changes, per-corpus routing

The system SHALL dispatch a single batched alert for a corpus to the channels enabled in its
`alert_json` when a scan newly detects an alarming change: a `missing` file (in any mode) or a
`modified` file in a WORM corpus. Informational `added` events and churn re-baselines SHALL NOT
trigger an alert. Alerts SHALL cover only changes newly detected in that scan, not the entire
unacknowledged backlog, so the operator is not re-nagged on every scan.

#### Scenario: Missing file triggers an alert

- **WHEN** a scan newly marks a file `missing` in a corpus with an enabled alert channel
- **THEN** the system SHALL dispatch one alert for that corpus summarizing the missing file(s)

#### Scenario: WORM modification triggers an alert

- **WHEN** a scan newly marks a file `modified` in a WORM corpus with an enabled channel
- **THEN** an alert SHALL be dispatched summarizing the modification

#### Scenario: Churn modification does not alert

- **WHEN** a file changes in a churn corpus (a silent re-baseline, no event)
- **THEN** no alert SHALL be dispatched

#### Scenario: Nothing alarming means no alert

- **WHEN** a scan only adds new files (no missing, no WORM modification)
- **THEN** no alert SHALL be dispatched

### Requirement: Pluggable channels with email active, others scaffolded

The system SHALL provide pluggable notification channels behind a common notifier interface, with
SMTP email as the implemented, active channel and webhook / ntfy / Signal (CallMeBot) / Kuma-push
as scaffolded channels. Channel credentials SHALL come from configuration (env/secret), never
hardcoded; per-corpus routing parameters (recipients, URLs) live in the corpus `alert_json`.

#### Scenario: Email is composed and sent via SMTP

- **WHEN** an alert is dispatched to an enabled email channel
- **THEN** the system SHALL compose a subject and body identifying the corpus and the change and
  send it via the configured SMTP server to the configured recipient(s)

#### Scenario: Only enabled channels receive the alert

- **WHEN** a corpus's `alert_json` enables some channels and disables others
- **THEN** the dispatch SHALL send only to the enabled channels

### Requirement: Dispatch is best-effort and never breaks a scan

A failure sending to one channel SHALL be logged and SHALL NOT prevent other channels from
receiving the alert, and SHALL NOT cause the scan that produced the alert to fail.

#### Scenario: One channel failing does not stop the others or the scan

- **WHEN** one enabled channel raises an error during dispatch
- **THEN** the remaining enabled channels SHALL still be attempted
- **AND** the scan that triggered the dispatch SHALL still complete and record its run
