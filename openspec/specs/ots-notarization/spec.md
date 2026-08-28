# ots-notarization Specification

## Purpose
TBD - created by archiving change add-ots-notary. Update Purpose after archive.
## Requirements
### Requirement: Stamp a file's hash into a parallel proof store

The system SHALL stamp a file's SHA-256 to the OpenTimestamps calendars and store the resulting
`.ots` proof in a writable proof store laid out parallel to the corpus, WITHOUT writing anything
under the read-only corpus root. After a successful stamp the file's `ots_state` SHALL be
`incomplete`, with `ots_path` and `ots_stamped_at` recorded. Files in a `none` (tripwire) corpus
SHALL never be stamped.

#### Scenario: Stamp writes only to the proof store

- **WHEN** a file in a `perfile` corpus is stamped
- **THEN** a `.ots` proof SHALL be written under the proof store at a path derived from the
  corpus id and the file's relative path
- **AND** no file SHALL be created or modified under the corpus root
- **AND** the file's `ots_state` SHALL become `incomplete`

#### Scenario: Tripwire corpus is never stamped

- **WHEN** a scan processes a corpus whose `ots_mode` is `none`
- **THEN** no proof SHALL be created and every file's `ots_state` SHALL remain `none`

### Requirement: Upgrade incomplete proofs after Bitcoin confirms

The system SHALL upgrade `incomplete` proofs by contacting the calendars; when the Bitcoin
attestation is available the proof SHALL be rewritten complete and the file's `ots_state` set to
`complete`. A proof that has not yet been confirmed SHALL remain `incomplete` and SHALL NOT be
treated as an error.

#### Scenario: Confirmed proof becomes complete

- **WHEN** `upgrade` runs against an incomplete proof that Bitcoin has now confirmed
- **THEN** the proof SHALL be rewritten with the Bitcoin attestation and the file's `ots_state`
  SHALL become `complete`

#### Scenario: Unconfirmed proof stays incomplete

- **WHEN** `upgrade` runs against a proof the calendars have not yet anchored
- **THEN** the file SHALL remain `incomplete` and the operation SHALL NOT raise an error

### Requirement: Verify a proof by digest

The system SHALL verify a stored proof against a file's SHA-256 digest without requiring the
original file to be shipped anywhere. The result SHALL state whether the proof is verified and,
when complete, the Bitcoin block and the "existed by" date.

The result SHALL additionally distinguish, as separate reportable outcomes rather than one generic
"not verified", each reason verification did not succeed that a caller must describe differently:

- a **digest disagreement** — the supplied digest is not the digest the proof commits to. This
  outcome SHALL be reported **neutrally**: it establishes that the two digests differ, and NOT which
  of the two artifacts changed. A `.ots` whose serialized committed digest has a single flipped byte
  still deserializes and reaches this comparison exactly as a modified file does, and no attestation
  has been validated at that point either. The result SHALL therefore NOT assert that the file
  changed, and SHALL NOT assert that the proof is intact or still attests an earlier version of the
  file — neither is established by the comparison. Attributing the disagreement requires the file's
  separately recorded baseline digest, which this operation is not given, so attribution is the
  caller's (see the panel and command-line requirements);
- a **proof mismatch** — **no** Bitcoin attestation in the proof commits to its real block's merkle
  root, and at least one commits to something else. The file's bytes may be exactly what was
  stamped; what failed is the proof or the block data used to check it, so it SHALL be reported as
  its own outcome and SHALL NOT be reported as a digest mismatch;
- a **transport failure** — the verification backend could not be reached **or answered with data
  that is not a well-formed block header**, so nothing about the file or the proof was established.
  The reason SHALL be carried on the result, together with **the number of failed lookups as a
  structural value**. That count SHALL NOT be recovered by splitting the joined human-readable
  reason, because a single reason may itself contain the separator used to join them, which
  overstates how much of a proof went unchecked;
- an **inconclusive** outcome — the backend cannot distinguish "not yet anchored" from "the digest
  no longer matches" (or from its own unreachability);
- an **unreadable proof** — the stored `.ots` could not be parsed at all, so nothing was established
  about the file *or* about the proof's content. This SHALL be its own reportable outcome, because
  every other not-verified outcome presupposes a readable proof and describing this one in their
  words offers the operator possibilities that were never established.

Callers cannot otherwise tell these apart from a proof that is merely not yet anchored, which is the
false-negative this requirement exists to prevent.

Verification SHALL be **existential over the proof's Bitcoin attestations**: a result SHALL be
verified when **any** attestation's commitment equals its real block's merkle root, regardless of
what the other attestations do. A mismatching attestation beside a matching one SHALL NOT make the
result not-verified and SHALL NOT set the proof-mismatch indicator; it MAY be recorded as diagnostic
detail on the verified result. The proof-mismatch indicator SHALL therefore be set only where no
attestation validated. A proof legitimately carries more than one attestation, and reporting one bad
sibling as a failed proof is a false alarm on the signal this product exists to make trustworthy.

A transport failure SHALL be carried on the returned result at **every** point where the
implementation swallows a network or subprocess failure into an otherwise ordinary return, not only
where it raises. This includes results whose verdict was decided by something else: a result that is
verified, and a result carrying a proof mismatch, SHALL each still record any fetch failure that
occurred while reaching it. A result that is verified SHALL remain verified when some, but not all,
of its attestations could be fetched — one attestation confirmed against a real block is proof —
while still recording the transport failure. Dropping the transport reason because another outcome
was decided first hides the difference between "this proof is bad" and "this proof is bad as far as
the part of it that could be checked".

Block data fetched from an external explorer SHALL be **validated as well-formed before it is
used**: a block hash and a merkle root SHALL each be exactly 64 hexadecimal characters, and a block
time SHALL be an integer within the range a Bitcoin block time can occupy. Data failing that
validation SHALL be reported as a transport failure and SHALL NOT reach the attestation comparison.
The comparison's only possible outcome for a malformed value is inequality, which is reported as a
**proof mismatch** — a categorical failure verdict over the operator's evidence. Manufacturing that
verdict out of a badly-formed response would let an explorer bug, a truncated response or a hostile
endpoint discredit an intact proof, and an out-of-range block time would likewise become an
"existed by" date the operator is invited to rely on.

The explorer backend parses the proof locally and therefore knows the file digest the proof commits
to and the commitment each attestation makes; it SHALL set the digest-disagreement and
proof-mismatch indicators at the points it establishes them, and SHALL NOT conflate the two. The
node backend shells out to `ots verify -d`, whose output reports a mismatch as an ordinary
non-success exit indistinguishable from an unanchored proof or an unreachable node; it SHALL NOT
guess at a mismatch, because reporting one that was not established is a false alarm on the
product's core signal. Instead it SHALL mark such a result **inconclusive**, so that no caller can
present it as a proof that is merely young. That the node backend cannot separate these cases is an
accepted, documented limitation of the non-default backend.

For the node backend the **process exit status SHALL be the verification contract**: a zero exit is
a verification, and a non-zero exit is not, regardless of what the tool printed. Any block height
or date parsed out of its output is **optional metadata** on an already-decided verdict, and a
verified result carrying none SHALL remain verified. Deciding the verdict by matching the tool's
output text instead both discards a verification that actually happened (a successful exit whose
wording the pattern does not recognise) and — the dangerous direction — can report a **failed** run
as verified because its output quoted a success line.

Every failure of the verification subprocess SHALL be reported as a typed transport failure rather
than raised, **including failures of the preliminary offline classification of the proof**, which
runs the same tool. A caller that verifies has no other way to distinguish an unusable tool from a
verdict, and one of the two callers (`cairn verify`) has no error handling of its own, so an escaped
exception aborts the command instead of reporting that nothing could be checked.

#### Scenario: Verify a complete proof

- **WHEN** a complete proof is verified against the matching digest
- **THEN** the result SHALL be verified, naming the Bitcoin block and an "existed by" UTC date, and
  the mismatch, transport and inconclusive indicators SHALL all be unset

#### Scenario: Digest mismatch fails verification

- **WHEN** a proof is verified against a digest that does not match it
- **THEN** the result SHALL be not-verified

#### Scenario: A digest mismatch is reported as a digest mismatch

- **WHEN** the explorer backend verifies a proof against a digest the proof does not commit to
- **THEN** the result SHALL be not-verified **and** SHALL carry the digest-disagreement indicator, so
  a caller can tell "the file's digest and the proof's disagree" apart from "this proof is not
  confirmed yet"

#### Scenario: A digest disagreement assigns no blame

- **WHEN** a proof's own serialized committed digest has been corrupted while the file is unchanged,
  so the proof still parses but its digest no longer equals the file's
- **THEN** the result SHALL report the disagreement without stating that the file changed and
  without stating that the proof is intact or still attests an earlier version of the file

#### Scenario: A proof that cannot be parsed is its own outcome

- **WHEN** the stored `.ots` is truncated, corrupt, or not a timestamp file at all
- **THEN** the result SHALL carry the unreadable-proof indicator, SHALL NOT carry either mismatch
  indicator, and SHALL NOT be described in the words used for a proof that is merely unconfirmed

#### Scenario: An unreadable proof is reported as such by every backend

- **WHEN** the node backend verifies against a stored `.ots` that exists but cannot be deserialized
- **THEN** the result SHALL carry the unreadable-proof indicator rather than being reported as no
  usable proof, so the caller SHALL NOT offer a possible file change as an explanation

#### Scenario: A proof that was never written is not reported as damaged

- **WHEN** verification is asked for a proof path that does not exist
- **THEN** the result SHALL report that there is no usable proof and SHALL NOT carry the
  unreadable-proof indicator

#### Scenario: Malformed block data from the explorer is a transport failure

- **WHEN** the explorer returns a block whose merkle root is not 64 hexadecimal characters, or whose
  timestamp is not an integer within the range a Bitcoin block time can occupy
- **THEN** the result SHALL carry a transport failure naming the malformed field, and SHALL NOT
  carry the proof-mismatch indicator and SHALL NOT report an "existed by" date

#### Scenario: The failed-lookup count survives a reason containing the separator

- **WHEN** a single lookup failure's reason itself contains the separator used to join several
  reasons
- **THEN** the number of failed lookups reported SHALL be one

#### Scenario: A merkle-root mismatch is reported as a proof mismatch, not a file mismatch

- **WHEN** the explorer backend finds that no Bitcoin attestation's commitment equals its real
  block's merkle root and at least one differs, while the supplied digest is exactly the one the
  proof commits to
- **THEN** the result SHALL be not-verified and SHALL carry the proof-mismatch indicator, and the
  digest-mismatch indicator SHALL remain unset

#### Scenario: One valid attestation outranks a mismatched sibling

- **WHEN** a proof carries two Bitcoin attestations for the committed digest, one confirming against
  its real block and one whose commitment differs
- **THEN** the result SHALL be verified with the proof-mismatch indicator unset, and the mismatching
  attestation MAY appear only as diagnostic detail

#### Scenario: A proof mismatch alongside an unreachable fetch records both

- **WHEN** the explorer backend cannot fetch the block for one attestation and finds the only
  attestation it could fetch mismatching
- **THEN** the result SHALL carry the proof-mismatch indicator **and** the transport failure with
  its reason, so the fetch failure is not lost to the mismatch verdict

#### Scenario: An unreachable explorer is reported as a transport failure

- **WHEN** the explorer backend cannot fetch the block for any of the proof's Bitcoin attestations
- **THEN** the result SHALL be not-verified and SHALL carry the transport failure with its reason,
  and SHALL NOT present the proof's stored state as the outcome

#### Scenario: A partially reachable explorer still verifies

- **WHEN** the explorer backend fails to fetch the block for one attestation but confirms another
  against its real block
- **THEN** the result SHALL be verified, and SHALL still record the transport failure that occurred

#### Scenario: The node backend verifies on the exit status, not on the output text

- **WHEN** the node backend's verification tool exits zero but prints no line the implementation can
  parse for a block and date
- **THEN** the result SHALL be verified, with the block and date simply absent

#### Scenario: The node backend never verifies on a failed exit

- **WHEN** the node backend's verification tool exits non-zero while its output contains text that
  reads like a success line
- **THEN** the result SHALL NOT be verified, and SHALL be marked inconclusive

#### Scenario: The node backend does not guess at a mismatch

- **WHEN** the node backend verifies a proof and `ots verify -d` reports no success
- **THEN** the result SHALL be not-verified with both mismatch indicators unset **and** SHALL be
  marked inconclusive, because that backend cannot distinguish a mismatch from an unanchored proof
  or from its own unreachability, and it SHALL NOT infer which of those occurred from the tool's
  output text

#### Scenario: An unusable verification tool is a transport failure, not a pending proof

- **WHEN** the node backend cannot run the verification tool at all (missing binary, timeout)
- **THEN** the result SHALL carry the transport failure with its reason, and SHALL NOT report the
  proof's stored state as the outcome

#### Scenario: An unusable tool during the preliminary classification is also a transport failure

- **WHEN** the node backend cannot run the tool for the offline classification it performs before
  verifying
- **THEN** the result SHALL carry the transport failure with its reason, and the operation SHALL NOT
  raise, so a caller without error handling still reports that nothing could be checked

### Requirement: Export a portable proof bundle

The system SHALL export a file together with its `.ots` proof to a chosen destination so a third
party can verify independently. Export SHALL fail clearly if the file has no stored proof.

#### Scenario: Export writes file and proof

- **WHEN** export is requested for a stamped file
- **THEN** both the file's bytes and its `.ots` proof SHALL be written to the destination

### Requirement: Flag proofs stuck incomplete

The system SHALL be able to list proofs that have remained `incomplete` longer than a configured
number of days, so a never-confirmed proof can be surfaced and re-stamped.

#### Scenario: Stale incomplete proof is listed

- **WHEN** a proof has been `incomplete` for longer than the configured alarm threshold
- **THEN** it SHALL appear in the stale-incomplete list

### Requirement: Stamp pending files in batches

The system SHALL stamp the files queued in a `perfile` corpus using batched OpenTimestamps
submissions: multiple files MAY be stamped in a single `ots stamp` invocation so their digests are
aggregated into one calendar commitment, amortizing the per-file network cost. Each file in a batch
SHALL still receive its own independent `.ots` proof in the proof store with the same per-file
outcomes as a single stamp (`ots_state` becomes `incomplete`, `ots_path` and `ots_stamped_at`
recorded, counted once in `runs.stamped`). Batching SHALL NOT produce a shared/aggregate proof and
SHALL NOT write anything under the read-only corpus root. The number of files per invocation SHALL
be bounded by a configurable batch size.

#### Scenario: A batch produces one independent proof per file

- **WHEN** a `perfile` corpus has N pending files and the configured batch size is at least N
- **THEN** they SHALL be stamped in a single `ots stamp` invocation
- **AND** each file SHALL get its own `.ots` under the proof store with `ots_state` `incomplete`
- **AND** each file SHALL be counted once in the run's `stamped` total

#### Scenario: Pending exceeds the batch size

- **WHEN** the number of pending files is greater than the configured batch size
- **THEN** the files SHALL be stamped across multiple invocations, each covering at most the
  configured batch size

### Requirement: A failed batch member does not drop the batch's proofs

A stamp failure SHALL never fail the scan, and a failure affecting one file in a batch SHALL NOT
prevent the other files in that batch from being stamped. A member fails either because the batch
invocation produced no proof for it (an unreachable calendar, a timeout, one bad input aborting the
run) or because its produced proof cannot be written to its output path (an un-writable path — see
"Notarization tolerates un-writable proof output paths"). In every case the system SHALL fall back to
stamping that member individually; a member that still fails with a **transient** error SHALL be left
`pending` and logged for retry on the next pass, a member that fails because its output path is
**un-writable** SHALL be skipped and left `ots_state = none` (a permanent skip, not re-attempted every
scan), and members that succeeded SHALL retain their stored proofs. An un-writable member SHALL be
skipped before a staging symlink or a calendar submission is spent on it.

#### Scenario: One unstampable file, the rest still stamped

- **WHEN** a batch is stamped and one of its files yields no proof
- **THEN** the remaining files in the batch SHALL keep their stored `.ots` proofs and `incomplete`
  state
- **AND** the unstamped file SHALL be retried individually, and if it still fails transiently it SHALL
  be left `pending` and logged
- **AND** the scan SHALL complete without error

#### Scenario: One file with an un-writable proof path, the rest still stamped

- **WHEN** a batch is stamped and one of its files has an output proof path the filesystem refuses
  (e.g. its `.ots` name exceeds `NAME_MAX` bytes)
- **THEN** the remaining files in the batch SHALL be stamped and keep their proofs
- **AND** the un-writable file SHALL be skipped, counted, logged, and left `ots_state = none`
- **AND** the batch SHALL complete without raising

### Requirement: Automatic stamping is scoped to new and changed files; baselines are stamped on demand

Automatic stamping at the end of a scan SHALL stamp only the files that scan newly added or whose
content changed (the files it queued `pending`); it SHALL NOT stamp the pre-existing unstamped
baseline (files with `ots_state = none`). The system SHALL additionally provide an on-demand
operation that stamps every currently-unstamped file in a corpus — those with `ots_state = none`
and `status != missing` — by queueing them and stamping via the batched path. That operation SHALL
NOT re-stamp files that already hold a proof (`incomplete` or `complete`) and SHALL NOT require
re-hashing the files through a scan.

#### Scenario: A scan leaves the unstamped baseline alone

- **WHEN** a `perfile` corpus has an existing baseline of `ok` files with `ots_state = none` and a
  normal scan finds no new or changed files
- **THEN** no file SHALL be stamped and every baseline file SHALL remain `ots_state = none`

#### Scenario: A new file in that corpus is still stamped automatically

- **WHEN** a file first appears in that corpus on a later scan
- **THEN** that file SHALL be stamped automatically while the baseline files remain `none`

#### Scenario: Stamp-all backfills only unstamped files

- **WHEN** the on-demand stamp-all operation is run for a corpus
- **THEN** every file with `ots_state = none` and `status != missing` SHALL be stamped
- **AND** files that already have a proof (`incomplete` or `complete`) SHALL NOT be re-stamped

### Requirement: Stamp and upgrade operations are recorded as typed runs with progress

The on-demand stamp backfill and the OTS upgrade pass SHALL each be recorded as a `runs` row with a
`kind` distinguishing it from an integrity scan — `kind = 'stamp'` for the stamp backfill and
`kind = 'upgrade'` for the upgrade pass. Each such run SHALL set `total` to the number of items it
will process — the count of files queued for stamping, or the count of incomplete proofs to upgrade —
known at the start, and SHALL update `processed` as it advances, so a concurrent reader can observe
exact progress. The run's result SHALL be `running` while in progress and SHALL transition to a
terminal value with `finished` set when it ends.

These `stamp` and `upgrade` runs SHALL NOT affect scan-freshness reporting (the dead-man's switch),
which is derived from `kind = 'scan'` runs only. The upgrade pass SHALL record a run only for a corpus
that actually has incomplete proofs to process (it SHALL NOT create an empty run when there is no
work). Recording these runs SHALL NOT change the batched stamping or upgrade mechanics or their
per-file outcomes.

#### Scenario: Stamp backfill records a stamp run with exact progress

- **WHEN** the on-demand stamp backfill runs over a `perfile` corpus with N files queued
- **THEN** a `runs` row with `kind = 'stamp'` SHALL be created with `total` = N, `processed`
  advancing as batches are stamped, and a terminal result with `finished` set when it completes

#### Scenario: Upgrade pass records an upgrade run that does not affect freshness

- **WHEN** the upgrade pass processes a corpus that has incomplete proofs
- **THEN** a `runs` row with `kind = 'upgrade'` SHALL be created with `total` = the count of
  incomplete proofs and `processed` advancing as they are upgraded
- **AND** that run SHALL NOT count toward the corpus's scan freshness

#### Scenario: Upgrade pass with no incomplete proofs records nothing

- **WHEN** the upgrade pass processes a corpus that has no incomplete proofs
- **THEN** no `kind = 'upgrade'` run SHALL be created for that corpus

### Requirement: Notarization operations do not block the application event loop

The application's asyncio event loop SHALL NOT be blocked by OpenTimestamps subprocess work or its
accompanying file IO. Every operation that shells out to the `ots` CLI — stamping (including the
batched stamp and its per-file fallback), upgrading incomplete proofs, and verifying a proof — and
any file-content work performed alongside them in a request handler (re-hashing a file for
verification, copying bytes for an export bundle) SHALL be executed off the event loop (for example,
via a worker thread) when invoked from asynchronous code, so that a single blocking subprocess or
file-IO call does not stall concurrent panel requests for the duration of a process spawn or a
calendar/explorer network round-trip. These operations MAY remain sequential (one `ots` subprocess
at a time); the requirement is only that the event loop stays free to service other work while a
call is in flight.

#### Scenario: An upgrade pass does not freeze the panel

- **WHEN** the daily pass upgrades a large number of `incomplete` proofs (each a blocking `ots
  upgrade` subprocess) while a user loads a panel page
- **THEN** the panel request SHALL be served without waiting for the upgrade subprocesses, because
  each `ots upgrade` runs off the event loop

#### Scenario: On-demand verify does not freeze the panel

- **WHEN** a user triggers a proof verification that re-hashes the file and runs `ots verify`
  (a network round-trip)
- **THEN** the re-hash and the verify SHALL run off the event loop, so other concurrent panel
  requests are not blocked for their duration

#### Scenario: Stamping runs off the loop

- **WHEN** a scan or an on-demand backfill stamps pending files via the `ots` CLI
- **THEN** each stamp subprocess (batched call and any per-file fallback) SHALL run off the event
  loop, leaving the panel responsive while stamping proceeds

### Requirement: Notarization tolerates un-writable proof output paths

Stamping SHALL NOT abort a batch, fail a scan, or crash the process when a file's proof output path
cannot be written by the filesystem. A proof output path is *un-writable* (a **permanent** condition)
when a component **that the system itself creates below the proof-store root** (`<collection_id>/`,
the file's relpath directories, and the final `.ots` name) exceeds the filesystem's per-name limit —
`ENAMETOOLONG`; `NAME_MAX` is measured in **bytes**, so a multi-byte name such as a Cyrillic filename
plus its extension plus `.ots` can exceed it while looking short. The proof-store root's own
components SHALL NOT be validated: the operator supplied that path and the filesystem already accepted
it, so judging it would mark every proof under that store permanently un-writable and silently drop a
whole collection to `none`. For each such file the system SHALL skip writing its proof, SHALL count
it, and SHALL log the skipped path so an operator can locate it. A skipped file SHALL be left unstamped
with `ots_state = none`, no `ots_path` and no stamp time (no proof recorded, no stale pointer, and no
timestamp claiming a notarization that no proof backs), so it is not re-queued and re-attempted by
every subsequent scan; the other files in the same batch SHALL be stamped normally.
A skip SHALL NOT change the file's monitored `status`, and SHALL NOT suppress `missing`/`modified`
alerting for that file.

Only a failure on the **final proof output path** may be classified permanent. **Every** staging-side
failure — the staging directory or a staging symlink that cannot be created — SHALL be treated as
transient *regardless of its errno*, `ENAMETOOLONG` included: the overlong operand there is the
staging pathname, a property of the deployment, not of the file. Cleaning up staging paths SHALL be
best-effort and SHALL NEVER mask, replace, or escape past the classified outcome of a member.

The system SHALL treat only that permanent `ENAMETOOLONG` condition as a `none` skip. **Every other**
write failure — a full or read-only proof store, a staging failure of any kind, a cross-device staging
dir, an I/O error — SHALL be treated as **transient**: the file
SHALL be left `pending` for retry on the next pass, exactly like an
unreachable calendar or a timeout (see "A failed batch member does not drop the batch's proofs"). A
transient error SHALL NEVER drop a file to `none`, because the proof could succeed once the condition
clears and a normal scan would not re-queue a `none` file.

#### Scenario: An overlong proof name is skipped, not fatal

- **WHEN** a `perfile` collection stamps a pending set that includes a file whose `.ots` output name
  exceeds the filesystem's per-name byte limit, alongside files with writable proof names
- **THEN** the system SHALL stamp every writable-name file to `ots_state = incomplete` with its proof
  stored, SHALL skip the overlong file without writing a proof, and SHALL complete without raising

#### Scenario: A skipped file is not retried every scan

- **WHEN** a file's proof output path is un-writable and it is skipped during stamping
- **THEN** the system SHALL set that file's `ots_state` to `none` so a later normal scan (which
  queues only newly added or changed files) does not re-queue it, and SHALL record it in the run's
  stamped count as not-stamped

#### Scenario: A staging-side failure is transient whatever its errno

- **WHEN** a staging symlink for a pending file cannot be created and the failure is `ENAMETOOLONG`
  (the staging pathname, not the file's proof name, is what the filesystem refused)
- **THEN** the system SHALL leave that file `pending`, SHALL NOT drop it to `none`, SHALL NOT abort
  the stamping pass, and SHALL NOT let the underlying `OSError` escape — including from the cleanup
  of staging paths

#### Scenario: A proof store under an over-limit directory still stamps

- **WHEN** the proof store root itself contains a path component longer than the per-name byte limit
  the system assumes, and a pending file's `<collection_id>/<relpath>.ots` components are all within it
- **THEN** the system SHALL stamp that file normally and SHALL NOT classify its proof path
  un-writable

#### Scenario: A transient failure is not treated as a permanent skip

- **WHEN** a file with a writable proof name fails to stamp because the calendar is unreachable, the
  call times out (no proof produced), or its produced proof cannot be placed because of a non-fatal
  filesystem error (a full or read-only proof store)
- **THEN** the system SHALL leave that file `pending` for retry on the next pass, and SHALL NOT drop
  it to `none`

### Requirement: The command-line verify reports the reason, not the proof's stored state

`cairn verify` SHALL choose what it prints from *why* verification did not succeed, in the same
order of precedence the panel uses, and SHALL NOT reach its "pending" wording while a mismatch,
transport failure or inconclusive outcome is present on the result. The command line and the panel
are two readings of the same integrity claim; a fix applied to one and not the other leaves the
false negative live wherever the operator happens to be looking.

A **digest disagreement SHALL be attributed from the file's recorded baseline digest** before it is
reported, and the command SHALL print a different line for each attribution. Where the live bytes no
longer hash to the recorded baseline, the command SHALL report that the **file** changed, and SHALL
NOT claim that the proof was validated or remains intact. Where the live bytes still hash to the
recorded baseline, the command SHALL report that the file is not what moved, and SHALL make the same
distinction the panel makes: where the file record indicates a re-stamp is owed — its proof state is
queued for stamping, or its status is modified or new — it SHALL report that the stored proof
**predates this version** of the file with a re-stamp pending, and that this is not evidence against
the current file; otherwise it SHALL report that the proof may be from an earlier version of the file
or may be corrupted and that Cairn cannot tell which, attributing the disagreement to neither
artifact. It SHALL NOT report as established that the proof is corrupted or misfiled, because the
recorded baseline is not the digest the stored proof was made from. Where there is no recorded
baseline, the command SHALL name both possibilities and attribute the disagreement to neither. A proof mismatch SHALL be reported as a failure of the proof,
not of the file. An unreadable proof SHALL be reported as the proof being unreadable, stating that
no conclusion was reached about the file, and SHALL NOT be reported in the words used for a file
that may have changed or a proof that is merely unconfirmed. A transport failure SHALL be reported
as verification being unavailable, with its reason, and SHALL NOT be reported as a pending proof. An inconclusive result SHALL name every possibility it cannot separate,
including the backend's own unreachability. Every one of these SHALL exit non-zero.

Where a transport failure is present on a result whose verdict was decided by something that
outranks it — a verified result or a proof mismatch — the command SHALL still print it, as a
diagnostic line beneath the verdict naming that some attestation lookups failed and that the verdict
rests on the attestations reached; on a proof mismatch that line SHALL qualify the mismatch as
established only over those attestations. The precedence order chooses the command's headline and
its exit status, not the whole of what it is allowed to report, and an operator reading a
categorical verdict over a partly-unreachable proof has been told more than was established. Neither
line SHALL change the exit status the verdict itself sets.

Where none of those reasons is present, the command SHALL distinguish the two not-yet-confirmed
proof states in the same words the panel uses: a proof awaiting Bitcoin confirmation, and a proof
merely queued for stamping. The queued state SHALL NOT be reported with awaiting-confirmation
wording.

#### Scenario: The command line blames neither artifact when the file matches its baseline

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it and whose record indicates no re-stamp is owed
- **THEN** the command SHALL report the stored proof as what does not match, SHALL state that Cairn
  cannot tell whether that proof predates the file's current version or is corrupted, and SHALL NOT
  print its changed-file wording nor state that the proof is corrupted or misfiled

#### Scenario: The command line reports a proof that predates the file while a re-stamp is owed

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it and whose record indicates a re-stamp is owed
- **THEN** the command SHALL report that the proof predates this version of the file with a re-stamp
  pending, SHALL state that this is not evidence against the current file, and SHALL NOT print its
  changed-file wording

#### Scenario: The command line blames neither artifact with no recorded baseline

- **WHEN** `cairn verify` receives a digest disagreement for a file that has no recorded baseline
  digest
- **THEN** the command SHALL name both possibilities and SHALL NOT attribute the disagreement to
  either artifact

#### Scenario: The command line reports an unreadable proof as such

- **WHEN** `cairn verify` receives a result whose proof could not be parsed
- **THEN** the command SHALL report the proof as unreadable and state that no conclusion was reached
  about the file

#### Scenario: A verified result with no parsed block details still reports verified

- **WHEN** `cairn verify` receives a verified result carrying no block height or date
- **THEN** the command SHALL report the verification and SHALL NOT print placeholder values in place
  of the missing details

#### Scenario: A changed file is not reported as pending on the command line

- **WHEN** an operator runs `cairn verify` on a stamped file whose bytes have changed — the live
  bytes hashing to neither the recorded baseline nor the proof's committed digest — and the result
  carries a digest disagreement alongside a not-yet-anchored proof state
- **THEN** the command SHALL report that the file changed and SHALL NOT print its pending wording,
  and SHALL NOT claim the proof was checked

#### Scenario: An unreachable backend is not reported as pending on the command line

- **WHEN** `cairn verify` receives a result carrying a transport failure or an inconclusive outcome
- **THEN** the command SHALL report that verification could not be completed, naming the reason, and
  SHALL NOT print its pending wording

#### Scenario: The command line discloses failed lookups beside a verified result

- **WHEN** `cairn verify` receives a result that is verified and also carries a transport failure,
  because one attestation confirmed while another's lookup failed
- **THEN** the command SHALL report the verified verdict **and** print that attestation lookups
  failed and the verdict is based on the attestations reached

#### Scenario: The command line discloses failed lookups beside a proof mismatch

- **WHEN** `cairn verify` receives a result carrying both a proof mismatch and a transport failure
- **THEN** the command SHALL report the proof-mismatch failure **and** print that attestation
  lookups failed, qualifying the mismatch as based on the attestations reached

#### Scenario: The command line names the queued state as queued

- **WHEN** `cairn verify` receives a result whose proof is queued for stamping, with no mismatch,
  transport failure or inconclusive outcome
- **THEN** the command SHALL report it as queued to stamp and SHALL NOT use its
  awaiting-confirmation wording

