## ADDED Requirements

### Requirement: Path rules decide which ordinary changes are flagged

A scan SHALL consult the collection's rules when it classifies a changed file. An **ordinary**
content change to a file that a rule matches SHALL be re-recorded and queued for stamping exactly
as a change in a `churn` collection is, and SHALL NOT be flagged, whatever the collection's mode.
The rules in force for a pass SHALL be those the collection held when the pass began.

A rule changes what is flagged and never what is watched: a matching file SHALL still be tracked,
compared on every pass, re-hashed by a deep pass, and stamped where the collection is notarized.

**A rule covers ordinary changes and nothing else.** Regardless of any rule, a scan SHALL flag, as
it would without the rule: a file that is no longer on disk; possible corruption; a file that
reappears with different bytes; and a mass change (the ceiling counts rule-covered changes). A
file that carries an open change alert SHALL stay recorded `modified` through a rule-covered
change, and a file found back at the contents kept from before an unaccepted change SHALL close
its issue, exactly as without the rule. A rule SHALL NOT cause any record to be removed, any alert
to be acknowledged, or any open issue to be resolved — neither when a scan applies it nor when it
is added to or removed from the collection.

#### Scenario: A rule-covered change in a worm collection is not flagged

- **WHEN** a `worm` collection has a rule matching a file, and that file's bytes and modification
  time change
- **THEN** the file SHALL stay `ok` with its new digest, no unacknowledged event SHALL be written
  for it, no alert SHALL be raised, and in a notarized collection it SHALL be queued for a stamp

#### Scenario: A file the rule does not match is still flagged

- **WHEN** another file of the same collection changes
- **THEN** it SHALL be recorded `modified` with an unacknowledged `modified` event

#### Scenario: A rule-covered file is still watched

- **WHEN** a deep pass runs over a collection with a rule
- **THEN** the files the rule matches SHALL be re-hashed like every other tracked file

#### Scenario: A deletion is flagged in spite of a rule

- **WHEN** a file matched by a rule is no longer on disk
- **THEN** it SHALL be recorded `missing` with an unacknowledged `missing` event

#### Scenario: Corruption is flagged in spite of a rule

- **WHEN** a file matched by a rule changes while its size and modification time do not
- **THEN** it SHALL be recorded `modified` with an unacknowledged `corrupted` event

#### Scenario: A changed reappearance is flagged in spite of a rule

- **WHEN** a file matched by a rule goes missing and reappears with bytes that match neither the
  recorded digest nor any kept one
- **THEN** it SHALL be recorded `modified` with an unacknowledged `restored_changed` event

#### Scenario: An open change alert survives a rule-covered edit

- **WHEN** a file matched by a rule carries an open `modified` or `corrupted` event and is then
  edited again
- **THEN** it SHALL stay `modified`, and its alert SHALL stay open

#### Scenario: Adding a rule resolves nothing

- **WHEN** a rule is added that matches a file already recorded `modified`
- **THEN** that file SHALL still be `modified` with its alert open, until it is accepted

#### Scenario: Removing a rule changes no record

- **WHEN** a rule is removed
- **THEN** no file record and no event SHALL change, and a later change to a file it matched SHALL
  be flagged according to the collection's mode

#### Scenario: A rule added during a scan applies from the next one

- **WHEN** a rule is added while a scan of the collection is running
- **THEN** that scan SHALL classify by the rules it started with

## MODIFIED Requirements

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode an ordinary content modification SHALL silently re-baseline the stored hash/size/mtime
with no nag event. A path rule (see "Path rules decide which ordinary changes are flagged") makes an ordinary
modification quiet, in the same way, for the files it matches under `worm`. Two findings are NOT ordinary modifications and SHALL NOT be absorbed by `churn`
mode: a change that carries the corruption signature (see "Possible corruption is its own
finding") and a change that is part of an unusually large change in one scan (see "A mass change
is not absorbed"). A `missing` file SHALL raise an unacknowledged event in BOTH modes, and so SHALL a file
that reappears with bytes that do not match the digest recorded for it (`restored_changed`): a file
that was absent and came back different is not the ordinary editing that `churn` mode exempts, and
suppressing it there would be a silent false negative in exactly the mode where the operator has
told the system to expect edits. The informational
kinds `added` and `restored` SHALL NOT nag in either mode — they are written already acknowledged.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content, its size or modification time having
  changed with it, and the scan's silent changes stay below the mass-change ceiling
- **THEN** the scan SHALL update its stored hash and leave status `ok` with no unacknowledged
  `modified` event

#### Scenario: Missing always nags

- **WHEN** a file goes missing in a `churn` corpus
- **THEN** the scan SHALL still write an unacknowledged `missing` event

#### Scenario: A changed reappearance always nags

- **WHEN** a file recorded `missing` in a `churn` corpus reappears with bytes that do not match the
  digest recorded for it
- **THEN** the scan SHALL write an unacknowledged `restored_changed` event and SHALL NOT silently
  re-baseline the file

#### Scenario: Informational events do not nag

- **WHEN** a scan in either mode writes an `added` or `restored` event
- **THEN** the event SHALL be acknowledged at creation and SHALL NOT increase the corpus's count
  of unacknowledged events

### Requirement: A mass change is not absorbed

A scan SHALL NOT silently accept an unusually large change. Where the content changes a pass
would re-baseline silently — under the `churn` policy, or because a path rule matches them, in a
collection of either policy — number at least a configured minimum **and** at
least a configured share of the files the collection tracks, the scan SHALL instead record every
one of them `modified`, keep the contents each replaced, write an unacknowledged `modified` event
for each that states how many files changed in that pass, and raise an alarm. Below either
threshold the pass SHALL behave as before. The policy, or a rule, tells the system to
expect edits; it does not tell it to expect the whole collection to be rewritten overnight, and
an exemption with no ceiling absorbs a disaster as readily as a typo.

The rule is evaluated once per pass, on the changes that pass itself re-baselined. It does not
accumulate across passes: a pass that is interrupted has absorbed what it had already committed,
and the pass that follows counts only what it finds changed. This limit is part of the
requirement, not an oversight — it is what the rule guarantees.

The share SHALL be computed over the records the collection tracked when the pass began, missing
ones excluded. Setting the share to zero SHALL disable the rule. The rule SHALL NOT apply to
changes that are reported anyway — in a `worm` collection, every change no path rule matches — nor
to deletions, which are already reported file by file.

#### Scenario: A burst over both thresholds becomes issues

- **WHEN** a pass over a `churn` collection finds changed files numbering at least the minimum and
  at least the configured share of the collection
- **THEN** every one of those files SHALL be `modified` with an unacknowledged `modified` event,
  and the scan SHALL count them as a mass change

#### Scenario: A burst under the share is absorbed

- **WHEN** the changed files reach the minimum count but not the configured share
- **THEN** they SHALL be re-baselined silently, as ordinary edits

#### Scenario: A burst under the minimum is absorbed

- **WHEN** most of a small collection changes but the number of changed files is below the minimum
- **THEN** they SHALL be re-baselined silently

#### Scenario: The rule can be disabled

- **WHEN** the configured share is zero
- **THEN** no number of changes in one pass SHALL be converted

#### Scenario: Converted files keep what they replaced

- **WHEN** the mass-change rule converts a file
- **THEN** its record SHALL hold the digest it had before the pass, so putting it back closes the
  issue

#### Scenario: A mass of rule-covered changes in a worm collection becomes issues

- **WHEN** a pass over a `worm` collection finds changes that a path rule makes quiet, numbering at
  least the minimum and at least the configured share of the collection
- **THEN** every one of those files SHALL be recorded `modified` with an unacknowledged `modified`
  event, and the scan SHALL count them as a mass change

