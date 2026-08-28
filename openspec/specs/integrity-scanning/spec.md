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

