## ADDED Requirements

### Requirement: Create a corpus with a resolved, existing root

The system SHALL allow creating a corpus owned by a user, given a name and a filesystem root. The
root SHALL be resolved to an absolute real path and SHALL be required to exist and be a directory;
otherwise creation SHALL fail with a clear error. The corpus SHALL persist its mode
(`worm`/`churn`), OTS mode (`none`/`perfile`), scan cadence, and exclude globs.

#### Scenario: Create over an existing directory

- **WHEN** `cairn add-corpus` is run with a name and a path that is an existing directory
- **THEN** a `corpora` row SHALL be created owned by the implicit single user, storing the
  resolved absolute root and the chosen mode/ots-mode/cadence/excludes

#### Scenario: Reject a non-existent root

- **WHEN** `cairn add-corpus` is run with a root path that does not exist or is not a directory
- **THEN** creation SHALL fail with a clear error and no `corpora` row SHALL be created
