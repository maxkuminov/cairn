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
the surviving record healthy). The scan SHALL therefore re-establish the rule once, after every
other classification step of the pass — the missing sweep, move reconciliation and automatic
graduation of new files — and in the same transaction as the pass's final batch, so that no step
of the same scan can undo it and no reader can observe the file as healthy in between.

Re-flagging a file SHALL NOT write an event and SHALL NOT raise an alert: the alert was sent when
the change was found. It SHALL NOT alter the file's recorded digest, size, proof state or
first-seen time. The number of files re-flagged SHALL be recorded in the scan's summary and logged.
The rule applies in both change policies. It SHALL also correct records left in that state by
earlier versions, on the first scan that sees them.

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
