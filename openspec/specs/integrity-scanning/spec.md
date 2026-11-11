# integrity-scanning Specification

## Purpose
TBD - created by archiving change add-scanner. Update Purpose after archive.
## Requirements
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

### Requirement: Orphaned running runs are reconciled on startup

On application startup the system SHALL mark a leftover run still recorded as `result` = `running`
with no `finished` as terminated (result `interrupted`, `finished` set) **only where that run has
reported no progress for longer than a bounded abandonment interval**. A run interrupted by process
termination would otherwise stay stuck at `running`, freezing the in-progress indicator and blocking
new operations on that collection; but the operation claim is held by a *process*, and a command-line
stamp or upgrade can legitimately hold one while the application restarts. Clearing a live claim
would admit a second writer to that collection's proofs, which is the loss the claim exists to
prevent, so startup alone SHALL NOT be treated as evidence that a run is dead.

A run's liveness SHALL be recorded as it progresses and read from the later of its last reported
progress and its start time, so a run that has not yet reported any (including rows predating the
liveness column) falls back to its start time. The abandonment interval SHALL comfortably exceed the
gap between two progress reports of the slowest operation.

The `interrupted` terminal state SHALL be distinct from `error` so that a benign restart-induced
interruption is not conflated with a genuine scan failure. `interrupted` SHALL be an allowed value of
`runs.result` but SHALL be produced only by this reconciliation — a scan/stamp/upgrade that runs to
completion SHALL still finish `ok`, `partial`, or `error`. This reconciliation SHALL clear any stale
in-progress indicator and SHALL NOT block starting a new operation on the affected collection. Like
`error`, an `interrupted` run SHALL NOT refresh scan freshness (the dead-man's switch derives from
`ok`/`partial` scan runs only), so deferring the reconciliation of a claim that may still be live
SHALL NOT affect staleness reporting.

Startup SHALL NOT be the only moment this reconciliation runs: the background scheduler SHALL also
run it on every tick, and a blocked claim SHALL reconcile a stale blocker in band (see "An abandoned
operation claim is reclaimed without a restart"), so a claim abandoned *after* startup does not
wedge its collection until the next restart.

Selecting the runs to reconcile and terminating them are separate steps, so a holder proved alive by
the selection may report progress before the termination is applied. The termination SHALL therefore
be conditional, at the moment of the write, on each run still being in progress and still stale —
re-asserting the full stale condition rather than trusting the earlier selection — and the count it
reports SHALL be the number of runs actually terminated. Revoking a lease whose holder is alive
would admit a second writer to that collection's proofs, which is the loss the claim exists to
prevent, so this reconciliation SHALL apply the same guarded write as the in-band reclamation path.

#### Scenario: Abandoned running run is cleared at startup

- **WHEN** the application starts and finds a `runs` row with `result` = `running`, no `finished`,
  and no progress reported for longer than the abandonment interval
- **THEN** that run SHALL be marked `interrupted` with `finished` set, so no collection is shown as
  perpetually scanning and a new operation can be started

#### Scenario: A run still reporting progress keeps its claim across a restart

- **WHEN** the application starts and finds a `runs` row with `result` = `running` whose last
  reported progress is recent, however long ago it started
- **THEN** that run SHALL be left `running`, so the operation still performing it keeps sole
  ownership of that collection's proofs

#### Scenario: A concurrent heartbeat defeats the startup reconciliation

- **WHEN** the reconciliation selects a `running` run as stale, and that run reports progress before
  the terminating write is applied
- **THEN** that run SHALL remain `running` with no `finished`, and the reconciliation SHALL report
  it as not terminated, so the live holder keeps sole ownership of that collection's proofs

#### Scenario: Interruption is distinguished from failure

- **WHEN** a run is reconciled by startup reconciliation versus a scan that finishes with errors
- **THEN** the reconciled run SHALL carry `result` = `interrupted` while the failed scan SHALL carry
  `result` = `error`, so the two are distinguishable in the run record

### Requirement: WORM and churn modes differ in nagging

In `worm` mode a content modification SHALL raise an unacknowledged `modified` event (a nag). In
`churn` mode an ordinary content modification SHALL silently re-baseline the stored hash/size/mtime
with no nag event. Two findings are NOT ordinary modifications and SHALL NOT be absorbed by `churn`
mode: a change that carries the corruption signature (see "Possible corruption is its own
finding") and a change that is part of an unusually large change in one scan (see "A mass change
is not absorbed"). A `missing` file SHALL raise an unacknowledged event in BOTH modes, and so SHALL a file
that reappears with bytes that do not match the digest recorded for it (`restored_changed`): a file
that was absent and came back different is not the ordinary editing that `churn` mode exempts, and
suppressing it there would be a silent false negative in exactly the mode where the operator has
told the system to expect edits. The informational
kinds `added` and `restored` SHALL NOT nag in either mode — they are written already acknowledged.

#### Scenario: Churn modification does not nag

- **WHEN** a file in a `churn` corpus changes content, its size or modification time having
  changed with it, and the scan's silent changes stay below the mass-change ceiling
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
classification for a file whose size or modification time moved: a recomputed hash that differs
from the stored hash SHALL be treated as a content modification (a `modified` nag in worm mode, a
silent re-baseline in churn mode). A recomputed hash that differs while size and modification time
are both unchanged SHALL be reported as possible corruption under either mode (see "Possible
corruption is its own finding"). Either way the file SHALL be re-queued for OTS stamping in
`perfile` collections; a recomputed hash that matches SHALL leave the file `ok`,
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
  detect the mismatch, set status `modified` and write an unacknowledged `corrupted` event, in worm
  mode and in churn mode alike

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
its `first_seen`, `sha256`, and OpenTimestamps proof (`ots_path`, `ots_state`, `ots_stamped_at`,
and the recorded proof provenance `ots_digest`, all unchanged). A
reconciled move SHALL emit a single informational `moved` event recording the old and new paths,
SHALL NOT raise a `missing` or `added` event, SHALL NOT be counted as missing or added on the run,
and SHALL increment the run's `moved` count. Reconciliation SHALL be conservative: empty
(zero-byte) files, and any content key matching more than one candidate on either side, SHALL NOT be
reconciled and SHALL retain the existing `missing` + `added` behavior (logged for visibility).
Reconciliation SHALL rewrite only the index — never the corpus bytes, and never the proof store —
and SHALL NOT re-queue the moved file for OTS stamping.

After reconciliation the retained `ots_path` names the old relpath's canonical proof location —
still the true location of the proof. The notarization capability owns what follows: its
referenced-slot stamp guard SHALL prevent any stamp from displacing that proof while the pointer
names it, and its healing sweep SHALL later relocate the proof to the new relpath's canonical
location. The scan itself SHALL NOT move proof files.

#### Scenario: A 1:1 move is reconciled to a single event

- **WHEN** a tracked file is moved/renamed to a previously-unseen path within the same corpus, its
  content unchanged, and no other file in the run shares that content
- **THEN** the scan SHALL produce one `moved` event (old → new path) and no `missing` or `added`
  event, and a single surviving `files` row SHALL hold the new `relpath` with status `ok`

#### Scenario: Moved file keeps its proof and is not re-stamped

- **WHEN** an already-stamped file in a `perfile` corpus is reconciled as a move
- **THEN** the surviving row SHALL retain its `ots_path`, `ots_state`, `ots_stamped_at`, and
  `ots_digest` unchanged, and the scan SHALL NOT mark it `pending`, stamp a new proof for it, or
  touch the proof store

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

### Requirement: An abandoned operation claim is reclaimed without a restart

The system SHALL reclaim an abandoned operation claim without requiring a process restart. Where an
attempt to start an operation on a collection is blocked by an existing in-progress run, the system
SHALL test that run's liveness (the later of its last reported progress and its start time) and,
where it has reported nothing for longer than the abandonment interval, SHALL terminate it with the
same terminal state that startup reconciliation uses (`interrupted`, `finished` set) and let the
operation proceed. Where the blocking run is still reporting progress, the operation SHALL be
refused exactly as before — liveness decides, never elapsed wall-clock or the impatience of the
caller.

This SHALL hold at **every** point that refuses an operation because the collection is busy — the
advisory readiness check an entry point makes before claiming as well as the claim itself — since a
check that reports a dead holder as busy would prevent the claim, and its reclamation, from ever
being attempted. A read made purely to display operation status SHALL NOT reclaim; it SHALL report
what the run record says. Where the reclamation cannot be carried out, the claim SHALL be reported
as held (refusing an operation is recoverable; admitting a second writer is not).

Reclamation only at startup makes the abandonment interval a *restart*, not a timeout: an
operation killed after startup keeps a claim that blocks every later scan, stamp and upgrade on its
collection until the service is restarted, and forever in a deployment that runs no web process or
scheduler at all. Because the claim is the gate on scanning, such a collection stops being monitored
silently. Reclamation SHALL therefore be available on the claim path itself, so it holds for every
entry point — panel, scheduler, and command line — including deployments with no background
scheduler.

The termination SHALL be conditional, at the moment of the write, on the run still being in progress
and still stale, so a claim holder that reports progress concurrently with the reclamation keeps its
claim and the blocked claim is refused. Reading liveness and revoking the claim are separate steps,
and revoking a claim whose holder is alive would admit a second writer to that collection's proofs —
the loss the claim exists to prevent — so the stale condition SHALL be re-asserted as part of the
revoking write rather than trusted from the earlier read.

The background scheduler SHALL additionally run the startup reconciliation on every tick, so a
collection nobody is currently operating on also has its abandoned claim cleared and its in-progress
indicator corrected. This periodic sweep SHALL NOT replace the reclamation on the claim path, which
is the only path available to a scheduler-less deployment. All reclamation paths SHALL apply the
same abandonment interval and the same terminal state, and SHALL be safe to run concurrently.

#### Scenario: A stale claim is reclaimed by the next claim attempt without a restart

- **WHEN** a collection has an in-progress run that has reported no progress for longer than the
  abandonment interval, no startup or periodic reconciliation has run since, and a new scan or stamp
  attempts to claim that collection
- **THEN** the abandoned run SHALL be marked `interrupted` with `finished` set and the new operation
  SHALL be granted the claim

#### Scenario: The busy check in front of an operation reclaims as well

- **WHEN** an entry point checks whether a collection is busy before claiming it, and the
  in-progress run it finds has reported no progress for longer than the abandonment interval
- **THEN** that run SHALL be reclaimed and the check SHALL report the collection as free, while a
  read made only to display operation status SHALL leave the run record untouched

#### Scenario: A live claim is never reclaimed by a blocked claim

- **WHEN** a collection has an in-progress run that reported progress within the abandonment
  interval and another operation attempts to claim that collection
- **THEN** the in-progress run SHALL keep its claim untouched and the attempting operation SHALL be
  refused

#### Scenario: A concurrent heartbeat defeats the reclamation

- **WHEN** a blocked claim reads a blocking run as stale, and that run reports progress before the
  reclamation's write is applied
- **THEN** the run SHALL remain in progress and the blocked claim SHALL be refused

### Requirement: An operation reports its claim alive independently of the work it is doing

An in-progress operation SHALL refresh the liveness of its collection claim on a schedule that does
not depend on it finishing a unit of work, for as long as it is running. Liveness that is only
written when a batch, a file or a proof completes measures the *shape of the work*, not whether the
process is alive: hashing one multi-terabyte file, or a single batch stalled on slow storage, can
exceed the abandonment interval on its own, so an operation that is working perfectly starves its
own claim and is legitimately reclaimed — after which two operations hold the same collection and
the second writer this claim exists to exclude is admitted by the machinery meant to prevent it.

The refresh SHALL be written outside the operation's own transaction, so that it lands while the
operation is mid-unit and is not held back by whatever the operation has open, and it SHALL be
conditional on the claim still being in progress, so that it can neither revive nor rewrite the
liveness of a run that has already been reclaimed or finished.

A failure to refresh SHALL NOT fail the operation, and repeated failure SHALL NOT loop
indefinitely: the refresh is a liveness signal, not part of the work. Where it can no longer be
written the operation SHALL continue, its claim SHALL be allowed to age out, and the fence below is
what prevents it from mutating anything afterwards. The refresh SHALL stop as soon as the run is no
longer the collection's in-progress claim.

#### Scenario: A single long unit of work does not starve the claim

- **WHEN** an operation spends longer than the abandonment interval inside one unit of work, with no
  batch, file or proof completing during that time
- **THEN** its claim's liveness SHALL still advance, so that no reclamation path treats it as
  abandoned

#### Scenario: A liveness refresh that cannot be written does not fail the operation

- **WHEN** the liveness refresh cannot be written
- **THEN** the operation SHALL continue and SHALL NOT fail, and the refresh SHALL stop rather than
  retry without limit

### Requirement: An operation that has lost its claim stops instead of continuing to write

An operation SHALL confirm, against the datastore, that it still holds its collection's claim
immediately before each point at which it commits a change or mutates the collection's proofs, and
SHALL stop where it stands if it does not. Reclamation is the correct response to a claim that has
stopped reporting liveness, but an operation that is reclaimed and does not notice is exactly the
second writer the claim exists to exclude: it goes on writing over a collection another operation
now owns.

The confirmation SHALL be read outside the operation's own open transaction, so it cannot be
answered from a snapshot taken before the reclamation was committed by another connection, and a
confirmation that cannot be obtained SHALL be treated as a claim that is no longer held — stopping
work is recoverable and the next pass takes it up, while continuing under a claim that cannot be
shown is not.

Once the claim is found lost, the operation SHALL commit nothing further: work already committed
under the valid claim stands, the unit in flight at the moment of detection SHALL be discarded, and
no further unit SHALL begin. The operation SHALL NOT write its own terminal state over the state the
reclamation recorded — that state is the record that the work was cut short, and replacing it with a
completed result would let a pass that never finished refresh scan freshness. The event SHALL be
reported prominently in the logs, naming the collection and the run whose claim was reclaimed, and
the operation SHALL NOT report the pass as completed.

#### Scenario: A scan whose claim is reclaimed mid-run stops without committing its in-flight batch

- **WHEN** a scan's claim is reclaimed while it is running
- **THEN** the scan SHALL discard the batch in flight, SHALL NOT commit any further batch, SHALL NOT
  run its stamping pass, and SHALL NOT report the scan as a completed pass

#### Scenario: A reclaimed run's terminal state survives the operation that lost it

- **WHEN** an operation whose claim was reclaimed reaches the point where it would record its result
- **THEN** the run SHALL keep the terminal state the reclamation wrote, the operation SHALL NOT
  overwrite it, and the reclamation SHALL be reported in the logs

#### Scenario: An operation that keeps its claim is unaffected

- **WHEN** an operation runs to completion holding its claim throughout
- **THEN** every commit, every proof placement and the recorded result SHALL be exactly as they were
  before the confirmation was introduced

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

### Requirement: A scan refuses an unreachable or empty root

A scan SHALL NOT interpret a collection root it cannot read as a collection whose files were all
deleted. Before walking, the scan SHALL verify that the root is an existing directory. After
walking, and before any tracked file is classified missing, the scan SHALL check whether it found
any file it can track — a regular file with a storable name, whether or not it could then be read:
where it found none while the collection holds at least one record that is not `missing`, it SHALL
refuse the pass. A symbolic link, and a file whose name cannot be stored, are not such files. An empty mount point is the usual appearance of a volume that
did not mount, and it cannot be told apart from a deliberate deletion of everything, so the
reading that destroys nothing is the default.

A refused pass SHALL change no file record and write no event. Its run SHALL be recorded with
result `error` and SHALL carry a human-readable reason naming the root and which of the two checks
failed. It SHALL NOT refresh the collection's scan freshness, so a root that stays unreachable
still ages into the stale state the health check reports.

An operator who really has emptied a collection SHALL be able to say so explicitly: the
command-line scan SHALL accept an option that permits a pass over an empty root, and only a pass
given that option SHALL mark the collection's files missing in that situation. Scheduled scans and
scans started from the panel SHALL never run with it.

A root that is reachable and holds some files SHALL be scanned normally, however many tracked
files it no longer holds: a folder that was deleted, or a nested volume that is gone, is reported
as missing files — **provided the walk itself completed**. Where a directory under the root
could not be listed (a permission or input/output error during the walk), the scan cannot tell
"this folder's files are gone" from "this folder could not be read", so that pass SHALL NOT mark
missing any file that lies **beneath that directory**. Files elsewhere in the collection SHALL be
judged as usual — an unreadable folder takes away knowledge of what is inside it and nothing else,
and a directory this process can never list (a root-owned `lost+found`, a storage appliance's
recycle folder) must not switch off deletion detection for the whole collection. Where the
unreadable directory is the root itself, or cannot be placed under it, no file SHALL be marked
missing. The pass SHALL still record what it found in the directories it could read, SHALL count
each unreadable directory among the run's errors with a diagnostic entry, and SHALL end `partial`.
A pass is **incomplete** when a tracked file lies beneath a directory it could not list. The same holds one level down: a file whose name the walk listed but which could not be examined
(its metadata could not be read) is present, not deleted, and SHALL NOT be marked missing — it is
counted among the files the run skipped. A file that was listed but not examined — its metadata could not be read, or reading its bytes
failed — SHALL NOT be treated as verified by that pass either: in particular a deep pass SHALL NOT
graduate it from `new`.

This rule takes precedence over the empty-root refusal: a walk that saw no file because
the directories holding them could not be listed is an unreadable folder, not an empty one, and
SHALL be reported as such.

A pass that did not cover the collection SHALL NOT count as a deep verification: where a deep pass
was refused, ended in error, or was incomplete, the collection's record of when it was last
deep-verified SHALL NOT advance, so the deep pass is still owed. A deep pass that covered every
tracked file's directory and merely skipped individual files it could not read or store SHALL
count, as it does today — and so SHALL one whose only unreadable directories hold nothing tracked.
A deep pass that is still owed for this reason SHALL NOT be retried on every scheduler tick: it
SHALL be attempted again no sooner than a day later, so that a collection whose deep pass cannot
complete neither re-hashes itself on every cadence nor keeps the other collections from their own
deep passes.

#### Scenario: A root that does not exist

- **WHEN** a collection whose root path no longer exists, or is no longer a directory, is scanned
- **THEN** no file record SHALL change, no event SHALL be written, and the run SHALL end `error`
  with a reason naming the root

#### Scenario: A root that is empty while files are tracked

- **WHEN** a collection that tracks files is scanned and its root exists but holds no file
- **THEN** no tracked file SHALL be marked `missing`, no event SHALL be written, and the run SHALL
  end `error` with a reason saying the folder is empty while files are tracked

#### Scenario: The override marks an emptied collection missing

- **WHEN** the command-line scan is run with the allow-empty option on a collection whose root is
  an existing, empty directory
- **THEN** the tracked files SHALL be marked `missing` as an ordinary scan would

#### Scenario: A collection that tracks nothing is not refused

- **WHEN** a collection with no tracked files (or only `missing` records) is scanned over an empty
  root
- **THEN** the pass SHALL complete normally

#### Scenario: A partly missing tree is scanned

- **WHEN** a collection's root holds some of its tracked files and not others
- **THEN** the pass SHALL complete, the absent files SHALL be reported `missing`, and the run SHALL
  NOT be refused

#### Scenario: An unreadable subfolder does not read as deleted files

- **WHEN** a directory under the root cannot be listed during the walk, and tracked files lie
  beneath it
- **THEN** none of the files beneath it SHALL be marked `missing` by that pass, the run SHALL end
  `partial` with the unreadable directory counted and sampled among its errors, and changes found
  in readable directories SHALL still be recorded

#### Scenario: A deletion elsewhere is still found

- **WHEN** one folder of a collection cannot be listed, and a tracked file in a different, readable
  folder has been deleted
- **THEN** that file SHALL be marked `missing` by the same pass

#### Scenario: An unreadable folder holding nothing tracked changes nothing else

- **WHEN** a directory under the root cannot be listed and no tracked file lies beneath it
- **THEN** the pass SHALL be reported `partial` naming that directory, SHALL NOT be incomplete,
  SHALL detect deletions as usual, and — if it was a deep pass — SHALL count as one

#### Scenario: A file whose bytes could not be read is not graduated

- **WHEN** a deep pass over a collection that graduates new files fails to read the bytes of a
  file recorded `new`
- **THEN** that file SHALL stay `new`, and SHALL NOT be marked `missing`

#### Scenario: A deep pass that cannot complete backs off

- **WHEN** a collection's due deep pass is refused or incomplete, and another collection also owes
  a deep pass
- **THEN** on the next scheduler tick the first collection SHALL be scanned without a deep pass,
  and the other collection SHALL receive its deep pass

#### Scenario: A file that is listed but cannot be examined is not missing

- **WHEN** a tracked file's name is returned by the walk but its metadata cannot be read
- **THEN** the file SHALL NOT be marked `missing`, and the run SHALL count it as skipped and end
  `partial`

#### Scenario: An unexamined file is not graduated

- **WHEN** a deep pass over a collection that graduates new files cannot read the metadata of a
  file recorded `new`
- **THEN** that file SHALL stay `new`

#### Scenario: A file Cairn cannot track does not make an empty root pass

- **WHEN** a collection that tracks files is scanned and its root holds nothing but a file whose
  name cannot be stored
- **THEN** the pass SHALL be refused as an empty root, and no file SHALL be marked `missing`

#### Scenario: A stray symbolic link does not make an empty root pass

- **WHEN** a collection that tracks files is scanned and its root holds nothing but a symbolic link
- **THEN** the pass SHALL be refused as an empty root, and no file SHALL be marked `missing`

#### Scenario: A refused deep pass is still owed

- **WHEN** the scheduler runs a collection's due deep pass and that pass is refused, or ends in
  error
- **THEN** the collection's last-deep-verified time SHALL be unchanged

#### Scenario: A deep pass that could not list a folder is still owed

- **WHEN** a due deep pass ends `partial` because a directory holding tracked files could not be
  listed
- **THEN** the collection's last-deep-verified time SHALL be unchanged
- **AND WHEN** a due deep pass ends `partial` only because an individual file was skipped, the
  time SHALL advance

#### Scenario: Unreadable takes precedence over empty

- **WHEN** every tracked file lies beneath a directory that cannot be listed, and the walk
  therefore sees no file at all
- **THEN** the run SHALL end `partial` with that directory diagnosed, SHALL NOT be reported as an
  empty root, and no file SHALL be marked `missing`

#### Scenario: A refused pass does not refresh freshness

- **WHEN** a collection's only recent scan runs were refused
- **THEN** the health check SHALL treat the collection as it would with no completed scan in that
  period

### Requirement: Possible corruption is its own finding

A scan SHALL report possible corruption when it finds a tracked file's digest different from the
recorded one **while its size and its modification time both equal the recorded ones**: it SHALL
set the file `modified` and write an unacknowledged event of kind `corrupted`. An edit moves the
modification time; a digest that changed behind an unchanged size and time is the signature of
bit-rot or of a write that bypassed the file system's bookkeeping, and it is never the routine
editing the `churn` policy exists to exempt. The finding SHALL therefore be raised under `worm` and
under `churn` alike, and SHALL be distinguishable from an ordinary modification everywhere it is
reported.

Where no modification time is on record for the file — a manifest import that carried none — a
digest that differs behind an unchanged size SHALL be reported the same way: the only thing that
makes a change of contents an ordinary edit is evidence that the file was written, and there is
none.

A `corrupted` event is a change alert: every rule that applies to an open `modified` or
`restored_changed` event — in particular that a file carrying one is never recorded healthy — SHALL
apply to it. A file with no recorded digest SHALL NOT be reported as corrupted: there is nothing to
compare against.

#### Scenario: Corruption in a churn collection is flagged

- **WHEN** a deep scan of a `churn` collection finds a file whose bytes changed while its size and
  modification time did not
- **THEN** the file SHALL be `modified`, an unacknowledged `corrupted` event SHALL exist for it,
  and the file SHALL NOT have been silently re-baselined

#### Scenario: Corruption in a worm collection is named as such

- **WHEN** a deep scan of a `worm` collection finds the same signature
- **THEN** the event written SHALL be of kind `corrupted`, not `modified`

#### Scenario: An edit that preserved the size is still an edit

- **WHEN** a file's bytes and modification time changed while its size did not
- **THEN** the change SHALL be classified as an ordinary modification, not as corruption

#### Scenario: A same-size change with no recorded date is not absorbed

- **WHEN** a `churn` collection holds a record with a digest and a size but no modification time,
  and the file's bytes change while its size does not
- **THEN** the scan that hashes it SHALL record it `modified` with an unacknowledged `corrupted`
  event rather than silently re-baselining it

#### Scenario: A file with no recorded digest is not corrupted

- **WHEN** a scan hashes a tracked file for the first time because no digest was on record
- **THEN** no `corrupted` or `modified` event SHALL be written for it

#### Scenario: A corrupted file stays an issue

- **WHEN** a file with an open `corrupted` event is renamed, or deleted and restored with the
  corrupted bytes that were recorded when the corruption was found
- **THEN** it SHALL still be recorded `modified` after the scan that sees it

#### Scenario: Restoring a corrupted file's original bytes closes it

- **WHEN** a file with an open `corrupted` event is replaced by, or deleted and restored with, the
  bytes it had before the corruption was found
- **THEN** it SHALL be recorded `ok` and its `corrupted` event SHALL be acknowledged, under the
  return rule of "The contents on record before an unaccepted change are kept"

### Requirement: The contents on record before an unaccepted change are kept

When a scan finds that a healthy file has changed and records it `modified`, it SHALL keep the
digest, size and modification time that were on record before the change, alongside the new ones,
until the change is accepted. Further changes to a file that is already `modified` SHALL NOT
replace what was kept: they are further unaccepted changes away from the same last-healthy state.
Where a missing file reappears with different bytes and nothing is yet kept for it — it was
healthy when it went missing — what is kept SHALL be the digest recorded when it went missing.
Where something is already kept, it SHALL be left as it is: the digest recorded when the file went
missing is then itself an unaccepted change, and adopting it as "the contents before the change"
would let that unaccepted version close the issue later as a return. Every accept of the file SHALL discard what was kept, and a healthy file
SHALL never carry any.

Each `modified` and `corrupted` event SHALL record what the file was and what it became — digest,
size and modification time on both sides — together with the file's path, so that the event still
says what it was about after the file's record has been removed.

**A file that returns to the kept contents closes its own issue.** Whenever a scan holds a digest
for a file that equals the kept one — because a changed file was put back, or because a file that
had changed and then gone missing was restored from a copy of the original — the scan SHALL record
the file `ok` with what it found, discard what was kept, acknowledge every open change alert of
that file as a system acknowledgement in the same committed transaction, and write one
informational event saying the file returned to the contents on record before it changed. It SHALL
raise no alarm. A file for which contents are kept SHALL be re-read by **every** pass, quick or
deep, whether or not its size or modification time moved: a restore that preserves metadata puts
the original bytes back under the very size and time already on record — for a corrupted file they
never differed — and a pass that trusted them would never see that the issue had closed itself.
Equality of digests is the only test: a matching size or name SHALL NOT be treated
as a return. On reappearance the order of tests SHALL be: equals the recorded digest (the file is
back as it was last seen); equals the kept digest (the file is back as it was before it changed);
otherwise it came back different. A record with no digest on record has nothing to be compared
with and nothing kept; its reappearance is governed, unchanged, by the existing requirement "A
file that reappears with different bytes is not reported as restored".

#### Scenario: A change keeps what it replaced

- **WHEN** a healthy file in a `worm` collection changes and is scanned
- **THEN** the file SHALL be `modified`, its record SHALL hold both the new digest and the digest,
  size and modification time it had before, and its `modified` event SHALL record both sides and
  the path

#### Scenario: A second change does not replace what was kept

- **WHEN** a `modified` file changes again before it is accepted
- **THEN** the kept digest SHALL still be the one from before the first change

#### Scenario: Putting the original back closes the issue

- **WHEN** a `modified` file is restored to exactly the bytes it had before it changed, and scanned
- **THEN** the file SHALL be `ok`, nothing SHALL be kept for it any more, its open change alerts
  SHALL be acknowledged with no acknowledging user, an informational event SHALL record the return,
  and no alert SHALL be dispatched

#### Scenario: A restore that preserves the file's size and date closes on a quick scan

- **WHEN** a file found possibly corrupted is replaced by its original bytes with its original
  size and modification time, and a quick (non-deep) scan runs
- **THEN** the file SHALL be `ok` after that scan, and its `corrupted` event SHALL be acknowledged

#### Scenario: A changed file that was deleted and restored from the original closes too

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  bytes it had before it changed
- **THEN** the file SHALL be `ok`, its `modified` and `missing` alerts SHALL be acknowledged, and no
  `restored_changed` event SHALL be written

#### Scenario: Coming back as the changed version is still unresolved

- **WHEN** a file is found changed and not accepted, then found missing, then found again with the
  changed bytes
- **THEN** the file SHALL be `modified`, and what was kept SHALL still be the digest from before the
  change

#### Scenario: A changed file that vanished and came back as a third version keeps the original

- **WHEN** a file is found changed and not accepted, then found missing, then found again with
  bytes matching neither the changed version nor the original, and is later put back as the
  changed version
- **THEN** the file SHALL stay `modified` throughout, what is kept SHALL still be the digest from
  before the first change, and only a return to that digest SHALL close the issue

#### Scenario: Something else again is still a change

- **WHEN** a `modified` file changes to bytes that match neither the recorded digest nor the kept
  one
- **THEN** it SHALL stay `modified` and a further alert SHALL be raised as for any change

#### Scenario: Accepting discards what was kept

- **WHEN** a `modified` file is accepted — on its own, as part of a set, or with its collection
- **THEN** its record SHALL hold no kept contents, and a later return to the old bytes SHALL be
  reported as a change

#### Scenario: Same size is not a return

- **WHEN** a `modified` file changes to different bytes that happen to have the size it had before
  the first change
- **THEN** the file SHALL NOT be recorded `ok`

### Requirement: A mass change is not absorbed

A scan of a `churn` collection SHALL NOT silently accept an unusually large change. Where the
content changes a pass would re-baseline silently number at least a configured minimum **and** at
least a configured share of the files the collection tracks, the scan SHALL instead record every
one of them `modified`, keep the contents each replaced, write an unacknowledged `modified` event
for each that states how many files changed in that pass, and raise an alarm. Below either
threshold the pass SHALL behave as before. The policy tells the system to expect edits; it does not
tell it to expect the whole collection to be rewritten overnight, and a rule with no ceiling
absorbs a disaster as readily as a typo.

The rule is evaluated once per pass, on the changes that pass itself re-baselined. It does not
accumulate across passes: a pass that is interrupted has absorbed what it had already committed,
and the pass that follows counts only what it finds changed. This limit is part of the
requirement, not an oversight — it is what the rule guarantees.

The share SHALL be computed over the records the collection tracked when the pass began, missing
ones excluded. Setting the share to zero SHALL disable the rule. The rule SHALL NOT apply to
`worm` collections, where every change is already reported, nor to deletions, which are already
reported file by file.

#### Scenario: A burst over both thresholds becomes issues

- **WHEN** a pass over a `churn` collection finds changed files numbering at least the minimum and
  at least the configured share of the collection
- **THEN** every one of those files SHALL be `modified` with an unacknowledged `modified` event,
  and the scan SHALL count them as a mass change

#### Scenario: A burst under the share is absorbed

- **WHEN** the changed files reach the minimum count but not the configured share
- **THEN** they SHALL be re-baselined silently, as ordinary edits

#### Scenario: A burst under the minimum is absorbed

- **WHEN** most of a small collection changes but the number of changed files is below the minimum
- **THEN** they SHALL be re-baselined silently

#### Scenario: The rule can be disabled

- **WHEN** the configured share is zero
- **THEN** no number of changes in one pass SHALL be converted

#### Scenario: Converted files keep what they replaced

- **WHEN** the mass-change rule converts a file
- **THEN** its record SHALL hold the digest it had before the pass, so putting it back closes the
  issue

