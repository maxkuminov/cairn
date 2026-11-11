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
