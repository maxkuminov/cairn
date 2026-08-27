# ots-notarization Specification (delta)

## MODIFIED Requirements

### Requirement: Verify a proof by digest

The system SHALL verify a stored proof against a file's SHA-256 digest without requiring the
original file to be shipped anywhere. The result SHALL state whether the proof is verified and,
when complete, the Bitcoin block and the "existed by" date.

The result SHALL additionally distinguish a **digest mismatch** — the supplied digest is not the one
the proof commits to, i.e. the file's bytes changed after it was stamped — from every other reason
verification did not succeed. A digest mismatch is the event this product exists to detect, and it
SHALL be reportable as such rather than being flattened into a generic "not verified", which callers
cannot tell apart from a proof that is merely not yet anchored.

The explorer backend parses the proof locally and therefore knows the file digest the proof commits
to; it SHALL set the mismatch indicator at every point it detects one, including a Bitcoin
merkle-root that does not match the attestation's commitment. The node backend shells out to
`ots verify -d`, whose output reports a mismatch as an ordinary non-success exit indistinguishable
from an unanchored proof; it SHALL NOT guess. Reporting a mismatch that was not established would
be a false alarm on the product's core signal, so on that backend the indicator stays unset — an
accepted, documented limitation of the non-default backend.

#### Scenario: Verify a complete proof

- **WHEN** a complete proof is verified against the matching digest
- **THEN** the result SHALL be verified, naming the Bitcoin block and an "existed by" UTC date, and
  the digest-mismatch indicator SHALL be unset

#### Scenario: Digest mismatch fails verification

- **WHEN** a proof is verified against a digest that does not match it
- **THEN** the result SHALL be not-verified

#### Scenario: A digest mismatch is reported as a mismatch

- **WHEN** the explorer backend verifies a proof against a digest the proof does not commit to
- **THEN** the result SHALL be not-verified **and** SHALL carry the digest-mismatch indicator, so a
  caller can tell "these are not the bytes that were stamped" apart from "this proof is not
  confirmed yet"

#### Scenario: A merkle-root mismatch is reported as a mismatch

- **WHEN** the explorer backend finds a Bitcoin attestation whose commitment does not equal the real
  block's merkle root
- **THEN** the result SHALL be not-verified and SHALL carry the digest-mismatch indicator

#### Scenario: The node backend does not guess at a mismatch

- **WHEN** the node backend verifies a proof and `ots verify -d` reports no success
- **THEN** the result SHALL be not-verified with the mismatch indicator unset, because that backend
  cannot distinguish a mismatch from an unanchored proof
