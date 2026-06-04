## ADDED Requirements

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

#### Scenario: Verify a complete proof

- **WHEN** a complete proof is verified against the matching digest
- **THEN** the result SHALL be verified, naming the Bitcoin block and an "existed by" UTC date

#### Scenario: Digest mismatch fails verification

- **WHEN** a proof is verified against a digest that does not match it
- **THEN** the result SHALL be not-verified

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
