# integrity-scanning Specification (delta)

## MODIFIED Requirements

### Requirement: Each scan records a run

Every scan of a corpus SHALL create a `runs` row capturing start and finish times, the counts of
added/modified/missing/stamped/upgraded, and a result of `ok`, `partial`, or `error`. Per-file IO
or permission errors SHALL be counted and SHALL NOT abort the whole scan.

The run SHALL **persist** that error count, and SHALL persist a bounded sample identifying the files
it skipped, so that a `partial` result can be explained to an operator rather than only reported.
The count is what already decides `partial`; a result an operator can see without the files it
refers to is not actionable. The sample SHALL be stored in a form the datastore can hold for
**every** cause of a skip — including a filename that could not be stored as text at all, which is
the very failure the sample most needs to report. It is therefore a diagnostic rendering of the
name, not a usable path.

The sample SHALL be bounded on three axes, so that no combination of pathological filenames can
turn a diagnostic column into an unbounded write: a maximum **number of entries**, a maximum
**size per entry**, and a maximum **total serialized size**. An entry over the per-entry limit SHALL
be truncated deterministically and SHALL carry a marker showing that it was truncated, so a
truncated rendering is never mistaken for a complete name. Where entries are dropped for any of the
three bounds, the sample SHALL state how many were dropped, so the operator is never shown a
silently shortened list. The serialized form SHALL be ASCII-only, so the stored value cannot fail to
bind for the same class of reason the sample exists to report.

A run that skipped files SHALL also **log** its error count and that same bounded sample at warning
level when it finalizes, covering every cause of a skip. Two things depend on it: the operator's
copy of the diagnosis survives independently of the database row, and the schema's downgrade may
drop the columns without destroying information that exists nowhere else.

The last-resort finalization that guarantees a run reaches a terminal state MAY leave the error count
unwritten: its sole obligation is terminality, and it runs precisely when everything else has already
failed.

A run SHALL carry a `kind`; an integrity scan SHALL have `kind = 'scan'`. The run SHALL record a
`processed` count of files handled so far, updated as the scan progresses (not only at the end), so
an in-flight scan's progress is observable by a concurrent reader. The run MAY carry a `total`
estimate of the files the scan will cover (e.g. the prior scan's processed count) for a progress
figure; when no estimate is available the `total` SHALL be absent (the scan reports indeterminate
progress). The result SHALL be `running` while the scan is in progress and SHALL transition to its
terminal value (`ok`, `partial`, or `error`) with `finished` set when the scan ends.

#### Scenario: Successful scan records counts

- **WHEN** a scan completes without fatal error
- **THEN** its `runs` row SHALL have `kind` = `scan`, `finished` set, the added/modified/missing
  counts populated, and `result` = `ok`

#### Scenario: In-progress scan exposes a growing processed count

- **WHEN** a scan is in progress over a corpus with many files
- **THEN** its `runs` row SHALL have `result` = `running` and a `processed` count that reflects files
  handled so far, observable by a concurrent reader before the scan finishes

#### Scenario: First-ever scan has no progress estimate

- **WHEN** a corpus is scanned for the first time with no prior completed scan to estimate from
- **THEN** the run SHALL carry no `total` estimate, so its progress is reported as indeterminate

#### Scenario: Unreadable file does not abort the scan

- **WHEN** one file under the root cannot be read (permissions/IO)
- **THEN** the scan SHALL continue processing the remaining files and SHALL finish with
  `result` = `partial` or `error`

#### Scenario: A partial run records how many files it skipped and which

- **WHEN** a scan skips one or more files (an un-storable name, a failed `stat`, or a hash IO error)
- **THEN** its `runs` row SHALL record the number skipped and a bounded sample identifying them,
  alongside the `partial` result

#### Scenario: An un-storable filename is recorded in a storable form

- **WHEN** a scan skips a file whose name is not valid UTF-8
- **THEN** the sample recorded on the run SHALL be a rendering of that name that the datastore can
  store, so recording the skip does not fail for the same reason the file was skipped

#### Scenario: An oversized sample entry is truncated, not stored whole

- **WHEN** a skipped file's diagnostic rendering is longer than the per-entry size limit
- **THEN** the stored entry SHALL be truncated to that limit and SHALL carry a truncation marker,
  and the run SHALL still record the true number of files skipped

#### Scenario: A flood of skipped files does not produce an unbounded sample

- **WHEN** a scan skips more files than the sample's entry or total-size budget allows
- **THEN** the stored sample SHALL stay within both bounds and SHALL state how many further skipped
  files it does not name, while the run's error count SHALL still be the true total

#### Scenario: A skipping run logs what it skipped

- **WHEN** a scan finalizes having skipped one or more files, for any cause
- **THEN** it SHALL emit a warning-level log line naming the number skipped and the same bounded
  sample, so the diagnosis exists outside the run row
