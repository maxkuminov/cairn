## ADDED Requirements

### Requirement: Path rules decide which ordinary changes are flagged

A scan SHALL consult the collection's rules when it classifies a file. An **ordinary** content
change to a file that a rule matches SHALL be re-recorded and queued for stamping exactly as a
change in a `churn` collection is, and SHALL NOT be flagged, whatever the collection's mode. A
tracked file that the walk did not find, that is not reconciled as a move, and that a **comes and
goes** rule matches SHALL be retired quietly: its events SHALL be detached, each keeping the path
it was about, and acknowledged, and its record SHALL be removed — the consequence of accepting its
deletion, with no alarm.

A rule changes what is flagged and never what is watched: a matching file SHALL still be tracked,
compared, re-hashed by a deep pass, and stamped where the collection is notarized.

**A rule never covers a finding that is not routine.** Regardless of any rule, the scan SHALL
flag: possible corruption; a file that reappears with different bytes; and a file that carries an
open change alert, which SHALL stay recorded `modified`. A refused pass, and a file beneath a
folder that could not be listed, are unaffected by rules.

**A rule has a ceiling in both directions.** The mass-change rule SHALL count rule-covered quiet
changes together with `churn` re-baselines, and SHALL therefore apply to rule-covered changes in a
`worm` collection as well. Quiet removals SHALL have the same ceiling, with the same two settings:
where the files a pass would retire quietly number at least the configured minimum and at least
the configured share of the files the collection tracks, the pass SHALL retire none of them and
SHALL record every one `missing` and alarm, as it would without the rule. A rule says that some
files change or come and go; it does not say that most of a collection may vanish overnight.

#### Scenario: A rule-covered change in a worm collection is not flagged

- **WHEN** a `worm` collection has a changes-expected rule matching a file, and that file's bytes
  and modification time change
- **THEN** the file SHALL stay `ok` with its new digest, no unacknowledged event SHALL be written
  for it, and in a notarized collection it SHALL be queued for a stamp

#### Scenario: A file the rule does not match is still flagged

- **WHEN** the same collection's other file changes
- **THEN** it SHALL be recorded `modified` with an unacknowledged `modified` event

#### Scenario: A rule-covered file is still watched

- **WHEN** a deep pass runs over a collection with a rule
- **THEN** the files the rule matches SHALL be re-hashed like every other tracked file

#### Scenario: Corruption is flagged in spite of a rule

- **WHEN** a file matched by a rule changes while its size and modification time do not
- **THEN** it SHALL be recorded `modified` with an unacknowledged `corrupted` event

#### Scenario: A changed reappearance is flagged in spite of a rule

- **WHEN** a file matched by a changes-expected rule goes missing and reappears with bytes that
  match neither the recorded digest nor any kept one
- **THEN** it SHALL be recorded `modified` with an unacknowledged `restored_changed` event

#### Scenario: An open change alert survives a rule-covered edit

- **WHEN** a file matched by a rule carries an open `corrupted` event and is then edited
- **THEN** it SHALL stay `modified`

#### Scenario: A comes-and-goes file is retired quietly

- **WHEN** a file matched by a comes-and-goes rule is no longer on disk
- **THEN** its record SHALL be removed, its events SHALL be detached with the path kept and
  acknowledged, no unacknowledged `missing` event SHALL exist for it, and no alert SHALL be
  dispatched

#### Scenario: A changes-expected rule does not cover a deletion

- **WHEN** a file matched only by a changes-expected rule is no longer on disk
- **THEN** it SHALL be recorded `missing` with an unacknowledged `missing` event

#### Scenario: A renamed comes-and-goes file is a move, not a removal

- **WHEN** a file matched by a comes-and-goes rule is renamed and the scan reconciles the move
- **THEN** the record SHALL be kept under its new path, as for any move

#### Scenario: A mass of rule-covered changes becomes issues

- **WHEN** a pass over a `worm` collection finds rule-covered changes numbering at least the
  minimum and at least the configured share of the collection
- **THEN** every one of those files SHALL be recorded `modified` with an unacknowledged event

#### Scenario: A mass of quiet removals is not absorbed

- **WHEN** the files a pass would retire under comes-and-goes rules number at least the minimum
  and at least the configured share of the collection
- **THEN** none SHALL be retired, and every one SHALL be recorded `missing` with an unacknowledged
  `missing` event

#### Scenario: A few quiet removals in a large collection are retired

- **WHEN** the quiet removals of a pass are below the minimum, or below the share
- **THEN** they SHALL be retired quietly

### Requirement: A new rule resolves the open issues it covers

When a rule is added to a collection, the open issues it covers SHALL be resolved in the same
operation, so that saying "this is normal" does not leave the operator accepting by hand the very
files they just described. A file recorded `modified` whose path the rule matches SHALL be
accepted — recorded `ok`, its alerts acknowledged, the contents kept from before the change
discarded — **only where every open alert on it is an ordinary modification**: a file that carries
a possible-corruption alert, a came-back-different alert, or an alert written by the mass-change
rule SHALL be left as it is. Under a comes-and-goes rule, a file recorded `missing` whose path the
rule matches SHALL be retired as an accepted deletion is. Nothing the rule does not match SHALL be
touched, and removing a rule SHALL change no record.

The operation SHALL be refused while a scan, stamp or upgrade is running on the collection, and
SHALL be applied in one transaction with the storing of the rule.

#### Scenario: Open changes the rule covers are accepted

- **WHEN** a changes-expected rule is added that matches two of a collection's three `modified`
  files, each carrying only ordinary modification alerts
- **THEN** those two SHALL be `ok` with their alerts acknowledged, and the third SHALL be unchanged

#### Scenario: A corrupted file is not resolved by a rule

- **WHEN** a rule is added that matches a `modified` file carrying an open `corrupted` event
- **THEN** that file SHALL stay `modified` and its alert SHALL stay open

#### Scenario: A mass-change issue is not resolved by a rule

- **WHEN** a rule is added that matches a file made an issue by the mass-change rule
- **THEN** that file SHALL stay `modified`

#### Scenario: A changes-expected rule leaves missing files alone

- **WHEN** a changes-expected rule is added that matches a `missing` file
- **THEN** that file SHALL stay `missing`

#### Scenario: A comes-and-goes rule retires the missing files it covers

- **WHEN** a comes-and-goes rule is added that matches three of a collection's `missing` files
- **THEN** those three records SHALL be removed with their events detached, path kept and
  acknowledged, and the other missing files SHALL be unchanged

#### Scenario: Removing a rule changes no record

- **WHEN** a rule is removed
- **THEN** no file record and no event SHALL change, and a later change to a file it matched SHALL
  be flagged according to the collection's mode

#### Scenario: A rule is not added under a running operation

- **WHEN** a rule is submitted while a scan is running on the collection
- **THEN** it SHALL be refused, no rule SHALL be stored and no record SHALL change

## MODIFIED Requirements

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode an ordinary content modification SHALL silently re-baseline the stored hash/size/mtime
with no nag event. A path rule (see "Path rules decide which ordinary changes are flagged") can make an
ordinary modification quiet for the files it matches under `worm` as well. Two findings are NOT ordinary modifications and SHALL NOT be absorbed by `churn`
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
