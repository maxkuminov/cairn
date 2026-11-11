## ADDED Requirements

### Requirement: A rule is created from a changed file, with its reach shown first

Every file listed on a review view as an ordinary change SHALL offer a link to say that changes to
it are expected. The link SHALL open a page that starts from that file and proposes patterns for it —
files of that name in any folder, everything in its folder, and that file alone — together with a
field for a pattern of the operator's own. A missing file's row SHALL NOT offer the link, and neither SHALL the row of a
file flagged for possible corruption or for coming back different: no rule covers any of those, and
a control that suggested otherwise beside such a finding would invite the wrong answer to it.

The page SHALL say what a rule does and what it never does: that ordinary changes to matching
files stop being flagged from the next scan; that deletions, possible corruption, a file that
comes back different and an unusually large change in one scan are flagged regardless; that
matching files are still watched and notarized; and that changes already listed stay listed until
they are accepted.

For each proposed pattern the page SHALL state its reach, computed from the collection's records:
how many of the collection's tracked files it matches, how many currently listed changed files it
matches, and how many change alarms raised in the last ninety days were for files it matches. An
operator SHALL NOT have to create a rule to find out what it does. The figure for past alarms
SHALL count ordinary modification alarms only, SHALL match each by the path recorded with the
alarm where one was recorded and otherwise by the file's present path, and SHALL be worded as
alarms for matching files, not as alarms the rule would have prevented.

Submitting SHALL store the rule and SHALL change no file record and no event. The operator SHALL
be returned to the collection's review view with a single confirmation that the rule was added
and applies from the next scan, stating how many listed changes it matches and that they are still
listed. A pattern the system refuses SHALL be reported on the page, with the reason, and nothing
SHALL be stored; where the refusal is that the pattern matches every file, the page SHALL say that
the collection's policy is the place for that. The starting path arrives in the address and SHALL
be treated as untrusted text: it SHALL only ever be shown escaped and split into proposals. The
page and its submission SHALL be scoped to the current user's own collections.

#### Scenario: A changed file offers the link and a missing file does not

- **WHEN** a review view lists a changed file and a missing file
- **THEN** the changed file's row SHALL offer a link that opens the rule page for it, and the
  missing file's row SHALL NOT

#### Scenario: A finding no rule covers does not offer the link

- **WHEN** a review view lists a file flagged for possible corruption, or one that came back
  different
- **THEN** its row SHALL NOT offer the link

#### Scenario: The rule page proposes patterns for a file

- **WHEN** the rule page is opened for the changed file `clients/acme/NOTES.md`
- **THEN** it SHALL propose the file name in any folder, the folder `clients/acme/`, and the file
  alone, and SHALL offer a free pattern field

#### Scenario: A pattern of the operator's own is checked before it is stored

- **WHEN** the operator types a pattern and asks what it matches
- **THEN** the page SHALL state that pattern's reach, or the reason it is refused, and nothing
  SHALL have been stored

#### Scenario: A rule in a collection whose policy already expects edits says so

- **WHEN** the rule page or the rules page is opened for a collection whose policy does not flag
  ordinary edits
- **THEN** it SHALL say that a rule changes nothing there while that policy stands

#### Scenario: Each proposal states its reach

- **WHEN** the rule page renders a proposed pattern
- **THEN** it SHALL state how many tracked files the pattern matches out of the collection's
  total, how many listed changed files it matches, and how many change alarms of the last ninety
  days were for matching files

#### Scenario: The page says what a rule never covers

- **WHEN** the rule page is rendered
- **THEN** it SHALL say that deletions, possible corruption, a file that comes back different and
  an unusually large change are flagged regardless, that matching files are still watched, and
  that listed changes stay listed until accepted

#### Scenario: Creating a rule resolves nothing and says so

- **WHEN** the operator submits a rule matching two listed changed files
- **THEN** the rule SHALL be stored, both files SHALL still be listed as changed with their accept
  actions, and the review view SHALL state once that the rule was added, that it applies from the
  next scan, and that two listed changes match it

#### Scenario: A match-all pattern is refused with a pointer to the policy

- **WHEN** the operator submits the pattern `**`
- **THEN** no rule SHALL be stored, and the page SHALL say that a rule cannot cover every file and
  that the collection's policy does

#### Scenario: A refused pattern states its reason

- **WHEN** the operator submits a pattern the grammar refuses
- **THEN** no rule SHALL be stored, and the page SHALL state why and keep what was typed

#### Scenario: A hostile starting path is only text

- **WHEN** the rule page is opened with a starting path containing markup, quotes or a path that
  is not a legal relative path
- **THEN** the page SHALL render it only as escaped text or not at all, and SHALL still offer the
  free pattern field

#### Scenario: A rule page is scoped to the owner

- **WHEN** a user opens or submits the rule page of another user's collection
- **THEN** the panel SHALL answer not found

### Requirement: A collection's rules are listed and can be removed

The panel SHALL provide a page listing a collection's rules, each with its pattern and how many
tracked files it matches now, and SHALL let the operator remove a rule after a confirmation. The
page SHALL offer the same form for adding a rule directly. It SHALL say that removing a rule
changes nothing about files already recorded and that later changes to the files it matched follow
the collection's policy. A rule
that currently matches no tracked file SHALL say so — a rule that matches nothing is either stale
or mistyped. The collection's own page SHALL state how many rules it has and link to the list. A
removal SHALL name the rule by its pattern, never by its position, so that a list that changed
since the page was rendered cannot remove a different rule.

#### Scenario: Rules are listed with what they match

- **WHEN** the rules page of a collection with two rules is opened
- **THEN** it SHALL list both, each with its pattern and the number of tracked files it matches

#### Scenario: A rule that matches nothing says so

- **WHEN** a listed rule matches no tracked file
- **THEN** the page SHALL say that it currently matches no files

#### Scenario: Removing a rule

- **WHEN** the operator removes a rule and confirms
- **THEN** that rule SHALL no longer be listed, the others SHALL be unchanged, and no file record
  SHALL have changed

#### Scenario: A stale removal removes nothing else

- **WHEN** a removal is submitted for a pattern that is no longer in the list
- **THEN** no rule SHALL be removed

#### Scenario: The collection page states its rules

- **WHEN** a collection with rules is opened
- **THEN** its page SHALL state how many rules it has and link to the rules page
