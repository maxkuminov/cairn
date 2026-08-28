# ots-notarization Specification (delta)

## MODIFIED Requirements

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

- where the file records the digest its stored proof was placed committing to, and that recorded
  digest **equals** the live digest, the stored proof at that path commits to something other than
  what the system recorded placing there: the command SHALL report **as established** that the
  stored proof is not the proof recorded for this file — corrupted, swapped or misfiled — and SHALL
  NOT suggest that the disagreement might be explained by the file having moved on;
- where that recorded digest **differs** from the live digest, the stored proof was made from
  earlier bytes: the command SHALL report **as established** that the proof predates this version of
  the file, and that this is not evidence against the current file. It SHALL state that a re-stamp
  is pending only where the record actually says one is owed;
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

#### Scenario: Recorded provenance establishes that the proof predates this version

- **WHEN** `cairn verify` receives a digest disagreement for a file whose live bytes still hash to
  the digest Cairn recorded for it, and whose recorded proof provenance differs from that digest
- **THEN** the command SHALL report as established that the proof predates this version of the file,
  SHALL state that this is not evidence against the current file, and SHALL claim a re-stamp is
  pending only if the record says one is owed

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


## ADDED Requirements

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

- existing proof commits to the **same** digest and carries a Bitcoin attestation — the existing
  proof SHALL be kept in place and the newly produced proof SHALL be discarded. A newly produced
  proof is never Bitcoin-anchored, so replacing an anchored proof for the same bytes is a strict
  downgrade of the claim;
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
where a proof is most at risk. Where two proofs for the same digest would be preserved to the same
location, the one already preserved SHALL be kept and the later duplicate discarded — both attest the
same fact, and the earlier-preserved one was stamped earlier.

Preservation SHALL happen **before** the placement, never after, so no interruption between the two
operations can leave the earlier proof destroyed. A failure to preserve SHALL **refuse the
placement** and SHALL be classified **transient**, leaving the file queued for retry: preservation
does not act on the final output path, and the governing rule is that only a failure on that path may
be permanent. A failure to preserve SHALL NOT drop the file out of the stamp queue, and SHALL NOT
prevent the other members of a batch from being stamped.

Each preservation, and each discarded newly-produced proof, SHALL be logged naming both paths.

#### Scenario: A same-digest re-stamp does not replace an anchored proof

- **WHEN** a file whose canonical proof is Bitcoin-anchored is stamped again for the same digest
- **THEN** the stored proof SHALL be unchanged byte for byte, the file SHALL be recorded with that
  proof and a `complete` proof state, and the newly produced proof SHALL be discarded

#### Scenario: A different-digest stamp keeps both proofs

- **WHEN** a file whose content changed is re-stamped and its canonical proof commits to the earlier
  digest
- **THEN** the canonical path SHALL hold the proof for the new digest, and the earlier proof SHALL
  still exist unmodified in the preserved location

#### Scenario: The accept-restore-rescan cycle does not destroy the original proof

- **WHEN** a stamped file's row is removed by accepting it as missing, the file later reappears at
  the same path, and a subsequent scan treats it as new and stamps it
- **THEN** the proof that was stored for that path before SHALL still exist afterwards, and the
  system SHALL NOT report a stamp time that post-dates it as the file's only notarization

#### Scenario: An unreadable existing proof is preserved, not deleted

- **WHEN** a proof is placed over a path holding an `.ots` that cannot be parsed
- **THEN** that file SHALL be preserved and the new proof placed

#### Scenario: A failure to preserve refuses the placement

- **WHEN** the existing proof at an output path cannot be preserved
- **THEN** the new proof SHALL NOT be placed, the existing proof SHALL remain intact at that path,
  and the file SHALL be left queued for retry rather than dropped out of the stamp queue

#### Scenario: One member's preservation failure does not drop a batch's proofs

- **WHEN** a batch is stamped and one member's existing proof cannot be preserved
- **THEN** every other member's proof SHALL still be placed and recorded, and the batch SHALL
  complete without raising

#### Scenario: Consumers still resolve proofs through the canonical path

- **WHEN** a proof has been superseded and preserved
- **THEN** verification, proof download, export and the upgrade pass SHALL all resolve the file's
  proof through its recorded proof path and SHALL find the proof for the file's current digest

### Requirement: An already-stamped digest is adopted instead of re-submitted

The system SHALL adopt an existing proof instead of submitting a duplicate stamp: where a file
queued for stamping already has a proof at its canonical output path that commits to the digest
recorded for that file, that proof SHALL be recorded as the file's proof — with its state and its
committed digest — and the file SHALL NOT be submitted to the calendars. The
common route into a duplicate stamp is a file that was accepted as missing and then restored
unchanged, where the calendar round-trip buys nothing and the proof it produces would be weaker than
the one already held.

The digest recorded for the file is what makes this adoption safe: the provenance written is
corroborated by the file's own bytes, never taken on the stored proof's word alone.

#### Scenario: A restored, unchanged file is not re-submitted to the calendars

- **WHEN** a file queued for stamping already has a proof at its canonical path committing to the
  digest recorded for that file
- **THEN** the system SHALL record that proof, its state and its committed digest for the file
  without invoking a stamp submission

#### Scenario: A proof committing to other bytes is not adopted

- **WHEN** a file queued for stamping has a proof at its canonical path committing to a digest other
  than the one recorded for that file
- **THEN** the system SHALL NOT adopt it, SHALL stamp the file normally, and SHALL preserve that
  existing proof
