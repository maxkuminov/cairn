## ADDED Requirements

### Requirement: Stamp-all control in the corpus view

The corpus view SHALL offer an owner/admin control to stamp all currently-unstamped files in that
corpus (the on-demand backfill). The control SHALL be subject to the same authorization scoping as
the rest of the panel (in `multi` mode a user SHALL only stamp corpora they own; an admin MAY act on
any). After triggering, the panel SHALL report how many files were queued/stamped. The control
SHALL NOT be offered for `none` (tripwire) corpora, which are never stamped.

#### Scenario: Owner triggers stamp-all from the corpus view

- **WHEN** the corpus owner (or an admin) activates the stamp-all control for a `perfile` corpus
- **THEN** every currently-unstamped file in that corpus SHALL be queued and stamped via the batched
  path
- **AND** the panel SHALL report the number of files stamped

#### Scenario: Stamp-all is not offered for tripwire corpora

- **WHEN** the corpus's OTS mode is `none`
- **THEN** the stamp-all control SHALL NOT be shown for that corpus
