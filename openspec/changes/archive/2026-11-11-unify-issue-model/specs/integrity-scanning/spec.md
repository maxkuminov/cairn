## ADDED Requirements

### Requirement: Accept can act on an explicit set of file records

The accept service SHALL offer a form that acts on an **explicit set of file records** of one
collection, named by identifier, for callers that have already established exactly which records an
operator authorised. It SHALL apply to each record what the scoped accept applies to a record in
that state: a `modified` (or `new`) record is set `ok`; a `missing` record has its events detached
— each keeping the path it was about — and is then removed. It SHALL close only the open events
of the records it acts on, recording who and when, and SHALL leave every other record and every
other open event in the collection untouched.

The set is a statement about records the caller has just read, so the service SHALL re-assert it
rather than trust it: a record that does not belong to the named collection SHALL NOT be acted on,
and a record that is no longer in an acceptable state SHALL be left as it is. The whole set SHALL
be applied in one transaction, and its size SHALL NOT be limited by the datastore's bound-parameter
ceiling.

#### Scenario: Only the named records are accepted

- **WHEN** the explicit-set accept is run naming two of a collection's four missing files
- **THEN** those two records SHALL be removed with their events detached and closed, and the other
  two records and their open alerts SHALL be unchanged

#### Scenario: Changed and missing records in one set

- **WHEN** the set names one modified file and one missing file
- **THEN** the modified file SHALL become `ok` with its open events closed, and the missing file's
  record SHALL be removed with its events detached, each keeping the path it was about

#### Scenario: A record of another collection is not touched

- **WHEN** the set includes the identifier of a record that belongs to a different collection
- **THEN** that record SHALL NOT be changed and none of its events SHALL be closed

#### Scenario: A record that is already healthy is left alone

- **WHEN** the set includes a record whose status is `ok`
- **THEN** that record SHALL be unchanged

#### Scenario: A very large set is applied whole

- **WHEN** the set names more records than the datastore accepts as bound parameters in one
  statement
- **THEN** every named record SHALL be accepted, in one transaction

### Requirement: An unresolved change stays an issue

A scan SHALL leave every file that carries an open (unacknowledged) `modified` or
`restored_changed` event recorded as `modified`, unless the file is recorded `missing`. A change
that was found and never accepted is unresolved whatever happens to the file afterwards, and the
file's status is the only thing the panel counts: a file whose status has returned to `ok` while
its change alert is still open would read as healthy.

Two existing paths return such a file to `ok`: a changed file that goes missing and reappears with
the bytes on record (the reappearance check compares against the changed contents, and closes only
the `missing` events), and a changed file that is then moved or renamed (move reconciliation sets
the surviving record healthy). A scan commits its work in batches, so the rule SHALL hold in
**every batch a scan commits**, not only at its end: wherever a pass would record an existing file
healthy — a reappearance that matches, a move, an expected edit under the `churn` policy, the
automatic graduation of a new file — it SHALL record `modified` instead when that file carries an
open change alert, so that no committed state of the scan shows the file as healthy and an aborted
scan leaves nothing healthy behind it. The scan SHALL additionally re-establish the rule over the
whole collection once, after every other classification step of the pass and in the same
transaction as its final batch, so that a path nobody thought of cannot undo it and records left
by earlier versions are corrected.

Re-flagging a file SHALL NOT write an event and SHALL NOT raise an alert: the alert was sent when
the change was found. It SHALL NOT alter the file's recorded digest, size, proof state or
first-seen time. The number of files re-flagged SHALL be recorded in the scan's summary and logged.
The rule applies in both change policies. It SHALL also correct records left in that state by
earlier versions, on the first scan that sees them.

The rule is about open alerts, so it SHALL follow them when they close. The set of files carrying
an open change alert SHALL be read again at the end of the pass, in the same transaction as its
final batch: a file the pass kept `modified` only because of an alert that has since been
acknowledged — an accept run from the command line is not excluded by a running scan — SHALL be
recorded `ok`, so that no file is left listed as changed with no open alert behind it and no action
that can clear it. A file a classification step of the pass itself found changed SHALL NOT be
returned to `ok` by this re-read.

The same rule SHALL bind every other writer of a file's status. In particular a manifest import,
which re-seeds recorded digests and accepts nothing, SHALL leave a file that carries an open change
alert recorded `modified`.

#### Scenario: A changed file that is deleted and restored is still listed as changed

- **WHEN** a file in a `worm` collection is found changed and not accepted, then found missing,
  then found again with the bytes recorded when it changed
- **THEN** after that scan the file's status SHALL be `modified`, its `missing` events SHALL be
  acknowledged, and its `modified` event SHALL still be open

#### Scenario: A changed file that is renamed is still listed as changed

- **WHEN** a file in a `worm` collection is found changed and not accepted, and is then renamed so
  that the next scan reconciles it as a move
- **THEN** after that scan the surviving record SHALL have status `modified`, and no additional
  alert SHALL have been raised for it

#### Scenario: No committed batch shows the file healthy

- **WHEN** a changed, unaccepted file that went missing reappears with the bytes on record during
  a scan that commits more than one batch, and the datastore is read after the batch containing
  that file is committed and before the scan finishes
- **THEN** the file's status SHALL already be `modified`

#### Scenario: An expected edit does not launder an earlier alarm

- **WHEN** a file in a `churn` collection carries an open `restored_changed` event and is then
  edited, so that the next scan re-baselines the edit
- **THEN** the file's status SHALL remain `modified`

#### Scenario: Re-flagging raises no alert and writes no event

- **WHEN** a scan re-flags a file under this rule
- **THEN** it SHALL write no new event for that file and SHALL dispatch no alert on its account

#### Scenario: An accepted change is not re-flagged

- **WHEN** a changed file is accepted, so that its events are acknowledged, and a later scan finds
  it unchanged
- **THEN** the file SHALL stay `ok`

#### Scenario: Records left by earlier versions are corrected

- **WHEN** a collection holds a file whose status is `ok` while one of its `modified` events is
  still open, and a scan runs
- **THEN** the file's status SHALL be `modified` after that scan

#### Scenario: A missing file is left as missing

- **WHEN** a file is recorded `missing` and also carries an open `modified` event
- **THEN** the scan SHALL leave its status `missing`

#### Scenario: An alert acknowledged during the pass does not leave the file flagged

- **WHEN** a pass keeps a file `modified` because it carried an open change alert when the pass
  began, and that alert is acknowledged before the pass ends
- **THEN** the file's status SHALL be `ok` after the scan, and the scan SHALL not count it as
  re-flagged

#### Scenario: A manifest import does not mark an unaccepted change healthy

- **WHEN** a manifest is imported over a collection in which a file has been found changed and not
  accepted
- **THEN** that file's status SHALL still be `modified` after the import, its change alert SHALL
  still be open, and the collection's other imported files SHALL be `ok`

### Requirement: An accept never runs under an operation in flight

The command-line accept SHALL refuse to act on a collection while a scan, stamp or upgrade
operation is running on it, exactly as the panel's accept actions do, and SHALL say so. A scan
holds the records it read when it began; an accept that lands underneath it is written over, and a
change the scan had just found could be recorded healthy. The refusal SHALL be decided under the
datastore's write lock, so that an operation cannot claim the collection between the check and the
accept. Accepting several collections SHALL continue past a refused one, and the command SHALL exit
non-zero only when every collection it was asked to accept was refused.

A scan SHALL nevertheless not depend on that exclusion for its own correctness: at the end of a
pass it SHALL establish the rule "a file with an open change alert is recorded `modified`" against
the stored records, not only against the copies it holds.

#### Scenario: The command-line accept is refused during a scan

- **WHEN** `cairn accept` is run for a collection that has an operation in progress
- **THEN** it SHALL change no file record and acknowledge no event, SHALL report that the
  collection is busy, and SHALL exit non-zero

#### Scenario: A change found while an accept lands elsewhere is not committed healthy

- **WHEN** a file already recorded `modified` changes again, and during the scan that finds the new
  change another connection acknowledges its earlier alert and records it `ok`
- **THEN** after the scan the stored record SHALL be `modified` and the new change's alert SHALL be
  open
