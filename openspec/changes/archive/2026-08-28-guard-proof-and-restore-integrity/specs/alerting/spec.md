# alerting Specification (delta)

## MODIFIED Requirements

### Requirement: Alert on newly-detected alarming changes, per-corpus routing

The system SHALL dispatch a single batched alert for a corpus to the channels enabled in its
`alert_json` when a scan newly detects an alarming change: a `missing` file (in any mode), a file
that reappeared with **different bytes** than the digest recorded for it (a `restored_changed` file,
in any mode — see the `integrity-scanning` requirement "A file that reappears with different bytes
is not reported as restored"), or a
`modified` file in a WORM corpus. Informational `added`, `restored`, and `moved` events, and churn
re-baselines, SHALL NOT trigger an alert. A file that the scan reconciles as a move (see the
`integrity-scanning` move/rename requirement) is not a `missing` change and SHALL NOT trigger an
alert. Alerts SHALL cover only changes newly detected in that scan, not the entire unacknowledged
backlog, so the operator is not re-nagged on every scan.

#### Scenario: Missing file triggers an alert

- **WHEN** a scan newly marks a file `missing` in a corpus with an enabled alert channel
- **THEN** the system SHALL dispatch one alert for that corpus summarizing the missing file(s)

#### Scenario: WORM modification triggers an alert

- **WHEN** a scan newly marks a file `modified` in a WORM corpus with an enabled channel
- **THEN** an alert SHALL be dispatched summarizing the modification

#### Scenario: A file that came back different triggers an alert in either mode

- **WHEN** a scan finds a file recorded `missing` back on disk with bytes that do not match the
  digest recorded for it, in a corpus with an enabled channel
- **THEN** an alert SHALL be dispatched for that corpus naming that file, in **worm and in churn
  mode alike**, because the operator restored something and what came back is not what left

#### Scenario: An identical restore does not alert

- **WHEN** a scan finds a file recorded `missing` back on disk with bytes matching the digest
  recorded for it
- **THEN** no alert SHALL be dispatched — this is the benign direction and its `restored` event is
  informational

#### Scenario: Churn modification does not alert

- **WHEN** a file changes in a churn corpus (a silent re-baseline, no event)
- **THEN** no alert SHALL be dispatched

#### Scenario: Reconciled move does not alert

- **WHEN** a scan reconciles a renamed/moved file (one `moved` event, no `missing` event)
- **THEN** no alert SHALL be dispatched for that change, in either worm or churn mode

#### Scenario: Nothing alarming means no alert

- **WHEN** a scan only adds new files (no missing, no WORM modification)
- **THEN** no alert SHALL be dispatched

