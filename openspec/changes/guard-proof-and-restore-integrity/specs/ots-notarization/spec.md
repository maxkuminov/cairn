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
