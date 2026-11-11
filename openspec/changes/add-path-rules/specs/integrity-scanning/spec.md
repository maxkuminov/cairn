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

### Requirement: The contents on record before an unaccepted change are kept

When a scan finds that a healthy file has changed and records it `modified`, it SHALL keep the
digest, size and modification time that were on record before the change, alongside the new ones,
until the change is accepted. Further changes to a file that is already `modified` SHALL NOT
replace what was kept: they are further unaccepted changes away from the same last-healthy state.
Where a missing file reappears with different bytes and nothing is yet kept for it — it was
healthy when it went missing — what is kept SHALL be the digest recorded when it went missing.
Where something is already kept, it SHALL be left as it is: the digest recorded when the file went
missing is then itself an unaccepted change, and adopting it as "the contents before the change"
would let that unaccepted version close the issue later as a return. Every accept of the file SHALL discard what was kept, and a healthy file
SHALL never carry any.

Each `modified` and `corrupted` event SHALL record what the file was and what it became — digest,
size and modification time on both sides — together with the file's path, so that the event still
says what it was about after the file's record has been removed.

**A file that returns to the kept contents closes its own issue.** Whenever a scan holds a digest
for a file that equals the kept one — because a changed file was put back, or because a file that
had changed and then gone missing was restored from a copy of the original — the scan SHALL record
the file `ok` with what it found, discard what was kept, acknowledge every open change alert of
that file as a system acknowledgement in the same committed transaction, and write one
informational event saying the file returned to the contents on record before it changed. It SHALL
raise no alarm. A file for which contents are kept SHALL be re-read by **every** pass, quick or
deep, whether or not its size or modification time moved: a restore that preserves metadata puts
the original bytes back under the very size and time already on record — for a corrupted file they
never differed — and a pass that trusted them would never see that the issue had closed itself.
Equality of digests is the only test: a matching size or name SHALL NOT be treated
as a return. On reappearance the order of tests SHALL be: equals the recorded digest (the file is
back as it was last seen); equals the kept digest (the file is back as it was before it changed);
otherwise it came back different. A record with no digest on record has nothing to be compared
with and nothing kept; its reappearance is governed, unchanged, by the existing requirement "A
file that reappears with different bytes is not reported as restored".

#### Scenario: A change keeps what it replaced

- **WHEN** a healthy file in a `worm` collection changes and is scanned
- **THEN** the file SHALL be `modified`, its record SHALL hold both the new digest and the digest,
  size and modification time it had before, and its `modified` event SHALL record both sides and
  the path

#### Scenario: A second change does not replace what was kept

- **WHEN** a `modified` file changes again before it is accepted
- **THEN** the kept digest SHALL still be the one from before the first change

#### Scenario: Putting the original back closes the issue

- **WHEN** a `modified` file is restored to exactly the bytes it had before it changed, and scanned
- **THEN** the file SHALL be `ok`, nothing SHALL be kept for it any more, its open change alerts
  SHALL be acknowledged with no acknowledging user, an informational event SHALL record the return,
  and no alert SHALL be dispatched

#### Scenario: A restore that preserves the file's size and date closes on a quick scan

- **WHEN** a file found possibly corrupted is replaced by its original bytes with its original
  size and modification time, and a quick (non-deep) scan runs
- **THEN** the file SHALL be `ok` after that scan, and its `corrupted` event SHALL be acknowledged

#### Scenario: A changed file that was deleted and restored from the original closes too

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  bytes it had before it changed
- **THEN** the file SHALL be `ok`, its `modified` and `missing` alerts SHALL be acknowledged, and no
  `restored_changed` event SHALL be written

#### Scenario: Coming back as the changed version is still unresolved

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  changed bytes
- **THEN** the file SHALL be `modified`, and what was kept SHALL still be the digest from before the
  change

#### Scenario: A changed file that vanished and came back as a third version keeps the original

- **WHEN** a file is found changed and not accepted, then found missing, then found again with
  bytes matching neither the changed version nor the original, and is later put back as the
  changed version
- **THEN** the file SHALL stay `modified` throughout, what is kept SHALL still be the digest from
  before the first change, and only a return to that digest SHALL close the issue

#### Scenario: Something else again is still a change

- **WHEN** a `modified` file changes to bytes that match neither the recorded digest nor the kept
  one
- **THEN** it SHALL stay `modified`, what was kept SHALL be unchanged, and a further alert SHALL be
  raised where the collection would flag that change — not where its policy, or a path rule, makes
  an ordinary change quiet, in which case the file simply stays `modified` with its existing alert
  open

#### Scenario: Accepting discards what was kept

- **WHEN** a `modified` file is accepted — on its own, as part of a set, or with its collection
- **THEN** its record SHALL hold no kept contents, and a later return to the old bytes SHALL be
  reported as a change

#### Scenario: Same size is not a return

- **WHEN** a `modified` file changes to different bytes that happen to have the size it had before
  the first change
- **THEN** the file SHALL NOT be recorded `ok`
