## MODIFIED Requirements

### Requirement: Alert on newly-detected alarming changes, per-corpus routing

The system SHALL dispatch a single batched alert for a corpus to the channels enabled in its
`alert_json` when a scan newly detects an alarming change: a `missing` file (in any mode), a file
that reappeared with **different bytes** than the digest recorded for it (a `restored_changed` file,
in any mode — see the `integrity-scanning` requirement "A file that reappears with different bytes
is not reported as restored"), a
`modified` file in a WORM corpus, a file showing **possible corruption** (a `corrupted` event, in
any mode), or the files of a **mass change** in a churn corpus (see the `integrity-scanning`
requirements of those names). The alert's summary SHALL name each of these kinds it contains with
its own count, and SHALL say when a count of modified files is a mass change. Informational
`added`, `restored`, and `moved` events, ordinary churn re-baselines, and a file that returned to
the contents on record before it changed SHALL NOT trigger an alert.

A scan that is refused because its root is unreachable or empty SHALL dispatch one alert saying so
and naming the collection, with no file paths. It SHALL be sent when the refusal begins and SHALL
NOT be repeated by each further refused scan of the same outage. A file that the scan reconciles as a move (see the
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

#### Scenario: Possible corruption alerts in a churn corpus

- **WHEN** a deep scan of a churn corpus with an enabled channel finds a file whose bytes changed
  while its size and modification time did not
- **THEN** an alert SHALL be dispatched whose summary names possible corruption with its count

#### Scenario: A mass change alerts

- **WHEN** a scan of a churn corpus converts a mass change into issues
- **THEN** an alert SHALL be dispatched whose summary gives the number of modified files and says
  it is unusually many for one scan

#### Scenario: A return does not alert

- **WHEN** a scan finds that a changed file is back as it was before it changed
- **THEN** no alert SHALL be dispatched on its account

#### Scenario: An unreachable root alerts once

- **WHEN** a scan is refused because the root is unreachable, and the next scheduled scan is
  refused for the same reason
- **THEN** exactly one alert SHALL have been dispatched, saying the folder is unreachable and
  listing no files
