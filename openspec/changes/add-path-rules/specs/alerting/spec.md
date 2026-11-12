## MODIFIED Requirements

### Requirement: Alert on newly-detected alarming changes, per-corpus routing

The system SHALL dispatch a single batched alert for a corpus to the channels enabled in its
`alert_json` when a scan newly detects an alarming change: a `missing` file (in any mode), a file
that reappeared with **different bytes** than the digest recorded for it (a `restored_changed` file,
in any mode — see the `integrity-scanning` requirement "A file that reappears with different bytes
is not reported as restored"), a
`modified` file in a WORM corpus, a file showing **possible corruption** (a `corrupted` event, in
any mode), or the files of a **mass change** — in a corpus of either mode (see the `integrity-scanning`
requirements of those names). The alert's summary SHALL name each of these kinds it contains with
its own count, and SHALL say when a count of modified files is a mass change. Informational
`added`, `restored`, and `moved` events, ordinary churn re-baselines, an ordinary change that a
path rule makes quiet, and a file that returned to the contents on record before it changed SHALL
NOT trigger an alert.

A scan that is refused because its root is unreachable or empty SHALL dispatch an alert saying so
and naming the collection, with no file paths. While the outage lasts the alert SHALL be repeated
once in twenty-four hours per collection: a refused scan SHALL dispatch it unless such an alert
was **last attempted** for that collection — for either reason, unreachable or empty — within the
preceding twenty-four hours. The limit is keyed on the last attempt, never on the existence of
earlier refused scans: with scans more frequent than a day an earlier refusal always exists, and a
limit keyed on it would send one alert per outage. Dispatch is best-effort, so an alert that failed
to deliver is followed by another within a day, and an unrelated earlier scan error never
suppresses it.

A scan that was incomplete — it could not list a directory beneath which tracked files lie —
SHALL dispatch an alert under the same once-in-twenty-four-hours limit, naming the collection and
how many folders could not be read and saying that the files beneath them are not being checked. A
scan whose only unreadable directories hold nothing tracked SHALL NOT alert; it is reported on the
collection as a partial scan. Such a scan completes as `partial`, which
the health check counts as a scan having run; without an alert a folder that stays unreadable
would go unchecked indefinitely behind a collection that looks current. A file that the scan reconciles as a move (see the
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

- **WHEN** a scan converts a mass change into issues — the silent re-baselines of a churn corpus,
  or the changes a path rule would have made quiet in a worm corpus
- **THEN** an alert SHALL be dispatched whose summary gives the number of modified files and says
  it is unusually many for one scan

#### Scenario: A return does not alert

- **WHEN** a scan finds that a changed file is back as it was before it changed
- **THEN** no alert SHALL be dispatched on its account

#### Scenario: An unreachable root alerts, and not on every scan

- **WHEN** a scan is refused because the root is unreachable, and a further scan of the same
  collection is refused for the same reason an hour later
- **THEN** exactly one alert SHALL have been dispatched, saying the folder is unreachable and
  listing no files

#### Scenario: A continuous outage is re-announced once a day

- **WHEN** a collection is scanned every hour and every scan is refused for an unreachable root,
  for twenty-six hours
- **THEN** exactly two alerts SHALL have been dispatched

#### Scenario: A lasting outage is repeated daily

- **WHEN** a scan is refused for an unreachable root and the only earlier refusals of that
  collection are more than twenty-four hours old
- **THEN** an alert SHALL be dispatched

#### Scenario: An unreadable folder alerts

- **WHEN** a scan cannot list a directory under a collection's root
- **THEN** an alert SHALL be dispatched saying that folders could not be read and their files are
  not being checked, and a further such scan within twenty-four hours SHALL NOT repeat it

#### Scenario: An unrelated earlier failure does not suppress the alert

- **WHEN** a collection's previous scan ended in error for some other reason, and its next scan is
  refused for an unreachable root
- **THEN** the unreachable-folder alert SHALL be dispatched

#### Scenario: A rule-covered change does not alert

- **WHEN** a scan re-records a change that a path rule makes quiet
- **THEN** no alert SHALL be dispatched on its account

#### Scenario: A rule does not silence what it never covers

- **WHEN** a file matched by a rule goes missing, or shows possible corruption
- **THEN** an alert SHALL be dispatched as it would be without the rule
