# integrity-scanning Specification

## Purpose
TBD - created by archiving change add-scanner. Update Purpose after archive.
## Requirements
### Requirement: Scan classifies files with fast-path hashing

A scan SHALL walk a corpus root (honoring its exclude globs), diff the filesystem against the
`files` table by relative path, and classify each file as `added`, `modified`, `missing`, `ok`,
or `restored`. A file whose relative path the datastore cannot store (see "Scan tolerates paths the
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

- **WHEN** a file previously recorded `missing` reappears during a scan
- **THEN** the scan SHALL set its status back to `ok` and write a `restored` event

#### Scenario: A move is not reported as missing-plus-added

- **WHEN** a tracked file is moved/renamed to a new path within the corpus with unchanged content
- **THEN** the scan SHALL NOT report it as a `missing` file plus an `added` file, but SHALL
  reconcile it into a single moved file per the reconciliation requirement

### Requirement: Each scan records a run

Every scan of a corpus SHALL create a `runs` row capturing start and finish times, the counts of
added/modified/missing/stamped/upgraded, and a result of `ok`, `partial`, or `error`. Per-file IO
or permission errors SHALL be counted and SHALL NOT abort the whole scan.

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

### Requirement: Orphaned running runs are reconciled on startup

On application startup the system SHALL mark any leftover run still recorded as `result` =
`running` with no `finished` as terminated (result `interrupted`, `finished` set), since a restarted
process cannot have an operation still running. A run interrupted by process termination would
otherwise stay stuck at `running`. The `interrupted` terminal state SHALL be distinct from `error`
so that a benign restart-induced interruption is not conflated with a genuine scan failure.
`interrupted` SHALL be an allowed value of `runs.result` but SHALL be produced only by this
reconciliation — a scan/stamp/upgrade that runs to completion SHALL still finish `ok`, `partial`,
or `error`. This reconciliation SHALL clear any stale in-progress indicator and SHALL NOT block
starting a new operation on the affected corpus. Like `error`, an `interrupted` run SHALL NOT
refresh scan freshness (the dead-man's switch derives from `ok`/`partial` runs only).

#### Scenario: Leftover running run is cleared at startup

- **WHEN** the application starts and finds a `runs` row with `result` = `running` and no `finished`
- **THEN** that run SHALL be marked `interrupted` with `finished` set, so no corpus is shown as
  perpetually scanning and a new scan can be started

#### Scenario: Interruption is distinguished from failure

- **WHEN** a run is reconciled by startup reconciliation versus a scan that finishes with errors
- **THEN** the reconciled run SHALL carry `result` = `interrupted` while the failed scan SHALL carry
  `result` = `error`, so the two are distinguishable in the run record

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

### Requirement: Accept re-baselines and acknowledges

Accepting a corpus SHALL set its `new` and `modified` files to `ok`, remove the rows for `missing`
files (accepted as gone), and mark every unacknowledged event for that corpus acknowledged
(recording who and when). Accepting again with nothing pending SHALL be a no-op.

#### Scenario: Accept clears pending changes

- **WHEN** a corpus has modified, new, and missing files and `accept` is run
- **THEN** modified/new files SHALL become `ok`, missing rows SHALL be deleted, and all
  unacknowledged events SHALL be marked acknowledged

#### Scenario: Accept is idempotent

- **WHEN** `accept` is run on a corpus with no pending changes or unacknowledged events
- **THEN** it SHALL make no changes

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

### Requirement: Deep verify re-hashes every tracked file

A deep scan SHALL recompute the SHA-256 of every tracked, non-missing file regardless of its size
and mtime, so that a content change that leaves size and mtime unchanged (silent bit-rot) is
detected — a case the size+mtime fast-path cannot catch. A deep scan SHALL reuse the standard
classification: a recomputed hash that differs from the stored hash SHALL be treated as a content
modification (a `modified` nag in worm mode, a silent re-baseline in churn mode) and re-queued for
OTS stamping in `perfile` collections; a recomputed hash that matches SHALL leave the file `ok`,
refresh `last_checked`, and SHALL NOT re-queue it for stamping. Each run SHALL record whether it
was a deep pass.

When the collection has `auto_baseline_new` enabled, a deep scan SHALL additionally promote to `ok`
every file that, after classification and the missing-sweep, is still `new` and was present and
intact this pass (its re-hash matched). Files reclassified `modified` or `missing` this pass, and
files first discovered by this pass, SHALL NOT be promoted. The promotion SHALL apply only on a deep
pass (a quick scan SHALL NOT auto-baseline) and SHALL NOT re-stamp the file (a `new` file was already
stamped when first seen). When `auto_baseline_new` is disabled (the default), a deep scan SHALL leave
`new` files `new`.

#### Scenario: Silent bit-rot is detected on a deep pass

- **WHEN** a tracked file's bytes change but its size and mtime are unchanged
- **THEN** a normal (non-deep) scan SHALL NOT detect it, AND a deep scan SHALL recompute its hash,
  detect the mismatch, and in worm mode set status `modified` and write a `modified` event

#### Scenario: Intact file on a deep pass is not re-stamped

- **WHEN** a deep scan recomputes the hash of a file whose bytes are unchanged
- **THEN** the file SHALL stay `ok` (or, when `auto_baseline_new` is on, graduate `new → ok`), its
  `last_checked` SHALL refresh, and it SHALL NOT be re-queued for OTS stamping

#### Scenario: Deep pass is recorded on the run

- **WHEN** a collection is scanned in deep mode
- **THEN** its `runs` row SHALL record that it was a deep pass (and a non-deep scan SHALL record
  that it was not)

#### Scenario: Auto-baseline graduates intact new files on a deep pass

- **WHEN** a collection with `auto_baseline_new` enabled is deep-scanned and a file already tracked
  as `new` re-hashes intact
- **THEN** that file SHALL be promoted to `ok`, while any file reclassified `modified` or `missing`
  this pass SHALL be left as-is and SHALL NOT be auto-accepted

#### Scenario: Auto-baseline is off by default and quick scans never promote

- **WHEN** a collection has `auto_baseline_new` disabled, OR any collection is scanned with a quick
  (non-deep) pass
- **THEN** `new` files SHALL remain `new`

### Requirement: Hash throughput benchmark estimates deep-scan cost

The system SHALL provide a read-only benchmark that measures local SHA-256 throughput and SHALL
optionally estimate the deep-scan duration of each corpus as the corpus's total tracked size
divided by the measured throughput. The benchmark SHALL NOT modify any file, proof, or database
row.

#### Scenario: Benchmark reports throughput and a per-corpus estimate

- **WHEN** the operator runs the benchmark with the estimate option
- **THEN** it SHALL print a measured MB/s throughput and, for each corpus, an estimated deep-scan
  duration derived from that throughput and the corpus's total size

### Requirement: Content-addressed move/rename reconciliation

A scan SHALL reconcile moved/renamed files before it routes alerts, stamps proofs, or finalizes the
run. After the missing-sweep, a file newly classified `missing` whose stored SHA-256 **and** size
match **exactly one** file newly classified `added` in the same scan — where that content key is
shared by no other `missing` or `added` file in the run — SHALL be treated as a single moved file
rather than an independent deletion plus addition. Reconciliation SHALL preserve the original file's
identity: one surviving `files` row SHALL carry the new `relpath` with status `ok` while retaining
its `first_seen`, `sha256`, and OpenTimestamps proof (`ots_path`, `ots_state`, `ots_stamped_at`). A
reconciled move SHALL emit a single informational `moved` event recording the old and new paths,
SHALL NOT raise a `missing` or `added` event, SHALL NOT be counted as missing or added on the run,
and SHALL increment the run's `moved` count. Reconciliation SHALL be conservative: empty
(zero-byte) files, and any content key matching more than one candidate on either side, SHALL NOT be
reconciled and SHALL retain the existing `missing` + `added` behavior (logged for visibility).
Reconciliation SHALL rewrite only the index — never the corpus bytes — and SHALL NOT re-queue the
moved file for OTS stamping.

#### Scenario: A 1:1 move is reconciled to a single event

- **WHEN** a tracked file is moved/renamed to a previously-unseen path within the same corpus, its
  content unchanged, and no other file in the run shares that content
- **THEN** the scan SHALL produce one `moved` event (old → new path) and no `missing` or `added`
  event, and a single surviving `files` row SHALL hold the new `relpath` with status `ok`

#### Scenario: Moved file keeps its proof and is not re-stamped

- **WHEN** an already-stamped file in a `perfile` corpus is reconciled as a move
- **THEN** the surviving row SHALL retain its `ots_path`/`ots_state`/`ots_stamped_at`, and the scan
  SHALL NOT mark it `pending` or stamp a new proof for it

#### Scenario: Ambiguous content does not reconcile

- **WHEN** a `missing` file's content (sha256 + size) matches more than one `added` file, or is a
  zero-byte file
- **THEN** the scan SHALL NOT reconcile it and SHALL retain the `missing` + `added` classification,
  logging the fallback

#### Scenario: A real deletion is unaffected

- **WHEN** a tracked file is deleted and no `added` file in the run matches its content
- **THEN** the scan SHALL still classify it `missing` and write a `missing` event

### Requirement: Scan tolerates paths the datastore cannot store

A scan SHALL NOT abort, and SHALL NOT leave any file's relative path partially written, when a file
under the corpus root has a name the datastore cannot store. A relative path is *un-storable* when it
does not round-trip through UTF-8 (a non-UTF-8 on-disk name, which the OS surfaces as lone surrogate
characters that SQLite TEXT cannot bind). For each such file the scan SHALL skip it without creating
or updating a `files` row, SHALL count it among the run's errors so the run finishes `partial` (or
`error`), and SHALL log the skipped path(s) so an operator can locate them. Because no `files` row is
created, a skipped file SHALL NOT subsequently be reported as `missing` or `added` on any scan, and
SHALL NOT churn alerts across scans. Storable files in the same corpus SHALL be classified and
tracked exactly as if the un-storable file were absent.

#### Scenario: A non-UTF-8 filename is skipped, not fatal

- **WHEN** a corpus contains a file whose name is not valid UTF-8 (un-storable) alongside files with
  storable names
- **THEN** the scan SHALL classify and track every storable file normally, SHALL skip the
  un-storable file without creating a `files` row for it, and SHALL finish with `result` = `partial`

#### Scenario: A skipped file does not churn across scans

- **WHEN** a corpus with an un-storable filename is scanned repeatedly with no other changes
- **THEN** each scan SHALL skip that file again with no `missing` or `added` event for it, and the
  storable files SHALL remain `ok`

#### Scenario: Skipped paths are surfaced

- **WHEN** a scan skips one or more un-storable filenames
- **THEN** the scan SHALL emit a log record identifying the count and the skipped path(s), and the
  run's non-zero error count SHALL be reflected in its `partial`/`error` result

### Requirement: A scan always reaches a terminal run state

A scan SHALL always finalize its run to a terminal `result` (`ok`, `partial`, or `error`) with
`finished` set, even if the scan body raises an unexpected exception after the run row was recorded
`running`. The scan SHALL NOT leave its run at `result` = `running`, because a run stuck `running`
blocks the corpus from any further operation (the concurrency guard refuses a second run and the
scheduler skips an in-flight corpus) until the next process restart. This in-process guarantee
complements the startup reconciliation of orphaned `running` runs: a failure during a scan SHALL
self-heal immediately, and a failure that kills the process SHALL be reconciled on the next startup.

#### Scenario: A failure mid-scan still finalizes the run

- **WHEN** the scan body raises an unexpected exception after its run row was committed `running`
- **THEN** the scan SHALL move that run to `result` = `error` with `finished` set, so the corpus is
  not left perpetually `running` and a new scan can be started without restarting the process

