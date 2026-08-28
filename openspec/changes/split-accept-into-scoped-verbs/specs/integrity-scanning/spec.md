# integrity-scanning Specification (delta)

## MODIFIED Requirements

### Requirement: Accept re-baselines and acknowledges

Accepting a collection SHALL be **scoped**: the caller SHALL be able to name which of the three
populations it acts on — files recorded `new`, files recorded `modified`, files recorded `missing` —
and the operation SHALL act on those populations only. Accepting with **no scope named** SHALL keep
the whole-collection behaviour unchanged: `new` and `modified` files set to `ok`, the rows for
`missing` files removed (accepted as gone), and every unacknowledged event for that collection
marked acknowledged, recording who and when. Accepting again with nothing pending SHALL be a no-op.

One verb acting on three populations cannot be labelled honestly: the caller that means "fold in the
files I have not vouched for yet" also rewrites the expected version of every changed file and
removes the record of every missing one. Scoping is what lets each control state its own
consequence.

**A scoped accept SHALL acknowledge only the events of the files its scope actually touched.** An
accept that still marked every open event on the collection acknowledged would clear alerts its
scope never mentions — a scope of `new`, which deletes nothing and rewrites no baseline, would
silently close a missing-file alarm. That is the same false negative the scoping exists to remove,
so the acknowledgement SHALL be narrowed with the verb rather than left collection-wide. The
unscoped form keeps the collection-wide acknowledgement, including events that are no longer
attached to any file.

Removing the rows for `missing` files SHALL continue to **detach that file's events before the rows
are deleted**, so the audit trail survives the cascade.

#### Scenario: Accept clears pending changes

- **WHEN** a collection has modified, new, and missing files and an unscoped `accept` is run
- **THEN** modified/new files SHALL become `ok`, missing rows SHALL be deleted, and all
  unacknowledged events SHALL be marked acknowledged

#### Scenario: Accept is idempotent

- **WHEN** `accept` is run on a collection with no pending changes or unacknowledged events
- **THEN** it SHALL make no changes

#### Scenario: A scoped accept touches only its own population

- **WHEN** an accept scoped to `new` is run on a collection that also has modified and missing files
- **THEN** the `new` files SHALL become `ok`, and no modified file's baseline SHALL be rewritten and
  no missing file's record SHALL be removed

#### Scenario: A scoped accept does not clear an alert outside its scope

- **WHEN** an accept scoped to `new` is run on a collection with an open `missing` alert
- **THEN** that alert SHALL remain unacknowledged

#### Scenario: Adopting changed files acknowledges only those files' alerts

- **WHEN** an accept scoped to `modified` is run on a collection that also has a missing file with
  an open alert
- **THEN** the adopted files' open events SHALL be acknowledged and the missing file's open alert
  SHALL remain unacknowledged

#### Scenario: Stopping tracking acknowledges only the removed files' alerts

- **WHEN** an accept scoped to `missing` is run on a collection that also has a modified file with
  an open alert
- **THEN** the removed files' events SHALL be detached, acknowledged, and their rows deleted, and
  the modified file's open alert SHALL remain unacknowledged

#### Scenario: An unrecognized scope is refused rather than widened

- **WHEN** an accept is requested with a scope naming anything other than the three file states
- **THEN** the operation SHALL fail rather than act on the whole collection

#### Scenario: More missing files than the datastore's bound-parameter limit are removed

- **WHEN** an accept scoped to `missing` runs on a collection with more missing files than one
  statement's bound-parameter limit allows to be named individually
- **THEN** every one of them SHALL be detached, acknowledged and removed

## ADDED Requirements

### Requirement: A single file can be accepted on its own

The system SHALL provide an accept operation that acts on **one file record**, so that a scan
reporting one legitimate edit and one suspicious deletion can be resolved one file at a time.
Without it the only control adopts both, which forces the operator to choose between hoarding
unactioned alerts and waving a genuine incident through — and a control that trains the operator to
clear alarms in bulk is a false negative with a human in the loop.

Accepting one file SHALL apply to that file exactly what the corresponding scoped collection accept
applies: a `new` or `modified` record SHALL be set `ok`, and a `missing` record SHALL have its
events **detached before the row is deleted**, in the same order and by the same means, so a single
file's history survives the cascade exactly as a bulk removal's does.

It SHALL acknowledge **only that file's** open events, and SHALL leave every other file's alerts,
baselines and records untouched. It SHALL refuse where the named file does not belong to the named
collection.

#### Scenario: One file is accepted without touching the others

- **WHEN** a collection has one modified file and one missing file and the modified file alone is
  accepted
- **THEN** that file SHALL become `ok` and the missing file's record and open alert SHALL be
  unchanged

#### Scenario: Accepting one missing file preserves its history

- **WHEN** a single `missing` file is accepted
- **THEN** its events SHALL be detached before its row is deleted, so they survive the deletion, and
  they SHALL be acknowledged

#### Scenario: A file from another collection is refused

- **WHEN** a single-file accept names a file that does not belong to the named collection
- **THEN** the operation SHALL refuse and SHALL mutate nothing

### Requirement: A removed file's events keep the path they refer to

Where an accept removes a file record, the events detached from it SHALL carry that file's path
forward in their own detail, recorded **at the moment of detachment**, so the surviving audit trail
still says which file it is about. Detaching the events preserves the history from the cascade, but a
`missing` event carries no path of its own: once its file record is gone the panel renders it with
no path at all, and the one fact those rows exist to record — *which* file the operator stopped
tracking — is the fact that is lost.

Each event SHALL receive **its own file's** path, not one value applied across the batch.

An event whose detail already carries something SHALL be left alone: a `moved` event's
`old → new` path pair and a `restored_changed` event's recorded/observed digest pair are the
findings those kinds exist to carry, and overwriting them with a path would destroy a record to fix
a rendering.

#### Scenario: A stopped-tracking file's event keeps its path

- **WHEN** an accept removes a `missing` file whose event carries no detail
- **THEN** that event SHALL be left with the removed file's path recorded in its detail

#### Scenario: Each event gets its own file's path

- **WHEN** an accept removes several missing files in one operation
- **THEN** each detached event SHALL carry the path of the file it belonged to, not a value shared
  across them

#### Scenario: An existing detail is never overwritten

- **WHEN** an accept removes a file whose events include a `moved` event recording its old and new
  paths, or a `restored_changed` event recording two digests
- **THEN** those details SHALL be unchanged

#### Scenario: An already-acknowledged event is not re-stamped

- **WHEN** an accept detaches an event that was already acknowledged
- **THEN** its recorded acknowledgement time and acknowledging user SHALL be unchanged
