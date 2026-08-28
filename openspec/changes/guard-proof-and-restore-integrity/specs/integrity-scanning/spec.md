# integrity-scanning Specification (delta)

## MODIFIED Requirements

### Requirement: Scan classifies files with fast-path hashing

A scan SHALL walk a corpus root (honoring its exclude globs), diff the filesystem against the
`files` table by relative path, and classify each file as `added`, `modified`, `missing`, `ok`,
`restored`, or `restored_changed`. A file whose relative path the datastore cannot store (see "Scan tolerates paths the
datastore cannot store") SHALL be skipped before classification and SHALL NOT receive a `files` row.
To avoid re-hashing unchanged data at scale, the scan SHALL compare size and mtime first and SHALL
compute the SHA-256 only when size/mtime differ or no prior hash exists. SHA-256 SHALL be computed by
streaming the file in chunks (never loading it wholly into memory). Files classified `missing` and
`added` within a single scan SHALL then be subject to move/rename reconciliation (see
"Content-addressed move/rename reconciliation") before alerts are routed and the run is finalized.

#### Scenario: New file is added

- **WHEN** a scan finds a file under the root with no matching `files` row
- **THEN** a `files` row SHALL be created with status `new`, its size/mtime/sha256 recorded, and
  an `added` event SHALL be written

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
- **THEN** the scan SHALL set its status `missing` and write a `missing` event

#### Scenario: Restored file

- **WHEN** a file previously recorded `missing` reappears during a scan with bytes matching the
  digest recorded for it
- **THEN** the scan SHALL set its status back to `ok` and write a `restored` event

#### Scenario: A file that reappears with different bytes is not classified restored

- **WHEN** a file previously recorded `missing` reappears during a scan with bytes that do not match
  the digest recorded for it
- **THEN** the scan SHALL NOT classify it `restored` or set its status `ok` (see "A file that
  reappears with different bytes is not reported as restored")

#### Scenario: A move is not reported as missing-plus-added

- **WHEN** a tracked file is moved/renamed to a new path within the corpus with unchanged content
- **THEN** the scan SHALL NOT report it as a `missing` file plus an `added` file, but SHALL
  reconcile it into a single moved file per the reconciliation requirement


### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode a content modification SHALL silently re-baseline the stored hash/size/mtime with no
nag event. A `missing` file SHALL raise an unacknowledged event in BOTH modes, and so SHALL a file
that reappears with bytes that do not match the digest recorded for it (`restored_changed`): a file
that was absent and came back different is not the ordinary editing that `churn` mode exempts, and
suppressing it there would be a silent false negative in exactly the mode where the operator has
told the system to expect edits. The informational
kinds `added` and `restored` SHALL NOT nag in either mode — they are written already acknowledged.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content
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


### Requirement: A restored file closes its own open missing alerts

When a scan finds that a file previously recorded `missing` has reappeared, it SHALL mark that
file's still-open `missing` events acknowledged, recording a **system** acknowledgement
(`acknowledged_by` NULL) in the same convention already used for the informational kinds. The
condition that raised the alert no longer holds, so leaving it open leaves a red "needs action"
count that nothing in the product can clear.

This SHALL apply to **every** reappearance, including one whose bytes do not match the digest
recorded for the file. The proposition a `missing` alert asserts is that the file is *absent*, and
that proposition is false once something is at the path again; the fact that what came back is wrong
is carried by the unacknowledged `restored_changed` event, which says strictly more than the
`missing` event it replaces. Holding the `missing` event open as well would leave a nag nothing can
clear and would report one incident as two open alerts on one file, with nothing distinguishing the
one that matters. The trigger for this acknowledgement is therefore that the file **reappeared**,
never that it is healthy.

The acknowledgement SHALL be scoped to events of kind `missing` for that file. It SHALL NOT
acknowledge every event belonging to the file: an open WORM `modified` event on the same file
describes a different, still-unresolved condition, and clearing it would be a silent false negative
on a change the operator has not seen.

The acknowledgement SHALL be written in the **same committed transaction as the reappearance it
follows from**: it SHALL be applied before the commit that persists that batch of reappeared files
and their `restored` / `restored_changed` events, so no commit can ever leave a file recorded as
present while the alert that
its absence raised is still open. A failure to write it SHALL therefore fail that batch too, and the
scan's run SHALL finalize in the error state in the ordinary way rather than leaving the datastore
half-updated. It SHALL be issued in bounded batches rather than one statement per restored file, so
a mass restore does not degrade the scan.

#### Scenario: A restored file's missing alert is closed

- **WHEN** a scan finds a file that was recorded `missing` back on disk
- **THEN** the file SHALL be set `ok`, a `restored` event SHALL be written already acknowledged, and
  the file's open `missing` event(s) SHALL be marked acknowledged with `acknowledged_by` NULL

#### Scenario: A file that came back different also closes its missing alert

- **WHEN** a scan finds a file that was recorded `missing` back on disk with bytes that do not match
  the digest recorded for it
- **THEN** the file's open `missing` event(s) SHALL be marked acknowledged with `acknowledged_by`
  NULL, and the unacknowledged `restored_changed` event SHALL be what keeps the incident open

#### Scenario: An open modified alert on the same file survives the restore

- **WHEN** a restored file also has an open WORM `modified` event
- **THEN** that `modified` event SHALL remain unacknowledged

#### Scenario: Other files' alerts are untouched

- **WHEN** one file is restored while another file in the collection is still missing
- **THEN** only the restored file's `missing` events SHALL be acknowledged, and the other file's
  open `missing` event SHALL remain unacknowledged

#### Scenario: A failed acknowledgement does not leave a committed half-update

- **WHEN** the acknowledgement write fails for a batch of restored files
- **THEN** that batch's restorations SHALL NOT be committed either — no file from it SHALL be left
  recorded healthy with its `missing` alert still open — and the scan's run SHALL finalize in the
  error state

#### Scenario: A mass restore is acknowledged in batches

- **WHEN** a scan restores more files than the datastore's bound-parameter limit allows in one
  statement
- **THEN** the acknowledgement SHALL still be applied to all of them, in bounded batches, each batch
  applied before the commit that persists the restorations it covers

## ADDED Requirements

### Requirement: A file that reappears with different bytes is not reported as restored

A scan SHALL compare a reappeared file's freshly computed digest against the digest already recorded
for that file **before** overwriting that recorded digest, and SHALL classify the reappearance from
that comparison. Overwriting first and classifying afterwards asserts that the bytes which came back
are the bytes that left, without ever checking — the one claim this product exists to make, made
without evidence.

The recorded digest SHALL still be updated to the observed digest in every outcome, because the
index must continue to describe the bytes now on disk; what this requirement changes is that the
comparison happens first and decides the classification.

- Where the digests **match**, the file SHALL be classified `restored` exactly as before: status
  `ok`, an informational `restored` event written already acknowledged, and **no re-stamp** — the
  stored proof already commits to these bytes.
- Where the digests **differ**, the file SHALL be set status `modified` **in both worm and churn
  mode**, and the scan SHALL write a single **`restored_changed`** event left **unacknowledged**, so
  it alarms and is routed to the collection's alert channels. A `restored` event SHALL NOT also be
  written for that file — one reappearance is one event. The event's `detail` SHALL record **both**
  digests: the one recorded before the file went missing and the one observed now. In a `perfile`
  collection the file SHALL be queued for stamping (`ots_state` `pending`) so the new bytes receive
  their own proof; the proof for the previous bytes SHALL be preserved, not replaced (see the
  `ots-notarization` requirement "A stamp never destroys an existing proof").
- Where **no digest was recorded** for the file, nothing can be established, so the scan SHALL
  classify it `restored` as before and SHALL NOT raise an alarm; the absence of a comparable digest
  SHALL be noted in the event's `detail`.

A file classified `restored_changed` SHALL NOT be reconciled as a move, and SHALL NOT be promoted by
the deep pass's auto-baseline: it is `modified`, not `new`, so neither mechanism may quietly clear
the alarm it raised.

#### Scenario: A wrong restore is detected instead of adopted

- **WHEN** a file recorded `missing` reappears with bytes whose SHA-256 differs from the digest
  recorded for it
- **THEN** the scan SHALL set its status `modified`, write one unacknowledged `restored_changed`
  event whose `detail` carries both digests, and SHALL NOT write a `restored` event or set the file
  `ok`

#### Scenario: An identical restore is unchanged

- **WHEN** a file recorded `missing` reappears with bytes matching the digest recorded for it
- **THEN** the scan SHALL set it `ok`, write an already-acknowledged `restored` event, and SHALL NOT
  queue it for re-stamping

#### Scenario: A changed reappearance is re-stamped without losing the previous proof

- **WHEN** a file in a `perfile` collection reappears with different bytes and the scan's stamping
  pass runs
- **THEN** the file SHALL be queued `pending` and stamped, and the proof for its previous bytes
  SHALL still exist afterwards

#### Scenario: No recorded digest means no alarm

- **WHEN** a file recorded `missing` reappears and no digest was recorded for it
- **THEN** the scan SHALL classify it `restored`, SHALL NOT raise an unacknowledged event, and SHALL
  record in the event's `detail` that no digest was available to compare

#### Scenario: A changed reappearance is not swallowed by move reconciliation or auto-baseline

- **WHEN** a scan that classifies a file `restored_changed` also reconciles a move in the same run,
  and is a deep pass on a collection with auto-baseline enabled
- **THEN** the `restored_changed` file SHALL remain `modified` with its event unacknowledged

