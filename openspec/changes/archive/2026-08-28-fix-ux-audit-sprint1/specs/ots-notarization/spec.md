# ots-notarization Specification (delta)

## MODIFIED Requirements

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

## ADDED Requirements

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
