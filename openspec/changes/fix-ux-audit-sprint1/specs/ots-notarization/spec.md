# ots-notarization Specification (delta)

## MODIFIED Requirements

### Requirement: Verify a proof by digest

The system SHALL verify a stored proof against a file's SHA-256 digest without requiring the
original file to be shipped anywhere. The result SHALL state whether the proof is verified and,
when complete, the Bitcoin block and the "existed by" date.

The result SHALL additionally distinguish, as separate reportable outcomes rather than one generic
"not verified", each reason verification did not succeed that a caller must describe differently:

- a **digest mismatch** — the supplied digest is not the one the proof commits to, i.e. the file's
  bytes changed after it was stamped. This is the event this product exists to detect;
- a **proof mismatch** — **no** Bitcoin attestation in the proof commits to its real block's merkle
  root, and at least one commits to something else. The file's bytes may be exactly what was
  stamped; what failed is the proof or the block data used to check it, so it SHALL be reported as
  its own outcome and SHALL NOT be reported as a digest mismatch;
- a **transport failure** — the verification backend could not be reached, so nothing about the file
  or the proof was established. The reason SHALL be carried on the result;
- an **inconclusive** outcome — the backend cannot distinguish "not yet anchored" from "the digest
  no longer matches" (or from its own unreachability).

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

The explorer backend parses the proof locally and therefore knows the file digest the proof commits
to and the commitment each attestation makes; it SHALL set the digest-mismatch and proof-mismatch
indicators at the points it establishes them, and SHALL NOT conflate the two. The node backend
shells out to `ots verify -d`, whose output reports a mismatch as an ordinary non-success exit
indistinguishable from an unanchored proof or an unreachable node; it SHALL NOT guess at a mismatch,
because reporting one that was not established is a false alarm on the product's core signal.
Instead it SHALL mark such a result **inconclusive**, so that no caller can present it as a proof
that is merely young. That the node backend cannot separate these cases is an accepted, documented
limitation of the non-default backend.

#### Scenario: Verify a complete proof

- **WHEN** a complete proof is verified against the matching digest
- **THEN** the result SHALL be verified, naming the Bitcoin block and an "existed by" UTC date, and
  the mismatch, transport and inconclusive indicators SHALL all be unset

#### Scenario: Digest mismatch fails verification

- **WHEN** a proof is verified against a digest that does not match it
- **THEN** the result SHALL be not-verified

#### Scenario: A digest mismatch is reported as a digest mismatch

- **WHEN** the explorer backend verifies a proof against a digest the proof does not commit to
- **THEN** the result SHALL be not-verified **and** SHALL carry the digest-mismatch indicator, so a
  caller can tell "these are not the bytes that were stamped" apart from "this proof is not
  confirmed yet"

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

## ADDED Requirements

### Requirement: The command-line verify reports the reason, not the proof's stored state

`cairn verify` SHALL choose what it prints from *why* verification did not succeed, in the same
order of precedence the panel uses, and SHALL NOT reach its "pending" wording while a mismatch,
transport failure or inconclusive outcome is present on the result. The command line and the panel
are two readings of the same integrity claim; a fix applied to one and not the other leaves the
false negative live wherever the operator happens to be looking.

A digest mismatch SHALL be reported as a failure naming that the bytes no longer match what was
stamped. A proof mismatch SHALL be reported as a failure of the proof, not of the file. A transport
failure SHALL be reported as verification being unavailable, with its reason, and SHALL NOT be
reported as a pending proof. An inconclusive result SHALL name every possibility it cannot separate,
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

#### Scenario: A changed file is not reported as pending on the command line

- **WHEN** an operator runs `cairn verify` on a stamped file whose bytes have changed, and the
  result carries a digest mismatch alongside a not-yet-anchored proof state
- **THEN** the command SHALL report the mismatch and SHALL NOT print its pending wording

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
