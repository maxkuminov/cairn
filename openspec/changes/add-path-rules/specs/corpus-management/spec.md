## ADDED Requirements

### Requirement: A collection carries path rules

A collection SHALL be able to carry an ordered list of rules, each a path pattern with exactly one
effect: **changes expected** (an ordinary change to a matching file is not flagged) or **comes and
goes** (nor is its disappearance; this effect includes the first). Where more than one rule matches
a path, the first SHALL apply. A collection SHALL hold at most one hundred rules, and SHALL NOT
hold two rules with the same pattern.

A pattern is matched against the file's path relative to the collection root, with `/` as the
separator, case-sensitively, and SHALL be read as follows and in no other way:

- a pattern containing no `/` is a **file name** and matches a file of that name in any folder;
- any other pattern is **anchored at the collection root**;
- a pattern ending in `/` matches **every file beneath that folder**;
- `*` matches any run of characters within one path segment, and `?` one character within a
  segment; `**` matches any number of whole segments, including none; every other character
  matches only itself.

A rule's reach has to be predictable from reading it, because a rule is a standing instruction not
to raise an alarm. The system SHALL therefore refuse to store a pattern that is empty, begins with
`/`, contains an empty, `.` or `..` segment, contains a NUL or a line break, is longer than 512
bytes, or contains **no literal character** — a pattern made only of wildcards and separators
matches every file, and changing what is flagged for a whole collection is what its policy is for.

#### Scenario: A name matches in any folder

- **WHEN** a collection has the rule pattern `NOTES.md`
- **THEN** it SHALL match `NOTES.md`, `a/NOTES.md` and `a/b/NOTES.md`, and SHALL NOT match
  `a/NOTES.md.bak` or `a/MY-NOTES.md`

#### Scenario: A path is anchored at the root

- **WHEN** a collection has the rule pattern `clients/*/NOTES.md`
- **THEN** it SHALL match `clients/acme/NOTES.md`, and SHALL NOT match `clients/NOTES.md`,
  `clients/acme/2025/NOTES.md` or `archive/clients/acme/NOTES.md`

#### Scenario: A trailing slash covers the folder

- **WHEN** a collection has the rule pattern `Agents/Queue/`
- **THEN** it SHALL match `Agents/Queue/a.md` and `Agents/Queue/x/y.md`, and SHALL NOT match
  `Agents/Queue.md`, `Agents/Queue2/a.md` or `Other/Agents/Queue/a.md`

#### Scenario: A double star spans folders

- **WHEN** a collection has the rule pattern `**/cache/**`
- **THEN** it SHALL match `cache/a`, `x/cache/a` and `x/y/cache/b/c`

#### Scenario: Wildcards do not cross a separator

- **WHEN** a collection has the rule pattern `reports/*.csv`
- **THEN** it SHALL match `reports/a.csv` and SHALL NOT match `reports/2025/a.csv`

#### Scenario: Other characters are literal

- **WHEN** a collection has the rule pattern `data[1].txt`
- **THEN** it SHALL match a file named exactly `data[1].txt` and SHALL NOT match `data1.txt`

#### Scenario: A match-all pattern is refused

- **WHEN** a rule is submitted with the pattern `*`, `**`, `**/*` or `*/**`
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: A malformed pattern is refused

- **WHEN** a rule is submitted whose pattern is empty, begins with `/`, contains a `..` segment,
  an empty segment, a NUL or a line break, or exceeds 512 bytes
- **THEN** it SHALL be refused, and no rule SHALL be stored

#### Scenario: The first matching rule applies

- **WHEN** a collection has the rules `drafts/` (comes and goes) followed by `*.md` (changes
  expected), and `drafts/a.md` goes missing
- **THEN** the first rule SHALL apply to it
