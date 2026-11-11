## ADDED Requirements

### Requirement: A rule is created from a flagged file, with its reach shown first

Every file and every folder group listed on a review view SHALL offer a link to say that what
happened to it is normal. The link SHALL open a page that starts from that file or folder and
proposes patterns for it — for a file: files of that name in any folder, everything in its folder,
and that file alone; for a folder: everything in the folder — together with a field for a pattern
of the operator's own, and a choice between the two effects, each described by what it stops
flagging **and what it never does**: that possible corruption, a file that comes back different,
and an unusually large change or deletion in one scan are flagged regardless, and that a matching
file is still watched and notarized. The effect offered first SHALL follow what was flagged: a
change proposes "changes expected", a deletion "comes and goes".

For each proposed pattern the page SHALL state its reach, computed from the collection's records:
how many tracked files it matches, how many open issues it would resolve now, and how many of the
alarms raised in the last ninety days were for files it matches. An operator SHALL NOT have to
create a rule to find out what it does.

Submitting SHALL store the rule and resolve the open issues it covers, after a confirmation that
states the pattern, the effect and how many issues will be resolved. The operator SHALL be
returned to the collection's review view with a single confirmation of what was done. A pattern
the system refuses SHALL be reported on the page, with the reason, and nothing SHALL be stored;
where the refusal is that the pattern matches every file, the page SHALL say that the collection's
policy is the place for that. The page and its submission SHALL be scoped to the current user's
own collections.

#### Scenario: A flagged file offers the link

- **WHEN** a review view lists a changed or missing file
- **THEN** its row SHALL offer a link that opens the rule page for that file

#### Scenario: The rule page proposes patterns for a file

- **WHEN** the rule page is opened for the changed file `clients/acme/NOTES.md`
- **THEN** it SHALL propose the file name in any folder, the folder `clients/acme/`, and the file
  alone, SHALL offer a free pattern field, and SHALL preselect "changes expected"

#### Scenario: A deletion proposes comes-and-goes

- **WHEN** the rule page is opened for a missing file or a folder of missing files
- **THEN** it SHALL preselect "comes and goes"

#### Scenario: Each proposal states its reach

- **WHEN** the rule page renders a proposed pattern
- **THEN** it SHALL state how many tracked files the pattern matches, how many open issues it
  would resolve, and how many alarms of the last ninety days were for matching files

#### Scenario: The effects say what they never cover

- **WHEN** the rule page describes the two effects
- **THEN** it SHALL say that possible corruption, a file that comes back different and an
  unusually large change or deletion are flagged regardless, and that matching files are still
  watched

#### Scenario: Creating a rule resolves what it covers and says so

- **WHEN** the operator submits a changes-expected rule matching two open changed files
- **THEN** the rule SHALL be stored, those two files SHALL be accepted, and the review view SHALL
  state once that the rule was added and two changes were accepted

#### Scenario: A match-all pattern is refused with a pointer to the policy

- **WHEN** the operator submits the pattern `**`
- **THEN** no rule SHALL be stored, and the page SHALL say that a rule cannot cover every file and
  that the collection's policy does

#### Scenario: A rule page is scoped to the owner

- **WHEN** a user opens or submits the rule page of another user's collection
- **THEN** the panel SHALL answer not found

### Requirement: A collection's rules are listed and can be removed

The panel SHALL provide a page listing a collection's rules in the order they apply, each with its
pattern, its effect in words, and how many tracked files it matches now, and SHALL let the
operator remove a rule after a confirmation. The page SHALL offer the same form for adding a rule
directly. It SHALL say that removing a rule changes nothing about files already recorded and that
later changes will be flagged again. A rule that currently matches no tracked file SHALL say so —
a rule that matches nothing is either stale or mistyped. The collection's own page SHALL state how
many rules it has and link to the list. A removal SHALL name the rule by its pattern and effect,
never by its position, so that a list that changed since the page was rendered cannot remove a
different rule.

#### Scenario: Rules are listed with what they match

- **WHEN** the rules page of a collection with two rules is opened
- **THEN** it SHALL list both, each with its pattern, its effect and the number of tracked files it
  matches

#### Scenario: A rule that matches nothing says so

- **WHEN** a listed rule matches no tracked file
- **THEN** the page SHALL say that it currently matches no files

#### Scenario: Removing a rule

- **WHEN** the operator removes a rule and confirms
- **THEN** that rule SHALL no longer be listed, the others SHALL be unchanged, and no file record
  SHALL have changed

#### Scenario: A stale removal removes nothing else

- **WHEN** a removal is submitted for a pattern and effect that are no longer in the list
- **THEN** no rule SHALL be removed

#### Scenario: The collection page states its rules

- **WHEN** a collection with rules is opened
- **THEN** its page SHALL state how many rules it has and link to the rules page
