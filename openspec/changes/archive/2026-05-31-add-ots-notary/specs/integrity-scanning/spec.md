## ADDED Requirements

### Requirement: Perfile corpora queue and stamp new and changed files

When a corpus's `ots_mode` is `perfile`, a scan SHALL mark files it classifies as `added` or
content-`modified` with `ots_state='pending'` (a queue marker) and SHALL stamp the pending files
at the end of the scan, recording the number stamped on the run. A file whose content changes
SHALL be re-stamped (each distinct content state gets its own proof). A stamp failure SHALL leave
the file `pending` for retry and SHALL NOT fail the scan. Corpora with `ots_mode='none'` SHALL
never queue or stamp.

#### Scenario: New file in a perfile corpus is queued and stamped

- **WHEN** a scan adds a new file in a `perfile` corpus
- **THEN** the file SHALL be marked for stamping and, at the end of the scan, stamped so its
  `ots_state` becomes `incomplete`

#### Scenario: Stamp failure does not fail the scan

- **WHEN** stamping a pending file fails (e.g. calendars unreachable)
- **THEN** the file SHALL remain `pending` and the scan SHALL still finish with a recorded run

#### Scenario: None corpus never stamps

- **WHEN** a scan processes a corpus whose `ots_mode` is `none`
- **THEN** no file SHALL be marked pending or stamped
