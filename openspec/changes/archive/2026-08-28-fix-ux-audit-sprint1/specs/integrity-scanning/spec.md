# integrity-scanning Specification (delta)

## ADDED Requirements

### Requirement: A restored file closes its own open missing alerts

When a scan finds that a file previously recorded `missing` has reappeared, it SHALL mark that
file's still-open `missing` events acknowledged, recording a **system** acknowledgement
(`acknowledged_by` NULL) in the same convention already used for the informational kinds. The
condition that raised the alert no longer holds, so leaving it open leaves a red "needs action"
count that nothing in the product can clear.

The acknowledgement SHALL be scoped to events of kind `missing` for that file. It SHALL NOT
acknowledge every event belonging to the file: an open WORM `modified` event on the same file
describes a different, still-unresolved condition, and clearing it would be a silent false negative
on a change the operator has not seen.

The acknowledgement SHALL be written in the **same committed transaction as the restoration it
follows from**: it SHALL be applied before the commit that persists that batch of restored files and
their `restored` events, so no commit can ever leave a file recorded healthy while the alert that
its absence raised is still open. A failure to write it SHALL therefore fail that batch too, and the
scan's run SHALL finalize in the error state in the ordinary way rather than leaving the datastore
half-updated. It SHALL be issued in bounded batches rather than one statement per restored file, so
a mass restore does not degrade the scan.

#### Scenario: A restored file's missing alert is closed

- **WHEN** a scan finds a file that was recorded `missing` back on disk
- **THEN** the file SHALL be set `ok`, a `restored` event SHALL be written already acknowledged, and
  the file's open `missing` event(s) SHALL be marked acknowledged with `acknowledged_by` NULL

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
