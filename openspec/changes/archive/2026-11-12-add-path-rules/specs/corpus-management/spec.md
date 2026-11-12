## ADDED Requirements

### Requirement: A collection carries path rules

A collection SHALL be able to carry a list of rules, each a path pattern naming files whose
changes are expected. A collection SHALL hold at most one hundred rules, and SHALL NOT hold two
rules with the same pattern.

A pattern is matched against the file's path relative to the collection root, with `/` as the
separator, case-sensitively and exactly as the path is stored, and SHALL be read as follows and in
no other way:

- a pattern containing no `/` is a **file name** and matches a file of that name in any folder;
- any other pattern is a path **anchored at the collection root** and matched segment by segment;
- a pattern ending in `/` matches **every file beneath that folder**, at any depth, the folder
  itself being anchored at the collection root like any other path pattern;
- within a segment, `*` matches any run of characters and `?` exactly one character, and neither
  matches `/`;
- a segment that is exactly `**` matches any number of whole folders, including none, when it is
  not the last segment; as the **last** segment it matches every file beneath, at any depth, so
  `a/**` and `a/` match the same files;
- every other character matches only itself.

Spaces and tabs around a submitted pattern are not part of it: they SHALL be removed before the
pattern is validated and stored, and everything the system then says about the pattern — what it
matches, what was stored — SHALL be about the pattern without them. No other character SHALL be
removed or altered.

Matching a path against a pattern SHALL take time bounded by a small multiple of the product of
their lengths, whatever the pattern: a rule is evaluated inside a scan, and a pattern that could
stall a match could stall the monitor.

A rule's reach has to be predictable from reading it, because a rule is a standing instruction not
to raise an alarm. The system SHALL therefore refuse to store a pattern that is empty, begins with
`/`, contains an empty, `.` or `..` segment, contains a NUL or a line break, is longer than 512
bytes, uses `**` other than as a whole segment of a path pattern, or contains **no literal
character** — a pattern made only of wildcards and separators matches every file, and changing
what is flagged for a whole collection is what its policy is for.

#### Scenario: A name matches in any folder

- **WHEN** a collection has the rule pattern `NOTES.md`
- **THEN** it SHALL match `NOTES.md`, `a/NOTES.md` and `a/b/NOTES.md`, and SHALL NOT match
  `a/NOTES.md.bak` or `a/MY-NOTES.md`

#### Scenario: A name may use wildcards within the name

- **WHEN** a collection has the rule pattern `*-Internal.xlsx`
- **THEN** it SHALL match `a/Acme-Internal.xlsx` and SHALL NOT match `a/Internal.xlsx` or
  `a/Acme-Internal.xlsx.tmp`

#### Scenario: A path is anchored at the root

- **WHEN** a collection has the rule pattern `clients/*/NOTES.md`
- **THEN** it SHALL match `clients/acme/NOTES.md`, and SHALL NOT match `clients/NOTES.md`,
  `clients/acme/2025/NOTES.md` or `archive/clients/acme/NOTES.md`

#### Scenario: A trailing slash covers the folder, anchored at the root

- **WHEN** a collection has the rule pattern `Agents/Queue/`
- **THEN** it SHALL match `Agents/Queue/a.md` and `Agents/Queue/x/y.md`, and SHALL NOT match
  `Agents/Queue.md`, `Agents/Queue2/a.md` or `Other/Agents/Queue/a.md`

#### Scenario: A single folder name with a trailing slash is a root-level folder

- **WHEN** a collection has the rule pattern `drafts/`
- **THEN** it SHALL match `drafts/a.md` and SHALL NOT match `work/drafts/a.md`

#### Scenario: A double-star segment spans folders

- **WHEN** a collection has the rule pattern `**/cache/**`
- **THEN** it SHALL match `cache/a`, `x/cache/a` and `x/y/cache/b/c`, and SHALL NOT match a file
  named `cache`

#### Scenario: Wildcards do not cross a separator

- **WHEN** a collection has the rule pattern `reports/*.csv`
- **THEN** it SHALL match `reports/a.csv` and SHALL NOT match `reports/2025/a.csv`

#### Scenario: Other characters are literal

- **WHEN** a collection has the rule pattern `data[1].txt`
- **THEN** it SHALL match a file named exactly `data[1].txt` and SHALL NOT match `data1.txt`

#### Scenario: A double star that is not a whole segment is refused

- **WHEN** a rule is submitted with the pattern `a**b`, `**.md` or `docs/**.md`
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: A match-all pattern is refused

- **WHEN** a rule is submitted with the pattern `*`, `**`, `**/*`, `*/**` or `?*`
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: A malformed pattern is refused

- **WHEN** a rule is submitted whose pattern is empty, begins with `/`, contains a `..` segment,
  an empty segment, a NUL or a line break, or exceeds 512 bytes
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: Surrounding spaces are removed and a line break is refused

- **WHEN** a rule is submitted with the pattern ` drafts/ ` (spaces around it)
- **THEN** the stored pattern SHALL be `drafts/`
- **WHEN** a rule is submitted whose pattern ends in a line break
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: A pattern built to backtrack still matches quickly

- **WHEN** a legal pattern made of many repeated wildcard segments is matched against a long path
  it does not match
- **THEN** the match SHALL complete in a fraction of a second

#### Scenario: A duplicate or a hundred-and-first rule is refused

- **WHEN** a rule is submitted whose pattern the collection already has, or the collection
  already holds one hundred rules
- **THEN** it SHALL be refused, and the stored rules SHALL be unchanged
