# manifest-import Specification

## Purpose
TBD - created by archiving change add-manifest-import. Update Purpose after archive.
## Requirements
### Requirement: Import a manifest as a pre-existing, unstamped baseline

The system SHALL import a photo-tripwire `manifest.tsv` into a target corpus, creating one `files`
row per entry as a pre-existing baseline. Imported rows SHALL carry the manifest's SHA-256, SHALL
have status `ok` and OTS state `none`, and SHALL NOT generate `added` events. By default the import
SHALL NOT re-hash files (it trusts the manifest). Because imported rows are `ok` with a known hash,
a subsequent scan SHALL recognize them as unchanged and SHALL NOT stamp them, while a file first
seen after the import SHALL be classified `added` and stamped per the corpus OTS mode.

#### Scenario: Imported files are an OK baseline with no events

- **WHEN** a manifest is imported into a corpus
- **THEN** each entry SHALL become a `files` row with status `ok`, OTS state `none`, and the
  manifest SHA-256
- **AND** no `added` events SHALL be created for the import

#### Scenario: Imported files are not stamped, new ones are

- **WHEN** a `perfile` corpus is scanned after a manifest import, and a brand-new file (not in the
  manifest) is present
- **THEN** the imported files SHALL NOT be stamped
- **AND** the brand-new file SHALL be queued and stamped

### Requirement: Tolerant parsing and idempotent re-import

The importer SHALL parse the manifest tolerantly: it SHALL detect the SHA-256 field, treat the
remaining path field as the relative path, read optional size/mtime fields when present, accept
both tab-separated and `sha256sum`-style whitespace lines, and skip (counting) blank, comment, or
malformed lines without failing. Re-importing the same manifest SHALL update existing
`(corpus, relpath)` rows rather than duplicate them.

#### Scenario: Malformed lines are skipped, valid ones imported

- **WHEN** a manifest contains valid entries plus a blank line and a line with no valid SHA-256
- **THEN** the valid entries SHALL be imported and the bad lines SHALL be counted as skipped
  without aborting the import

#### Scenario: Re-import does not duplicate

- **WHEN** the same manifest is imported a second time
- **THEN** the existing rows SHALL be updated in place and no duplicate `files` rows SHALL be
  created

### Requirement: Optional re-hash trust check

The importer SHALL support an opt-in re-hash mode that recomputes each file's SHA-256 from disk and
reports any mismatch with the manifest, without aborting and without changing the no-stamp rule.
Re-hash SHALL be off by default so a large archive is not re-read on a normal import.

#### Scenario: Re-hash reports a tampered file

- **WHEN** the import runs with re-hash enabled and a file's bytes differ from its manifest hash
- **THEN** the importer SHALL report that file as a mismatch and SHALL still complete the import

