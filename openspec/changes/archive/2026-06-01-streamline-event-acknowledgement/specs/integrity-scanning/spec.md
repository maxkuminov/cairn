## MODIFIED Requirements

### Requirement: Scan classifies files with fast-path hashing

A scan SHALL walk a corpus root (honoring its exclude globs), diff the filesystem against the
`files` table by relative path, and classify each file as `added`, `modified`, `missing`, `ok`,
or `restored`. To avoid re-hashing unchanged data at scale, the scan SHALL compare size and mtime
first and SHALL compute the SHA-256 only when size/mtime differ or no prior hash exists. SHA-256
SHALL be computed by streaming the file in chunks (never loading it wholly into memory).

Events for the informational kinds `added` and `restored` SHALL be written already acknowledged
(`acknowledged_at` set to the detection time, `acknowledged_by` NULL to denote a system
acknowledgement) — they are recorded for the activity feed and audit trail but SHALL NOT count as
unacknowledged "needs action" events. Only `missing` (both modes) and worm `modified` events SHALL
be written unacknowledged.

#### Scenario: New file is added

- **WHEN** a scan finds a file under the root with no matching `files` row
- **THEN** a `files` row SHALL be created with status `new`, its size/mtime/sha256 recorded, and
  an `added` event SHALL be written already acknowledged (`acknowledged_at` set, `acknowledged_by`
  NULL) so it does not count as a needs-action event

#### Scenario: Unchanged file is not re-hashed

- **WHEN** a tracked file's size and mtime equal the stored values
- **THEN** the scan SHALL mark it `ok` and update `last_checked` without recomputing its SHA-256

#### Scenario: Modified content is detected

- **WHEN** a tracked file's bytes change so its SHA-256 differs from the stored hash
- **THEN** the scan SHALL record the new hash and (in worm mode) set status `modified` and write
  a `modified` event

#### Scenario: mtime moved but content identical

- **WHEN** a tracked file's mtime changes but its recomputed SHA-256 matches the stored hash
- **THEN** the scan SHALL keep status `ok`, refresh the stored mtime, and SHALL NOT write a
  `modified` event

#### Scenario: Missing file is detected

- **WHEN** a tracked file is absent from the filesystem during a scan
- **THEN** the scan SHALL set its status `missing` and write an unacknowledged `missing` event

#### Scenario: Restored file

- **WHEN** a file previously recorded `missing` reappears during a scan
- **THEN** the scan SHALL set its status back to `ok` and write a `restored` event already
  acknowledged (`acknowledged_at` set, `acknowledged_by` NULL)

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode a content modification SHALL silently re-baseline the stored hash/size/mtime with no
nag event. A `missing` file SHALL raise an unacknowledged event in BOTH modes. The informational
kinds `added` and `restored` SHALL NOT nag in either mode — they are written already acknowledged.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content
- **THEN** the scan SHALL update its stored hash and leave status `ok` with no unacknowledged
  `modified` event

#### Scenario: Missing always nags

- **WHEN** a file goes missing in a `churn` corpus
- **THEN** the scan SHALL still write an unacknowledged `missing` event

#### Scenario: Informational events do not nag

- **WHEN** a scan in either mode writes an `added` or `restored` event
- **THEN** the event SHALL be acknowledged at creation and SHALL NOT increase the corpus's count
  of unacknowledged events
