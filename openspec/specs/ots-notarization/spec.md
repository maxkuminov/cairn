# ots-notarization Specification

## Purpose
TBD - created by archiving change add-ots-notary. Update Purpose after archive.
## Requirements
### Requirement: Stamp a file's hash into a parallel proof store

The system SHALL stamp a file's SHA-256 to the OpenTimestamps calendars and store the resulting
`.ots` proof in a writable proof store laid out parallel to the corpus, WITHOUT writing anything
under the read-only corpus root. After a successful stamp the file's `ots_state` SHALL be
`incomplete`, with `ots_path`, `ots_stamped_at` and `ots_digest` recorded. Files in a `none`
(tripwire) corpus SHALL never be stamped.

`ots_digest` SHALL record **the digest the proof the system placed at `ots_path` commits to**, and
SHALL be written in the same transaction as `ots_path`/`ots_state`, so a stored proof is never
recorded without its provenance. It is a record of an action the system took, not a description of
whichever file currently occupies that path — that distinction is what lets a later verification
detect a proof that has been corrupted, swapped or misfiled. It SHALL therefore **NOT** be filled in
from an **uncorroborated** later read of the stored proof: a value taken from whatever `.ots` is on
disk would record a swapped proof as the one the system placed and permanently disable that
detection. It SHALL be written only where the file's own recorded baseline digest corroborates it —
at placement, on adoption of an existing proof, or by the corroborated fill the upgrade pass performs
— and no read-side operation (proof verification, proof download, proof export, or a scan) SHALL
write it. It SHALL be cleared whenever `ots_path` is cleared, so no provenance is ever recorded for a
proof that does not exist. A scan that queues a modified file for re-stamping SHALL NOT alter it — the stored proof still
commits to what it commits to.

#### Scenario: A stamp records the digest its proof commits to

- **WHEN** a file in a `perfile` corpus is stamped
- **THEN** `ots_digest` SHALL be recorded as the digest that proof commits to, in the same
  transaction as `ots_path` and `ots_state`

#### Scenario: Queuing a re-stamp does not disturb the stored proof's provenance

- **WHEN** a scan detects that a stamped file's content changed and queues it for re-stamping
- **THEN** `ots_digest` SHALL still describe the proof currently stored at `ots_path`

#### Scenario: Clearing a proof pointer clears its provenance

- **WHEN** a file's `ots_path` is cleared because its proof output path can never be written
- **THEN** `ots_digest` SHALL be cleared with it

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

The upgrade pass SHALL additionally record the provenance of the proofs it already handles, and it
SHALL do so only where the file's own recorded baseline corroborates it. Where a file has **no**
recorded proof provenance and its stored proof can be read, the system SHALL record that proof's
committed digest as the file's proof provenance **if and only if** that digest equals the digest the
system has recorded for the file's content. This is the same corroboration that makes adopting an
existing proof safe: a swapped or misfiled proof cannot be recorded this way, because to be recorded
it would have to commit to exactly the digest already on file for those bytes. The pass is where this
belongs because it already reads every one of these proofs, so the provenance costs no additional
work, and because it is already a writer holding the record — whereas a read-side operation filling
the same field would both record uncorroborated values and write from a read path.

Where the stored proof's committed digest **differs** from the digest recorded for the file's
content, the provenance SHALL be left unrecorded and the discrepancy SHALL be logged as a warning
naming the file, both digests, and the verification the operator should run on that file. That
discrepancy is the corrupted, swapped or misfiled proof this provenance exists to detect; recording
it would destroy the finding, and passing over it silently would hide it. Neither a recorded
provenance nor a logged discrepancy SHALL change whether the proof is upgraded, and a file that
already has recorded provenance SHALL NOT have it rewritten by this pass.

#### Scenario: Confirmed proof becomes complete

- **WHEN** `upgrade` runs against an incomplete proof that Bitcoin has now confirmed
- **THEN** the proof SHALL be rewritten with the Bitcoin attestation and the file's `ots_state`
  SHALL become `complete`

#### Scenario: Unconfirmed proof stays incomplete

- **WHEN** `upgrade` runs against a proof the calendars have not yet anchored
- **THEN** the file SHALL remain `incomplete` and the operation SHALL NOT raise an error

#### Scenario: The upgrade pass records corroborated provenance for a proof that had none

- **WHEN** `upgrade` runs against a file with no recorded proof provenance whose stored proof reads
  and commits to the same digest the system has recorded for that file's content
- **THEN** that digest SHALL be recorded as the file's proof provenance

#### Scenario: The upgrade pass refuses to record uncorroborated provenance

- **WHEN** `upgrade` runs against a file with no recorded proof provenance whose stored proof reads
  and commits to a digest other than the one recorded for that file's content
- **THEN** the file's proof provenance SHALL remain unrecorded, the discrepancy SHALL be logged as a
  warning naming both digests and the verification to run, and the upgrade SHALL still proceed

#### Scenario: The upgrade pass does not rewrite provenance it already has

- **WHEN** `upgrade` runs against a file that already has recorded proof provenance
- **THEN** that recorded provenance SHALL be left unchanged, whatever the stored proof reads as

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
distinction the panel makes, **using the recorded provenance of the stored proof where the file has
it**:

These readings SHALL be evaluated **in the order given**, and the "not the proof recorded for this
file" reading SHALL be reached **before** any reading that calls the stored proof merely old. A proof
that commits to neither the live digest nor the digest the system recorded placing there is not an
older proof of this file at all, and reporting it as one is a false reassurance about the very
artifact the operator is asking about:

- where the file records the digest its stored proof was placed committing to, the digest the stored
  proof actually commits to is known, and the two **differ**, the proof at that path is not the one
  the system recorded placing there: the command SHALL report **as established** that the stored
  proof is not the proof recorded for this file — corrupted, swapped or misfiled — and SHALL NOT
  suggest that the disagreement might be explained by the file having moved on. This SHALL hold
  whether or not the recorded provenance also differs from the live digest;
- where the recorded provenance **equals** the live digest, the same conclusion SHALL be reported as
  established even where the stored proof's own committed digest is unavailable: the system recorded
  placing a proof for exactly these bytes, and the proof at that path disagrees with them;
- where the recorded provenance **differs** from the live digest **and** the stored proof's committed
  digest is known to **equal** that recorded provenance, the command SHALL report **only what that
  comparison established**: that the stored proof commits to the fingerprint previously recorded for
  this file rather than to its current one, that this is not evidence against the current file, and —
  explicitly — that the proof's Bitcoin attestations were **not** validated in this check. It SHALL
  NOT report that artifact as the proof the system placed, nor as still covering or attesting the
  earlier version: a committed digest identifies bytes, not an artifact, so any `.ots` built over the
  same earlier bytes — fabricated, unanchored or substituted — satisfies the comparison identically,
  and verification exits on the digest disagreement before any attestation is checked. It SHALL state
  that a re-stamp is pending only where the file record's **proof state** says one is queued, never
  from the file's status, which stays `modified` indefinitely in a collection whose stamping has since
  been turned off;
- where the recorded provenance differs from the live digest but the stored proof's committed digest
  is **not** available, neither of those findings is established and the command SHALL fall back to
  the wording used where no provenance is recorded. It SHALL NOT report the proof as predating this
  version on the strength of the recorded provenance alone;
- where the file records **no** provenance for its stored proof, the command SHALL fall back to the
  file record's own state: where it indicates a re-stamp is owed — its proof state is
  queued for stamping, or its status is modified or new — it SHALL report that the stored proof
  **predates this version** of the file with a re-stamp pending, and that this is not evidence against
  the current file; otherwise it SHALL report that the proof may be from an earlier version of the file
  or may be corrupted and that Cairn cannot tell which, attributing the disagreement to neither
  artifact, and SHALL NOT report as established that the proof is corrupted or misfiled, because the
  recorded baseline is not the digest the stored proof was made from.

Where there is no recorded
baseline, the command SHALL name both possibilities and attribute the disagreement to neither. In no
case SHALL the command report that the **file's** bytes changed while they still hash to the
recorded baseline. A proof mismatch SHALL be reported as a failure of the proof,
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
  the digest Cairn recorded for it, which records **no** provenance for its stored proof, and whose
  record indicates no re-stamp is owed
- **THEN** the command SHALL report the stored proof as what does not match, SHALL state that Cairn
  cannot tell whether that proof predates the file's current version or is corrupted, and SHALL NOT
  print its changed-file wording nor state that the proof is corrupted or misfiled

#### Scenario: Recorded provenance establishes that the stored proof is not this file's proof

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it, and whose recorded proof provenance equals that same digest
- **THEN** the command SHALL report as established that the stored proof is not the proof recorded
  for this file, SHALL NOT print its changed-file wording, and SHALL NOT offer the
  proof-predates-this-version explanation

#### Scenario: Recorded provenance establishes which fingerprint the stored proof commits to

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it, whose recorded proof provenance differs from that digest, and
  whose stored proof commits to exactly that recorded provenance
- **THEN** the command SHALL report that the stored proof commits to the fingerprint previously
  recorded for this file rather than to its current one, SHALL state that its Bitcoin attestations
  were not validated in this check, SHALL state that this is not evidence against the current file,
  and SHALL NOT describe that artifact as the proof Cairn placed or as still covering the earlier
  version

#### Scenario: An established staleness reading claims no re-stamp that is not queued

- **WHEN** `cairn verify` receives such a result for a file whose record carries a stamping-complete
  proof state and a modified status — a collection whose per-file stamping was turned off after the
  modification
- **THEN** the command SHALL NOT state that a re-stamp is pending or queued, and SHALL claim one only
  where the file record's proof state says one is queued

#### Scenario: A proof matching neither the file nor the record is not reported as merely old

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it, whose recorded proof provenance is a **different, earlier**
  digest, and whose stored proof commits to a **third** digest matching neither
- **THEN** the command SHALL report as established that the stored proof is not the proof recorded
  for this file, and SHALL NOT report that the proof merely predates this version of the file

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

### Requirement: A stamp never destroys an existing proof

Placing a newly produced proof SHALL NOT overwrite, replace or delete a proof already stored at that
path. An OpenTimestamps proof is the only artifact in the system that cannot be recomputed: the file
can be re-hashed and the index can be rebuilt, but a Bitcoin attestation made three years ago cannot
be made again. Replacing one with a proof submitted today silently converts "this existed in 2023"
into "this existed this morning", which is the strongest claim this product makes being quietly
withdrawn while every surface stays green.

Before placing a proof the system SHALL determine whether its output path is already occupied and,
if so, SHALL read the existing proof's committed digest and whether it carries a Bitcoin attestation,
and act as follows:

- existing proof commits to the **same** digest and carries a Bitcoin attestation the caller has
  **established confirms** against the configured verification backend at that moment — the existing
  proof SHALL be kept in place and the newly produced proof SHALL be discarded, and the file MAY be
  recorded against it with a `complete` proof state. A newly produced proof is never Bitcoin-anchored,
  so replacing a confirmed anchored proof for the same bytes is a strict downgrade of the claim;
- existing proof commits to the **same** digest and carries a Bitcoin attestation the caller has
  **established does not confirm** against the configured verification backend — the existing proof
  SHALL be preserved and the new one placed. The inspection performed here reads a local file and
  reaches the network for nothing, so "carries an attestation" is a syntactic fact that anyone able
  to write into the proof store can manufacture. Keeping such a proof would discard a genuine proof
  produced moments earlier in favour of a fabricated one, and would defeat the adoption rule below by
  the simple route of writing the forgery to the canonical path;
- existing proof commits to the **same** digest, carries a Bitcoin attestation, and the caller has
  established **neither** finding about it — because the verification backend could not be reached,
  or because no lookup was made — the placement SHALL be **deferred**, changing nothing: the existing
  proof SHALL be kept at the canonical path, the newly produced proof SHALL be preserved rather than
  discarded, the file SHALL be left queued for stamping, and **no** proof state, provenance or stamp
  time SHALL be recorded for it. A syntactic attestation nobody has confirmed is not evidence the
  system may stand behind, so recording it `complete` would launder an unverified — possibly
  fabricated — artifact into a completed notarization at exactly the moment verification was
  unavailable. Nor may the existing proof be demoted or discarded on the strength of an outage: it may
  well be genuine. Keeping both artifacts and re-attempting on a later pass, when the backend answers,
  is the only outcome that neither asserts nor destroys evidence. A deferral SHALL be logged naming
  the file, the canonical path and the location the newly produced proof was preserved to;
- existing proof commits to the **same** digest and is not yet anchored — the existing proof SHALL
  be preserved and the new one placed. A proof the calendars never anchored may legitimately be
  refreshed, and nothing is lost;
- existing proof commits to a **different** digest — the existing proof SHALL be preserved and the
  new one placed. The old proof is the evidence for the old bytes and SHALL outlive them;
- existing proof **cannot be read** — it SHALL be preserved and the new one placed. An unparseable
  file may still be a valid proof this build cannot interpret.

Preservation SHALL be **to a location that no consumer resolves proofs through**, and the
canonical output path SHALL continue to hold the proof for the file's current digest, so proof
verification, proof download, the command-line verify, proof export and the upgrade pass keep
resolving proofs exactly as they do today, through the file's recorded proof path.

The preserved location SHALL be derived from the preserved proof's own committed digest, and SHALL
NOT incorporate any component of the watched file's name or relative path. Names the system does not
control are what makes a proof path un-writable (see "Notarization tolerates un-writable proof output
paths"); a preservation path that could inherit that failure would make preservation fail exactly
where a proof is most at risk. Preservation SHALL NOT discard, overwrite or replace anything already preserved. Where a proof would
be preserved to a location already holding a preserved proof, the incoming proof SHALL be preserved
under a distinct, monotonically suffixed name in the same location, and both SHALL survive. Two
proofs committing to one digest are NOT interchangeable: one may carry a Bitcoin attestation the
other lacks, and which of them is the stronger evidence is a judgement the preservation store SHALL
NOT make. Keeping the earlier and discarding the later can therefore discard an anchored proof in
favour of an unanchored one, which is the loss this requirement exists to prevent. The suffix SHALL
be derived only from how many proofs are already preserved at that location, so it cannot inherit any
component of a watched file's name. The search for a free name SHALL NOT be bounded by a fixed
ceiling: preservation is the step that exists so that no proof is ever destroyed, and a ceiling
makes a store that reaches it refuse every later preservation permanently — reachable by a prolonged
deferral loop or by a prepopulated store. The system SHALL find the first free name efficiently,
without one failed attempt per name already taken, while the exclusive create remains what decides.

Preservation SHALL claim each name by an **exclusive create** — create-the-name-or-fail-if-it-exists,
then write the proof's bytes, flush them to durable storage, close, **flush the preserved copy's name
to durable storage**, and only then remove the source that was preserved — and a name that already
exists SHALL cause the next suffix to be tried rather than anything being replaced.

The write SHALL cover the **complete** proof before anything is flushed, named or removed. A write
that reports fewer bytes taken than it was given SHALL be continued until the whole proof is written,
the total written SHALL be confirmed to equal the source's size before the durability sequence
begins, and a write that makes no progress SHALL be treated as a failure rather than retried
indefinitely. Otherwise a preserved copy can be a **prefix** of the proof — flushed, durably named,
and followed by the removal of the intact original, destroying the evidence as completely as an
overwrite while reporting success. The source SHALL NOT be removed unless the complete copy and its
whole durability sequence succeeded, and a preservation that fails SHALL NOT leave a partial copy
occupying a preserved name.

Flushing the preserved proof's **bytes** is not sufficient: the directory entry that *names* those
bytes SHALL also be made durable **before** the source is removed. Otherwise an interruption may
persist the removal of the source while the preserved copy's name never lands, and the only copy of
the proof is destroyed — the precise loss this requirement exists to prevent. Preservation SHALL
therefore flush the directory holding the preserved proof and **every directory above it up to and
including the proof store's root**, and SHALL do so **deepest-first — each directory only after the
entry it holds exists** — before removing the source. Directories above the store root SHALL NOT need
flushing: the store root's own name predates every proof.

The chain SHALL be derived from the preserved copy's **path**, and SHALL NOT be narrowed to the
directories a particular attempt created. Those differ: an earlier attempt can create the directories
and then fail before flushing them, leaving directories a later attempt cannot distinguish from
durable ones. A later attempt that flushed only what *it* created would remove the source while an
ancestor's name was never made durable — losing the preserved proof on the next power cut, which is
the loss this requirement exists to prevent. Every successful preservation SHALL therefore flush the
whole chain, whichever attempt created each directory.

The placement that follows SHALL likewise flush the directory holding the canonical name — and every
directory above it up to the store root, in the same order — **before** the placement is reported to its
caller and before any proof path, proof state, provenance or stamp time is recorded for the file. A
name established by a rename is not durable until the directory holding it is, so recording a proof
path first would let the system commit to a path whose entry did not survive, while the preserved
copy sits under a name nothing in the product resolves. Preservation SHALL **NOT** require the proof store's filesystem to
support hard links. The proof store's only stated requirement is that it be writable, and writable
filesystems that reject hard links are ordinary; a preservation method that needed them would turn
every occupied output path on such a store into a permanent retry loop classified as transient, so no
proof for that collection could ever be refreshed — a failure this requirement's own retry rule would
then hide indefinitely.

Writable filesystems that **cannot flush a directory at all** are ordinary — SMB/CIFS shares, FUSE
mounts and FAT-derived volumes accept create, write, rename and a *file* flush while rejecting the
flush of a directory as an unsupported operation. Such a result is **deterministic, not transient**:
it states that the filesystem cannot make directory entries durable, and it SHALL be classified as
unsupported rather than transient. A directory flush that fails specifically because the operation is
unsupported (`EINVAL`, `ENOTSUP` or `EOPNOTSUPP`) SHALL NOT refuse the preservation, SHALL NOT be
retried, and SHALL NOT leave the file queued on that account; treating it as transient would stop the
collection notarizing forever while every retry preserved another copy under a further suffix, growing
the preserved family without ever placing a proof.

On the **first** such result for a proof store, the system SHALL emit one prominent warning naming the
limitation — that the store's filesystem cannot flush directory entries, so a power loss in the
instant between preserving a proof and placing the new one could lose the newest preserved entry's
name, while canonical proofs and all previously flushed entries are unaffected — and SHALL record that
store as **best-effort** for name durability. The condition SHALL be detected on the first directory
flush actually attempted for that store, not by a separate probe or a startup check. Thereafter
directory-flush steps for that store SHALL be skipped without error and SHALL NOT be warned about
again, and preservation and placement SHALL otherwise proceed exactly as specified: the proof's
**bytes** SHALL still be flushed, the source SHALL still be removed last, and the ordering SHALL be
unchanged. This is an **accepted limitation** — on such a store the guarantee weakens to what the
filesystem itself can make durable — and it is preferred to a notary that refuses to stamp, since
refusing loses every future proof to close a crash window measured in milliseconds.

A directory flush that fails for **any other** reason SHALL keep its transient classification exactly
as specified above: it SHALL refuse the placement, SHALL leave the file queued for retry, and the
source that was preserved SHALL NOT have been removed. The unsupported-operation cases SHALL be
enumerated exactly, so that a genuine I/O failure on a filesystem that does support the operation can
never be treated as best-effort.

Preservation SHALL happen **before** the placement, never after, so no interruption between the two
operations can leave the earlier proof destroyed. A failure to preserve SHALL **refuse the
placement** and SHALL be classified **transient**, leaving the file queued for retry: preservation
does not act on the final output path, and the governing rule is that only a failure on that path may
be permanent. A failure to preserve SHALL NOT drop the file out of the stamp queue, and SHALL NOT
prevent the other members of a batch from being stamped.

Each preservation, and each discarded newly-produced proof, SHALL be logged naming both paths.

#### Scenario: A same-digest re-stamp does not replace a confirmed anchored proof

- **WHEN** a file whose canonical proof is Bitcoin-anchored is stamped again for the same digest, and
  the caller has established that the existing proof's anchor confirms against the configured backend
- **THEN** the stored proof SHALL be unchanged byte for byte, the file SHALL be recorded with that
  proof and a `complete` proof state, and the newly produced proof SHALL be discarded

#### Scenario: An unconfirmed anchor defers the placement instead of recording it complete

- **WHEN** a proof is placed over a path holding an `.ots` that commits to the same digest and carries
  a Bitcoin attestation the caller has established neither confirms nor fails to confirm — the
  verification backend could not be reached
- **THEN** the existing proof SHALL remain byte-identical at the canonical path, the newly produced
  proof SHALL be preserved rather than discarded, the file SHALL be left queued for stamping, and no
  proof state, provenance or stamp time SHALL be recorded for it
- **AND** a later pass run while the backend answers SHALL reach a conclusive outcome for that file

#### Scenario: A different-digest stamp keeps both proofs

- **WHEN** a file whose content changed is re-stamped and its canonical proof commits to the earlier
  digest
- **THEN** the canonical path SHALL hold the proof for the new digest, and the earlier proof SHALL
  still exist unmodified in the preserved location

#### Scenario: A disproven anchor does not keep the canonical path

- **WHEN** a proof is placed over a path holding an `.ots` that commits to the same digest and
  carries a Bitcoin attestation which the caller has established does not confirm against the
  configured backend
- **THEN** the existing proof SHALL be preserved and the newly produced proof SHALL be placed at the
  canonical path, and the newly produced proof SHALL NOT be discarded

#### Scenario: Preserving a second proof for one digest keeps both

- **WHEN** a proof committing to a digest is preserved, and later a different proof committing to
  that same digest is preserved
- **THEN** both proofs SHALL exist afterwards, each byte-identical to what was preserved, under
  distinct names, and neither SHALL have been overwritten or discarded

#### Scenario: Preservation succeeds on a store whose filesystem has no hard links

- **WHEN** a proof must be preserved on a writable proof store whose filesystem rejects hard links
- **THEN** the proof SHALL still be preserved byte-identically and the placement SHALL proceed, with
  no failure classified transient and no file left retrying that placement forever

#### Scenario: A store that cannot flush directories warns once and keeps notarizing

- **WHEN** a proof must be preserved and placed on a writable proof store whose filesystem rejects the
  flushing of a directory as an unsupported operation, and a second proof is later preserved and
  placed on that same store
- **THEN** both placements SHALL succeed — each proof preserved byte-identically, each new proof
  placed at its canonical path, the proofs' bytes still flushed and each source still removed only
  after its preserved copy was written — with no failure classified transient and no file left
  retrying
- **AND** exactly one warning SHALL be emitted for that store, naming the durability limitation, with
  none emitted on the second placement
- **AND** no proof SHALL be preserved under a further suffix on account of a retry, so the preserved
  family SHALL NOT grow beyond the two proofs actually preserved

#### Scenario: A directory flush that fails for any other reason still refuses the placement

- **WHEN** the flush of a directory during preservation fails with an I/O error rather than because
  the operation is unsupported
- **THEN** the placement SHALL be refused, the failure SHALL be classified transient with the file
  left queued for retry, and the source that was being preserved SHALL still exist intact at the
  canonical output path

#### Scenario: The accept-restore-rescan cycle does not destroy the original proof

- **WHEN** a stamped file's row is removed by accepting it as missing, the file later reappears at
  the same path, and a subsequent scan treats it as new and stamps it
- **THEN** the proof that was stored for that path before SHALL still exist afterwards, and the
  system SHALL NOT report a stamp time that post-dates it as the file's only notarization

#### Scenario: An unreadable existing proof is preserved, not deleted

- **WHEN** a proof is placed over a path holding an `.ots` that cannot be parsed
- **THEN** that file SHALL be preserved and the new proof placed

#### Scenario: An interruption during preservation cannot leave the proof with no name

- **WHEN** the system is interrupted — by power loss, kill or crash — at any point while an existing
  proof is being preserved, including after the preserved copy's bytes were flushed and after the
  source at the canonical output path was removed
- **THEN** at least one intact, named copy of that proof SHALL exist afterwards: it SHALL NOT be
  possible for the removal of the source to persist while the preserved copy's name does not, because
  the directory holding the preserved copy — and every directory above it up to the proof store's
  root, each flushed only after the entry it holds exists — SHALL have been flushed to durable
  storage before the source was removed
- **AND** where the interruption instead falls after the placement, the directory holding the
  canonical name SHALL have been flushed before the file's proof path was recorded, so no recorded
  proof path can name an entry that did not survive

#### Scenario: A write that reports fewer bytes than given still preserves the whole proof

- **WHEN** a proof is preserved on a store whose writes report fewer bytes taken than they were given
- **THEN** the preserved copy SHALL be byte-identical to the proof, and the source SHALL have been
  removed only after that complete copy was made durable

#### Scenario: A copy that cannot make progress refuses and keeps the source

- **WHEN** the copy of a proof into its preserved name can make no progress
- **THEN** the preservation SHALL fail rather than retry indefinitely, the failure SHALL be
  classified transient with the file left queued, the proof SHALL remain intact at its original
  path, and no partial copy SHALL be left occupying a preserved name

#### Scenario: A retry after a failed attempt still makes the whole chain durable

- **WHEN** a preservation attempt creates the directories leading to a preserved name and then fails,
  and a later attempt succeeds using those same directories
- **THEN** the successful attempt SHALL flush the whole directory chain up to the proof store's root
  before removing the source, so no directory left by the failed attempt is treated as durable
  because it already existed

#### Scenario: Preservation into an already-crowded location still succeeds

- **WHEN** a proof must be preserved to a location that already holds a very large number of
  preserved proofs for that digest
- **THEN** the proof SHALL still be preserved under the next free name, with no fixed bound refusing
  the preservation

#### Scenario: A failure to preserve refuses the placement

- **WHEN** the existing proof at an output path cannot be preserved
- **THEN** the new proof SHALL NOT be placed, the existing proof SHALL remain intact at that path,
  and the file SHALL be left queued for retry rather than dropped out of the stamp queue

#### Scenario: One member's preservation failure does not drop a batch's proofs

- **WHEN** a batch is stamped and one member's existing proof cannot be preserved
- **THEN** every other member's proof SHALL still be placed and recorded, and the batch SHALL
  complete without raising

#### Scenario: Consumers still resolve proofs through the canonical path

- **WHEN** a proof has been superseded and preserved, for a file whose recorded proof path still
  corresponds to its own current relative path
- **THEN** verification, proof download, export and the upgrade pass SHALL all resolve the file's
  proof through its recorded proof path and SHALL find the proof for the file's current digest

#### Scenario: A moved file's superseded proof is reachable only outside the product

- **WHEN** a file was reconciled as moved, so its recorded proof path still names its former
  relative path, and another file has since been stamped onto that path, displacing the moved file's
  proof into the preserved location
- **THEN** verification, proof download, export and the upgrade pass SHALL all continue to resolve
  that recorded proof path — reaching the other file's proof, not the moved file's — and the moved
  file's own proof SHALL be recoverable only from the preserved location by an operator, until the
  recorded proof path is made to follow the file

### Requirement: Proof mutation runs under the collection's single-operation claim

Every operation that can create, replace, preserve, adopt or upgrade a collection's proofs SHALL
first claim that collection's single in-progress operation slot, and SHALL hold it for the whole
sequence that inspects the output path, preserves any proof found there, places the new proof and
records the result against the file. The placement rules above are a read-then-act sequence: without
serialization two writers can both find an output path unoccupied and both write to it, and the
proof written first is destroyed with no trace — the same loss the placement rules exist to prevent,
reached by a different route. Serializing only the final move is insufficient, because the decision
of what to do and the record of what was done both sit outside it.

The claim SHALL be the same collection-scoped mechanism the system already uses to admit one
operation per collection at a time, so that it serializes **across processes** and not only within
one. A guard local to a single process (an in-memory lock or flag) SHALL NOT be relied on: the
command line and the running panel are separate processes over one datastore, and that is exactly
the pairing that races.

This SHALL hold for every production entry point, whichever front door the work arrives through:
scheduled scans, the scheduled upgrade pass, panel-initiated scans and stamp backfills, and the
command-line scan, stamp and upgrade commands. Work invoked from **inside** an operation that
already holds the collection's claim SHALL NOT take a second claim.

A command-line entry point that cannot obtain the claim SHALL **refuse and return**, never wait: it
SHALL report that an operation is already in progress for that collection, SHALL NOT perform any
proof mutation for it, and SHALL NOT report the work as done. Waiting would stall a scheduled
invocation behind an unrelated long-running pass, and the work is idempotent — the next invocation
takes it up. Where a command acts on several collections it SHALL process those it can claim and name
those it skipped; where every collection it was asked to act on was refused it SHALL exit non-zero, so
that a scheduled invocation which did nothing is visibly distinguishable from one that succeeded, and
where it acted on at least one it SHALL exit zero, so that one busy collection does not fail an
otherwise healthy fleet run.

Because the claim is held by a **process**, and a claim held by a live process may outlive the start
of another, no maintenance action SHALL clear a claim merely because a process started. A claim SHALL
be releasable by cleanup only on evidence that the operation holding it is no longer running: the
holder SHALL record liveness as it makes progress, and cleanup SHALL treat a claim as abandoned only
once no progress has been reported for a bounded interval that comfortably exceeds the gap between
two progress reports of the slowest operation. Clearing a claim a live command-line stamp or upgrade
still holds would admit a second writer to the same proofs, which is the loss this requirement
exists to prevent. The cost — that a claim survives a crash until the interval elapses, during which
the collection is skipped — SHALL be accepted, and it SHALL NOT affect scan freshness, which is
derived from completed scan runs and never from a claim.

This exit-status rule SHALL apply to the command-line **scan** exactly as it does to stamp and
upgrade. A scan is the operation an operator schedules to assert that the files were examined; a run
in which every requested collection was refused examined nothing, and reporting success for it lets a
scheduler record an integrity pass that never happened — the same false negative the refusal message
exists to prevent, reached through the exit status instead of the output.

#### Scenario: A second concurrent stamper is refused rather than racing

- **WHEN** one process is stamping a collection's pending files and a second stamp of the same
  collection is started from another process
- **THEN** the second SHALL be refused with a message naming the collection and the operation in
  progress, SHALL NOT place, preserve or adopt any proof, and SHALL NOT wait for the first to finish
- **AND** every proof placed by the first SHALL be intact afterwards

#### Scenario: Startup cleanup does not revoke a live command-line claim

- **WHEN** a command-line stamp or upgrade holds a collection's claim and is still reporting
  progress, and the web application starts
- **THEN** the claim SHALL be left in place, and no scheduled or panel-initiated operation SHALL be
  admitted for that collection while it is held

#### Scenario: A claim whose holder died is released by cleanup

- **WHEN** an operation holding a collection's claim is killed and reports no further progress for
  longer than the abandonment interval
- **THEN** cleanup SHALL release that claim so the collection can be operated on again

#### Scenario: The command-line stamp claims the collection's operation slot

- **WHEN** `cairn stamp` is run against a collection with no operation in progress
- **THEN** it SHALL claim the collection's in-progress operation slot for the duration of the stamp,
  so that a concurrent scan or panel-initiated operation on that collection is refused while it runs

#### Scenario: The command-line upgrade claims each collection it processes

- **WHEN** `cairn upgrade` runs while one collection has an operation in progress and another does
  not
- **THEN** it SHALL upgrade the proofs of the collection it can claim, SHALL skip and name the one it
  cannot, and SHALL NOT upgrade any proof of the skipped collection

#### Scenario: A command-line entry point refused everywhere exits non-zero

- **WHEN** a command-line scan, stamp or upgrade is refused for every collection it was asked to act
  on
- **THEN** it SHALL report the refusal and SHALL exit non-zero

#### Scenario: A fleet invocation that acted on at least one collection exits zero

- **WHEN** a command-line scan, stamp or upgrade is asked to act on several collections, is refused
  for some, and acts on at least one
- **THEN** it SHALL name the collections it skipped and SHALL exit zero

#### Scenario: A scan refused the claim is reported as refused, not as a clean scan

- **WHEN** `cairn scan` is run against a collection that already has an operation in progress
- **THEN** the command SHALL report that the collection was skipped because an operation is in
  progress, and SHALL NOT report it as a completed scan with no findings

### Requirement: An already-anchored proof is adopted instead of re-submitted

The system SHALL adopt an existing proof instead of submitting a duplicate stamp, but only where the
proof has been shown to be evidence the system may stand behind. Adoption both promotes a file out
of the stamp queue and records provenance from a proof the system did not itself place, so a digest
match alone SHALL NOT be sufficient. Where a file queued for stamping has a proof at its canonical
output path, the system SHALL adopt it **if and only if** all of the following hold:

- the proof can be read; **and**
- it commits to the digest recorded for that file's content — so the file's own bytes corroborate
  the provenance about to be recorded; **and**
- the proof's Bitcoin attestation is confirmed against the configured verification backend **at the
  moment of adoption**.

The confirmation SHALL be required of **every** adoption, and the file's own recorded proof
provenance SHALL NOT substitute for it. Recorded provenance names the digest the system placed a
proof *committing to* — a property of the watched file's bytes, not an identity of the proof artifact
on disk. Any number of distinct `.ots` files commit to one digest, and one of them is trivially
produced by anyone able to write into the proof store, so a recorded provenance equal to the digest of
the artifact now at that path establishes nothing about whether it is the artifact the system placed.
Treating it as sufficient would let a fabricated same-digest proof be promoted to `complete` and
recorded as the file's notarization without any chain ever being consulted. The chain is the only
thing that distinguishes a proof the system may stand behind.

Where adoption applies, the proof SHALL be recorded as the file's proof with its state and its
committed digest, and the file SHALL NOT be submitted to the calendars. The common route into a
duplicate stamp is a file that was accepted as missing and then restored unchanged, where the
calendar round-trip buys nothing and the proof it produces would be weaker than the one already
held.

Where any condition fails, the file SHALL NOT be adopted and SHALL be stamped normally, so that the
placement rules above preserve whatever proof is at that path and place the newly produced one.
Specifically:

- a proof that commits to the recorded digest but carries **no confirmed Bitcoin attestation** SHALL
  NOT be adopted. It has no anchor to confirm, and adopting it would freeze a proof the calendars
  never anchored in place of the refresh the incomplete-proof rules require;
- a proof whose attestation **cannot be confirmed** — fabricated, corrupt, or disagreeing with the
  chain — SHALL NOT be adopted. A proof committing to the right digest is trivial to fabricate for
  anyone able to write into the proof store; only the chain distinguishes one the system may adopt.
  This SHALL hold whatever the file's recorded provenance says about it;
- where the verification backend **cannot be reached**, the condition is not satisfied and the file
  SHALL NOT be adopted. Adoption SHALL NOT fall back to accepting the proof's own word or the file's
  recorded provenance, because either would make an unreachable backend sufficient to have a forged
  proof adopted — verification unavailable is precisely when an attacker wants the decision taken. An
  outage SHALL instead leave the file **unchanged and still queued**: the placement rules above defer,
  keeping the existing proof canonical, preserving the newly produced one, and recording nothing. The
  cost of degrading this way is one calendar round-trip and a preserved proof; no proof is lost, no
  state is asserted, and the next pass that reaches the backend settles it.

Adoption SHALL NOT move the file's recorded stamp time forward: no submission was made, so recording
one would assert a notarization that did not happen, and the proof's own attestation carries the real
date. Consequently no adoption SHALL leave a file recorded as submitted-but-unconfirmed with no
recorded stamp time — the only adopted state is confirmed — so an adopted proof can never become
invisible to the stuck-proof report. **No file SHALL be promoted out of the stamp queue to a
`complete` state on the strength of a proof the system did not itself place in that pass unless that
proof's Bitcoin anchor was confirmed against the configured backend during the pass.** Each adoption
SHALL be logged naming the file, the digest, and the block the attestation was confirmed against.

#### Scenario: A restored, unchanged file with an anchored proof is not re-submitted

- **WHEN** a file queued for stamping already has a readable proof at its canonical path committing
  to the digest recorded for that file, whose Bitcoin attestation confirms against the configured
  backend
- **THEN** the system SHALL record that proof as the file's proof with a `complete` state and its
  committed digest as the file's proof provenance, SHALL NOT invoke a stamp submission, and SHALL
  NOT move the file's recorded stamp time forward

#### Scenario: Recorded provenance alone does not qualify a proof for adoption

- **WHEN** a file queued for stamping has a readable proof at its canonical path committing to the
  digest recorded for that file, the file already records that same digest as the provenance of its
  stored proof, and that proof's Bitcoin attestation does not confirm against the configured backend
- **THEN** the system SHALL NOT adopt it and SHALL NOT record it `complete`, SHALL stamp the file
  normally, and SHALL preserve that existing proof rather than replacing it — the recorded provenance
  SHALL NOT be treated as evidence about which artifact is at that path

#### Scenario: An unanchored same-digest proof is not adopted

- **WHEN** a file queued for stamping has a proof at its canonical path that commits to the digest
  recorded for that file but carries no Bitcoin attestation
- **THEN** the system SHALL NOT adopt it, SHALL stamp the file normally, and SHALL preserve that
  existing proof rather than replacing it

#### Scenario: A same-digest proof whose anchor cannot be confirmed is not adopted

- **WHEN** a file queued for stamping has a proof at its canonical path committing to the digest
  recorded for that file, carrying a Bitcoin attestation that does not confirm against the configured
  backend, and the file records no provenance for it
- **THEN** the system SHALL NOT adopt it, SHALL stamp the file normally, and SHALL preserve that
  existing proof

#### Scenario: An unreachable backend leaves the file queued with both proofs intact

- **WHEN** a file queued for stamping has a proof at its canonical path committing to the digest
  recorded for that file and carrying a Bitcoin attestation, and the verification backend cannot be
  reached
- **THEN** the system SHALL NOT adopt it and SHALL NOT record it `complete`, the existing proof SHALL
  remain byte-identical at the canonical path, the proof produced in that pass SHALL be preserved
  rather than discarded, the file SHALL remain queued for stamping with no provenance and no stamp
  time recorded, and the outcome SHALL be the same whether or not the file records provenance for
  that proof

#### Scenario: A proof committing to other bytes is not adopted

- **WHEN** a file queued for stamping has a proof at its canonical path committing to a digest other
  than the one recorded for that file
- **THEN** the system SHALL NOT adopt it, SHALL stamp the file normally, and SHALL preserve that
  existing proof

### Requirement: Proof mutation stops when the claim it runs under has been reclaimed

Every pass that places, preserves, adopts or upgrades proofs SHALL re-confirm the collection's
operation claim against the datastore immediately before each batch it places and each proof it
rewrites, and SHALL stop without mutating anything further if the claim is no longer held. Holding
the claim at the *start* of a pass is not sufficient: a pass that runs longer than the abandonment
interval can have its claim reclaimed while it works, and a reclaimed pass that keeps placing proofs
is precisely the concurrent writer the claim exists to exclude — two processes finding one canonical
proof path unoccupied and each writing to it, with one submission destroyed and no trace that it
existed.

Proofs already placed under the claim while it was valid SHALL be left exactly as they are. They are
evidence that was correctly created at the time it was created, and the placement rules never
destroy a proof; unwinding them to tidy up bookkeeping would discard evidence to resolve a
bookkeeping question. The recorded state of the files whose proofs were placed before the stop SHALL
likewise stand where it was already committed, and nothing SHALL be committed after the claim is
found lost.

#### Scenario: A reclaimed stamp pass places no further proof

- **WHEN** a stamping pass's claim is reclaimed after it has placed one batch of proofs
- **THEN** it SHALL stop before placing the next batch, SHALL leave the proofs already placed intact
  on disk, and SHALL leave the files it did not reach queued for a later pass

#### Scenario: A reclaimed upgrade pass stops rewriting proofs

- **WHEN** the claim under which an upgrade pass is running is reclaimed part-way through
- **THEN** it SHALL stop before rewriting the next proof, and the proofs it already upgraded SHALL
  keep their upgraded state

#### Scenario: A stamp pass that holds its claim throughout is unaffected

- **WHEN** a stamping pass runs to completion holding the collection's claim
- **THEN** every pending file SHALL be stamped and recorded exactly as it was before the
  re-confirmation was introduced

### Requirement: Proof placement for one collection is serialized at the proof store itself

Proof mutation SHALL be serialized at the resource itself: every placement of a stamped proof and
every rewrite of an existing proof SHALL be performed while holding an exclusive advisory lock over
that collection's proof subtree, and the operation claim SHALL be re-confirmed **after** the lock is
held and before anything is mutated.

Re-confirming the operation claim before a placement is otherwise a check followed by an act, and
the two are not one operation: a claim reclaimed in the interval between them leaves the original
pass and the replacement claimant both placing proofs for the same collection, each finding the
canonical path free and each replacing it, with one submission destroyed and no record that it
existed. A pass that finds its claim no longer held at that point SHALL
release the lock and stop without mutating anything, exactly as the earlier re-confirmation does.

The lock SHALL be held per unit of work — one batch of placements, one rewritten proof — and never
for the duration of a pass, so that an operation whose claim has already been reclaimed can delay
the operation that legitimately holds the collection by no more than a single unit. Waiting for the
lock SHALL be bounded, and exhausting that wait SHALL be treated as a transient failure: no proof is
placed, no queued file is dropped from the stamp queue, and the files involved stay queued for a
later pass.

Where the proof store's filesystem does not support advisory locking at all, the system SHALL log
one warning naming that store and SHALL continue with the datastore claim and its re-confirmation
alone, rather than refusing to notarize. Only the filesystem's explicit "locking is not supported"
answers SHALL be treated this way; every other locking failure SHALL remain a transient failure.

#### Scenario: Two racing placers take their turns instead of overwriting each other

- **WHEN** an operation's claim is reclaimed after it has re-confirmed the claim but before it places
  a proof, and the replacement claimant places a proof for the same file
- **THEN** exactly one proof SHALL be canonical at that file's proof path, it SHALL be the one placed
  by the operation that held the claim, and no proof SHALL have been superseded

#### Scenario: The placer whose claim was reclaimed aborts after taking the lock

- **WHEN** a pass takes the collection's placement lock and the claim it runs under has been
  reclaimed in the meantime
- **THEN** it SHALL place no proof, SHALL release the lock, and SHALL report the reclamation the same
  way the pre-placement re-confirmation does

#### Scenario: A second placer holding the lock is waited for, then given up on

- **WHEN** another placer holds the collection's placement lock for longer than the bounded wait
- **THEN** the pass SHALL place nothing, SHALL leave every file it did not place queued for stamping,
  SHALL NOT record any file as unstampable, and SHALL succeed on a later pass once the lock is free

#### Scenario: A proof store that cannot lock keeps notarizing

- **WHEN** the proof store's filesystem answers that advisory locking is unsupported
- **THEN** the system SHALL warn once for that store, SHALL continue placing and recording proofs
  under the datastore claim alone, and SHALL NOT repeat the warning for subsequent placements

