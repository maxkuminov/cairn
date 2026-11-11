## ADDED Requirements

### Requirement: A scan that could not run says why

Where the panel reports that a collection's latest scan failed, it SHALL state the reason that
same run recorded — the run the failure report is about, never an earlier completed scan's
diagnostics — in the operator's terms, next to that report — in particular that the collection's
folder is unreachable, or empty while files are tracked. A bare "failed, check the logs" leaves an
operator who is looking at a perfectly quiet collection with no way to know that it is quiet
because nothing is being read.

The recent-activity summary SHALL name files found with possible corruption as such, distinct from
changed files.

#### Scenario: An unreachable folder is named on the collection

- **WHEN** a collection's latest scan was refused because its root is unreachable
- **THEN** the collection's card and its page SHALL say that the folder is unreachable, and SHALL
  NOT describe the collection's files as missing

#### Scenario: The reason shown belongs to the failed run

- **WHEN** a collection's last completed scan skipped some files, and a later scan was refused for
  an unreachable root
- **THEN** the failure note SHALL state the unreachable-folder reason, and the completed scan's
  skipped-file sample SHALL NOT be presented as the reason for the failure

#### Scenario: Recent activity names possible corruption

- **WHEN** the recent-activity summary covers a day on which a file was found possibly corrupted
- **THEN** that line SHALL say so, and SHALL NOT fold it into the count of changed files

## MODIFIED Requirements

### Requirement: The review view lists issues by folder with the facts needed to decide

The panel SHALL provide a per-collection **review** view that lists every `missing` file and every
`modified` file of the collection and lets the operator resolve them. It SHALL be reachable
directly from the dashboard: a collection card's issue count, and the collection page's
changed-or-missing statistic, SHALL be visible, clickable links to that collection's review view.
All review actions SHALL be scoped to the current user's own collections.

**Files first.** The view SHALL open with a one-line summary of what is listed — how many files
changed, how many are gone, and on which days they were found — followed by the page-wide actions
and then the files. Explanation of what the actions do, and recovery guidance, SHALL come after the
list or be collapsed; the view SHALL NOT place instructions between the heading and the first file.

**Grouped by folder.** Where the listed files lie in more than one folder, the view SHALL group them
by the folders immediately below their deepest common folder, with a separate group for any files
that sit directly in that common folder. Each group SHALL name its folder, state how many of its
files changed and how many are gone, state when they were found (one day, or a range), and offer
one bulk action per kind present, acting on that group only. A large group SHALL be collapsed by
default, SHALL say how many files it holds beyond those it shows, and SHALL offer a link that opens
that group as the view's scope — the same view, restricted to exactly the files the group held and
grouped one level deeper — from which the operator can return to the wider scope. Following that
link SHALL NOT widen the population: the scope it opens for a group of the files directly in a
folder SHALL exclude that folder's subfolders.

A bulk action MAY cover files the view does not show — a group or a scope past its row limit —
because an incident of thousands of files cannot be listed in full. It SHALL NOT do so silently:
wherever an action's population is larger than the rows shown for it, the control's hint and its
confirmation SHALL state how many of the files it acts on are not shown on the page, and the view
SHALL link to where the full set can be listed. A group of which the page lists **no** file at all
SHALL NOT offer its bulk action there: it SHALL name the folder, state its counts, and link to the
folder's own view, where the action is offered beside its rows.

After an accept is performed, the review surface the operator is returned to SHALL confirm it once
— what was accepted and how many files — and SHALL NOT repeat that confirmation on a later visit.
A refused accept SHALL produce no such confirmation. The confirmation SHALL be derived from what
the accept reported, and nothing a client supplies SHALL be able to make it state anything other
than a count of changes or deletions accepted. Where every listed file lies in one
folder the view SHALL render a single list rather than a group of one. Groups SHALL be ordered
newest finding first.

**Facts on the row.** For each file the view SHALL show what happened (changed, possibly
corrupted, gone, or came back different), when it was found, its size, and whether it was notarized — noting for a notarized file
that its proof of prior existence is kept. For a changed file it SHALL also show when the file
itself was last written, what its size was before the change where that differs from its size now,
and, where the file has changed more than once, how many changes are on record and since when: those are the facts that let the operator answer "was that expected?"
without leaving the page. Each row SHALL carry that file's own accept action.

**Recovery guidance SHALL assume no particular backup tool**: a copyable list of the affected file
paths in the view's scope, relative and absolute, and tool-neutral instructions. Where the guidance
tells the operator what to do about files they do not want back, it SHALL name the accept-deletion
action by the label it carries. Recovery guidance SHALL refer to the copied list by the control
that produced it rather than by its position on the page.

**Recovery guidance SHALL describe the check the scan actually performs, and both of its outcomes.**
A restored *missing* file is compared against the digest recorded for it, so the guidance SHALL say
that a rescan returns such a file to a healthy state **only where the restored bytes match what was
recorded**, and that a file which comes back different **closes the now-obsolete missing alert and
replaces it with a new "came back different" issue**. It SHALL NOT tell the operator that restoring
a file and rescanning returns it to a healthy state unconditionally. For a *changed* file the
guidance SHALL say that the issue closes by itself only where the file is put back **exactly as it
was before the change** — the scan keeps that digest and compares against it — and that any other
version is found as a further change. Promising a verification the product does not perform is the
failure class this view exists to close.

A file showing **possible corruption** SHALL be labelled as such, with the visual weight of an
alarm and in words that say what was observed — its contents changed while its size and date did
not — and SHALL NOT be presented as an ordinary change. Where a file carries more than one open
alert, "came back different" takes precedence, then possible corruption.

Where the list of affected files is truncated, the link to the full set SHALL open the file browser
already switched to the list view and already filtered to issues. Where the copyable path list is
truncated, the view SHALL point at that same filtered browser for the full set, and SHALL NOT direct
the operator to a command that is not implemented.

Copy-to-clipboard controls SHALL handle a clipboard write being unavailable or rejected, and SHALL
report the failure rather than silently appearing to succeed. This SHALL hold for **every** copy
control the panel renders, including those on the verification result, and the behaviour SHALL be
implemented once and shared rather than reimplemented per surface.

On a view with nothing to review, the summary, actions and guidance SHALL NOT be rendered: the view
SHALL say that nothing is changed or missing — or, for a collection that has indexed no files, that
nothing has been checked yet, which is not the same thing and SHALL NOT be presented as all clear.

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: The keyboard-activated issue count responds to both activation keys

- **WHEN** the user focuses a dashboard card's issue count and presses Space
- **THEN** the review view SHALL open, as it does for Enter

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with what happened to it, when it was found, its
  size, and whether it was notarized

#### Scenario: A changed file's row says when the file was last written

- **WHEN** the review view lists a modified file
- **THEN** its row SHALL show the file's own last-written time alongside when the change was found

#### Scenario: A file that keeps changing says so

- **WHEN** the review view lists a modified file that has more than one change on record
- **THEN** its row SHALL state how many changes are recorded and since when

#### Scenario: Files in several folders are grouped

- **WHEN** the review view lists missing files that lie in two or more folders below their common
  folder
- **THEN** it SHALL render one group per such folder, each naming the folder, its counts and when
  its files were found, and each offering its own bulk action

#### Scenario: Files directly in the common folder get their own group

- **WHEN** some listed files sit directly in the deepest common folder and others in its subfolders
- **THEN** the files directly in it SHALL form a group of their own whose bulk action leaves the
  subfolders' files alone

#### Scenario: Files in one folder are a single list

- **WHEN** every listed file lies in the same folder
- **THEN** the view SHALL render one list, not a group of one

#### Scenario: A large group is collapsed and can be opened as its own scope

- **WHEN** a group holds more files than the view shows for it
- **THEN** the group SHALL be collapsed by default, SHALL state how many more files it holds, and
  SHALL link to the view scoped to that folder

#### Scenario: Opening a direct-files group does not widen it

- **WHEN** a group of the files directly in a folder is opened as the view's scope, and that
  folder also has issues in a subfolder
- **THEN** the scoped view SHALL list only the files directly in the folder

#### Scenario: An action over files that are not shown says so

- **WHEN** a group or scope holds more issue files than the view renders for it
- **THEN** its bulk action's hint and confirmation SHALL state how many of the files it acts on are
  not shown on the page

#### Scenario: A group the page lists nothing of offers no bulk action there

- **WHEN** a group's files are all beyond the page's row budget, so none of them is listed
- **THEN** the group SHALL render its folder, its counts and a link to its own view, and SHALL NOT
  render a bulk action on this page

#### Scenario: An accept is confirmed once

- **WHEN** the operator accepts files and is returned to a review surface
- **THEN** that page SHALL state what was accepted and how many, and loading the page again SHALL
  NOT show the statement again

#### Scenario: A refused accept is not confirmed

- **WHEN** an accept is refused because its population changed
- **THEN** the page the operator is returned to SHALL show the refusal notice and SHALL NOT show a
  confirmation

#### Scenario: A scoped view acts only inside its scope and offers the way back

- **WHEN** the review view is opened scoped to a folder
- **THEN** it SHALL list, count and offer bulk actions for that folder's issues only, SHALL name
  the folder it is scoped to, and SHALL link back to the collection-wide view

#### Scenario: The summary says when the issues were found

- **WHEN** the listed issues were found by scans on different days
- **THEN** the view's summary SHALL name those days with how many issues each found

#### Scenario: Accept one file from its own row

- **WHEN** the user accepts a single row from the review view
- **THEN** only that file SHALL be acted on, its own events SHALL be closed, and the view SHALL
  refresh to reflect the resolved row

#### Scenario: A bulk action from the review view

- **WHEN** the user triggers a group's bulk action, or a page-wide one
- **THEN** exactly that action's population SHALL be accepted, scoped to the user's own
  collections, and the view SHALL refresh to reflect the resolved issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  is kept

#### Scenario: Recovery guidance names both outcomes of a rescan

- **WHEN** the review view renders its recovery guidance
- **THEN** it SHALL state that a restored missing file returns to a healthy state only where its
  bytes match the digest recorded for it, and that a file restored with different bytes closes its
  obsolete missing alert and raises a new "came back different" issue in its place

#### Scenario: Recovery guidance states the exact condition for a changed file

- **WHEN** the review view renders its recovery guidance
- **THEN** it SHALL say that a changed file put back exactly as it was before the change closes its
  own issue, and SHALL NOT say that restoring any earlier version does

#### Scenario: Possible corruption is labelled on the row

- **WHEN** the review view lists a file whose open alert is a `corrupted` event
- **THEN** its row SHALL say that the contents changed while the size and date did not, styled as
  an alarm, and SHALL NOT read as an ordinary change

#### Scenario: A changed row says what the size was

- **WHEN** the review view lists a modified file whose size before the change differs from its size
  now
- **THEN** its row SHALL state the earlier size

#### Scenario: Recovery guidance names the action for unwanted files

- **WHEN** the recovery guidance tells the operator what to do about files they do not want back
- **THEN** it SHALL name the accept-deletion action by its rendered label, and SHALL NOT name a
  control the view no longer offers

#### Scenario: A truncated copy list points at the filtered browser

- **WHEN** the review view's copyable path list is truncated
- **THEN** the notice SHALL link to the file browser filtered to issues for the full set, and SHALL
  NOT name an unimplemented command

#### Scenario: The truncation link lands filtered

- **WHEN** the affected-file list is truncated and the user follows the link to the full set
- **THEN** the file browser SHALL open in its list view with the issues filter already applied and
  reflected in the filter control

#### Scenario: A failed clipboard write is reported

- **WHEN** a copy-paths action cannot write to the clipboard
- **THEN** the panel SHALL attempt a fallback and, if that also fails, SHALL tell the user the copy
  did not happen rather than showing the success confirmation

#### Scenario: Nothing to review

- **WHEN** the user opens the review view for a collection that has indexed files, none of them
  missing or modified
- **THEN** the view SHALL say that nothing is changed or missing, SHALL NOT render the summary,
  actions or recovery guidance, and SHALL offer no accept action

#### Scenario: Nothing indexed is not all clear

- **WHEN** the user opens the review view for a collection that has indexed no files
- **THEN** the view SHALL say that nothing has been checked yet and SHALL NOT present an all-clear

### Requirement: Policies and collection status are named in plain words

The panel SHALL name a collection's change policy in plain words wherever it shows it — **Locked**
for a collection where nothing should change, **Working** for one where edits are normal — and
SHALL NOT present the stored policy value as the label. Wherever the policy is shown with room for
it, the panel SHALL say what that policy flags, and SHALL say it accurately: under Locked every
change and every deletion is flagged; under Working ordinary edits are not flagged, while
deletions are, and so are a file that comes back different after going missing, possible
corruption, and an unusually large change in one scan. It SHALL NOT describe Working as flagging
deletions only, and SHALL NOT describe any finding as unflagged that the scan does flag.

A collection's status indicator SHALL state the fact rather than a grade: that the collection is
all clear, how many of its files changed, how many are missing, or both. It SHALL NOT label a
collection with a severity word that leaves the operator to guess which kind of problem it has. A
collection that has indexed no files SHALL be labelled as such and SHALL NOT read as all clear.

#### Scenario: The policy is shown in plain words

- **WHEN** a collection page is rendered for a collection stored with either policy
- **THEN** the page SHALL show "Locked" or "Working" and SHALL NOT show the stored value as the
  policy's label

#### Scenario: The Working policy is not described as deletions-only

- **WHEN** the panel describes what the Working policy flags
- **THEN** the description SHALL include a file that comes back different after going missing

#### Scenario: The status states what is wrong

- **WHEN** a collection has two modified files and no missing files
- **THEN** its status indicator SHALL say that two files changed, and SHALL NOT read "Attention"

#### Scenario: Both kinds are stated

- **WHEN** a collection has both missing and modified files
- **THEN** its status indicator SHALL state both counts

#### Scenario: An empty collection is not all clear

- **WHEN** a collection has indexed no files
- **THEN** its status indicator SHALL say so and SHALL NOT read as all clear
