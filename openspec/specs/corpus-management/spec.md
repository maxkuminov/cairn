# corpus-management Specification

## Purpose
TBD - created by archiving change add-scanner. Update Purpose after archive.
## Requirements
### Requirement: Create a corpus with a resolved, existing root

The system SHALL allow creating a collection owned by a user, given a name and a filesystem root.
The root SHALL be resolved to an absolute real path and SHALL be required to exist and be a
directory; otherwise creation SHALL fail with a clear error. The collection SHALL persist its mode
(`worm`/`churn`), OTS mode (`none`/`perfile`), scan cadence, deep-verify cadence
(`verify_cadence_seconds`, where `0` disables deep verify), exclude globs, and an
`auto_baseline_new` flag (default off) that, when on, lets the deep-verify pass promote intact `new`
files to `ok`. For backward compatibility the `cairn add-collection` command SHALL accept
`add-corpus` as an alias, and the `--collection` option SHALL accept `--corpus` as an alias.

#### Scenario: Create over an existing directory

- **WHEN** `cairn add-collection` is run with a name and a path that is an existing directory
- **THEN** a `collections` row SHALL be created owned by the implicit single user, storing the
  resolved absolute root and the chosen mode/ots-mode/cadence/verify-cadence/excludes, with
  `auto_baseline_new` off unless requested

#### Scenario: Reject a non-existent root

- **WHEN** `cairn add-collection` is run with a root path that does not exist or is not a directory
- **THEN** creation SHALL fail with a clear error and no `collections` row SHALL be created

#### Scenario: Deep-verify cadence defaults to weekly

- **WHEN** a collection is created without an explicit deep-verify cadence
- **THEN** its `verify_cadence_seconds` SHALL default to one week (`604800`)

#### Scenario: Auto-baseline defaults off

- **WHEN** a collection is created without explicitly enabling auto-baseline
- **THEN** its `auto_baseline_new` SHALL be off, preserving the manual-baseline behavior

#### Scenario: The legacy add-corpus / --corpus aliases still work

- **WHEN** `cairn add-corpus` is run, or any command is given `--corpus NAME` instead of
  `--collection NAME`
- **THEN** it SHALL behave identically to the new `add-collection` / `--collection` form

