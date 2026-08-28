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
