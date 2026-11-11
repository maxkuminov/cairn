## ADDED Requirements

### Requirement: The panel has one kind of open item, the issue

The panel SHALL treat a tracked file whose status is missing or modified as an **issue**, and SHALL
present issues as the only thing waiting for the operator. Every count of outstanding work the panel
renders — the sidebar badge, the dashboard's needs-you total, a collection's status, a review view's
totals — SHALL count issues, SHALL be computed by one shared definition, and SHALL therefore agree
wherever two of them are on screen together. The sidebar badge SHALL carry an accessible label
naming what it counts, and SHALL be recomputed on every render so a background scan keeps it
current.

The panel SHALL NOT render a count of unacknowledged events, and SHALL NOT offer any control whose
only effect is to mark an event acknowledged, reviewed or seen. The event log is history: the scan
writes it, and resolving an issue closes the entries that belong to it. Two counters that move
independently, with a verb for each, ask the operator to hold a distinction that exists in the data
model and not in their intent — and the verb that clears the smaller counter leaves every status
indicator where it was. The addresses that used to acknowledge events — one event, all of a user's
events, all of a collection's events — and the address that baselined new files SHALL NOT exist; a
request to any of them SHALL mutate nothing.

An issue SHALL leave the count only by being resolved: accepted from a review view, or cleared by a
scan that finds the file restored. Viewing an issue SHALL change nothing. Because issues are the
only thing the panel counts, a change that was never accepted SHALL remain an issue through
whatever happens to the file next (the scan guarantees this — see the integrity-scanning
capability), so that removing the event count removes no unresolved change from view.

A file that is merely new SHALL NOT be presented as outstanding work: no surface SHALL describe it
as waiting for a baseline, and no surface SHALL offer an action to baseline it. A new file is
watched from the scan that found it; whether it is notarized depends on the collection's
notarization mode and is reported by the file's own proof state, so no surface SHALL describe new
files as notarized on the strength of being new.

#### Scenario: Every count of outstanding work agrees

- **WHEN** a user's collections contain both missing and modified files
- **THEN** the sidebar badge and the dashboard's needs-you total SHALL show the same number, and the
  badge SHALL expose an accessible label naming what the number counts

#### Scenario: No surface offers to acknowledge

- **WHEN** the dashboard, a collection page, a collection's review view and the fleet-wide review
  page are rendered for a user with open issues
- **THEN** none of them SHALL contain a control that marks an event reviewed or acknowledged, and
  none SHALL render a count of unreviewed or unacknowledged events

#### Scenario: The retired addresses mutate nothing

- **WHEN** a request is posted to any of the former acknowledge addresses or to the former
  baseline address
- **THEN** the panel SHALL answer not-found or method-not-allowed, no event SHALL be acknowledged
  and no file record SHALL change

#### Scenario: Resolving an issue is what lowers the count

- **WHEN** the user accepts one changed file from a review view
- **THEN** the sidebar badge on the next rendered page SHALL be one lower, and the collection's
  status SHALL reflect the remaining issues only

#### Scenario: New files are not work

- **WHEN** a collection's only files that are not `ok` are new
- **THEN** the collection SHALL read as all clear, and no page SHALL describe those files as not yet
  baselined or offer an action on them

### Requirement: Issues are resolved by actions named for their outcome

The panel SHALL offer exactly two resolving actions, one per kind of issue, each named for what
happens: **Accept change** for a file whose contents changed, and **Accept deletion** for a file
that is gone. It SHALL NOT offer a single control that acts on both kinds, nor one that also acts
on files the view does not list. Each action SHALL be offered per file, on that file's row, and in
bulk for a folder scope, labelled with **the number of files it acts on**; a bulk action whose
population is empty SHALL NOT be rendered.

Each action SHALL state, in the operator's terms, what is kept and what is lost:

- Accepting a change treats the current contents as correct from now on. Cairn recorded the new
  contents when it detected the change; accepting clears the issue, and this change will not be
  reported again. The hint SHALL NOT say the new contents were notarized: stamping is queued only
  in a notarized collection and may still be pending, and the file's proof state is reported by
  its own badge.
- Accepting a deletion permanently removes the file from Cairn's records — its path, its first-seen
  date, and the link to its timestamp proof. **Nothing on disk is deleted and the proof file stays
  in the proof store**, but Cairn will no longer list the file under Verify. To get it back the
  operator restores it from backup and runs a scan.

Two claims SHALL NOT be made anywhere in this vocabulary, because both are false and both are
plausible enough to be re-derived:

- That accepting a change is **reversible by a rescan**, or leaves some earlier version on record
  to compare against. The scan already recorded the new digest, so a rescan matches it and re-raises
  nothing.
- That accepting a deletion **deletes the file's proof**. No proof file is deleted by any path in
  the product. What is lost is the *link* from Cairn's records to the proof.

Every accept SHALL require a confirmation that names the file, or the number of files, and the
consequence; the confirmation for a deletion SHALL say that the record is removed.

The controls SHALL be distinguishable by consequence without shouting. The control that removes
records SHALL differ from the one that does not in both wording and colour. No page SHALL render a
filled destructive button once per row: a column of several hundred loud buttons trains the
operator to stop reading them, and puts the irreversible control where a tap lands by accident.

Each action's count SHALL be derived from the **same snapshot the page's list is rendered from**, so
the number on the button and the rows on the page cannot disagree.

#### Scenario: Each action names its own population and count

- **WHEN** a review view is opened for a collection with changed and missing files
- **THEN** it SHALL render one bulk action per non-empty kind, each naming what it does and the
  number of files it acts on, and SHALL NOT render a single control that acts on both kinds

#### Scenario: An empty population is not offered an action

- **WHEN** a review view is opened for a collection that has missing files but no modified files
- **THEN** no accept-change action SHALL be rendered

#### Scenario: Button counts agree with the list

- **WHEN** a review view renders its actions
- **THEN** each action's count SHALL come from the same read as the rows it lists

#### Scenario: The forbidden reassurances are not rendered

- **WHEN** any accept action, hint, confirmation or recovery instruction is rendered
- **THEN** it SHALL NOT describe accepting a change as reversible by a rescan, and SHALL NOT state
  that timestamp proofs are deleted

#### Scenario: A single row is resolved on its own

- **WHEN** the operator uses a row's own accept control on a collection with other reviewable rows
- **THEN** only that file SHALL be acted on, and every other row's record and alert SHALL be
  unchanged

#### Scenario: A deletion is confirmed with its consequence

- **WHEN** the operator activates an accept-deletion control, per file or in bulk
- **THEN** the panel SHALL ask for confirmation in words that say the record is removed from Cairn
  and that nothing on disk and no proof file is deleted

#### Scenario: Rows do not carry a filled destructive button

- **WHEN** a review view renders rows for missing files
- **THEN** each row's accept-deletion control SHALL be rendered in the quiet outline style, not as
  a filled destructive button

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
SHALL link to where the full set can be listed. Where every listed file lies in one
folder the view SHALL render a single list rather than a group of one. Groups SHALL be ordered
newest finding first.

**Facts on the row.** For each file the view SHALL show what happened (changed, gone, or came back
different), when it was found, its size, and whether it was notarized — noting for a notarized file
that its proof of prior existence is kept. For a changed file it SHALL also show when the file
itself was last written, and, where the file has changed more than once, how many changes are on
record and since when: those are the facts that let the operator answer "was that expected?"
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
guidance SHALL NOT promise that restoring the earlier version clears the issue: the scan compares
against the newest recorded contents, so a restored earlier version is found as a further change,
which the operator then accepts. Promising a verification the product does not perform is the
failure class this view exists to close.

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

#### Scenario: Recovery guidance does not promise that reverting a change clears it

- **WHEN** the review view renders its recovery guidance
- **THEN** it SHALL NOT state that restoring the earlier version of a changed file returns it to a
  healthy state

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

### Requirement: Fleet-wide review groups every collection's issues

The panel SHALL serve a fleet-wide review page listing every missing or modified file across all of
the collections the current user owns, in one section per collection, so that a fleet-wide issue
count has a destination that can act on it. Collections with no missing or modified files SHALL NOT
be listed.

Each section SHALL name its collection and its change policy, state **both** that collection's
missing and modified counts — including a count that is zero, since an unstated count cannot be
told apart from an absent one — and offer a link to that collection's own review view. Within a
section the issues SHALL be grouped by folder exactly as on the collection's own review view, with
the same rows, the same per-file actions and the same per-group bulk actions.

Sections SHALL be ordered by what most deserves the operator's attention, not by size: collections
under the **Locked** policy before collections under the **Working** policy, then the collection
with the most recently found issue first, then by name. A handful of changes in a collection where
nothing should change SHALL NOT be listed beneath hundreds of routine deletions elsewhere.

Every number a section shows and every action a row or group authorises SHALL be derived from
**one read** of that collection's review population — the same single-snapshot property the
per-collection review view depends on.

The page SHALL NOT offer any **collection-spanning action**. Every accept is authorised over a
single collection's population and is irreversible, so each control on this page SHALL act within
one collection — one file, or one folder of one collection — and no control SHALL accept across
collections.

An accept submitted from this page SHALL return the operator to this page, on success and on
refusal alike. The return destination SHALL be chosen from a fixed set and SHALL NOT be taken from
the submission.

The page SHALL bound the rows it renders, both per collection and in total, so that one collection
with a very large issue set cannot crowd the others out. Where a section's rows are truncated the
page SHALL say how many more that collection holds and link to its review view; a collection over
the total budget SHALL still be listed with its counts and its link rather than omitted, and a
section rendered with **no** rows because the budget was already spent SHALL say that is what
happened.

The page SHALL be scoped to the current user's own collections, and SHALL render a distinct empty
state when the user has collections but no open issues, separate from the state where the user has
no collections at all.

#### Scenario: Issues in several collections are listed together

- **WHEN** a user with missing files in one collection and modified files in another opens the
  fleet-wide review page
- **THEN** the page SHALL render one section per affected collection, each naming its collection
  and stating both its missing and its modified count

#### Scenario: Locked collections lead

- **WHEN** one Working collection has several hundred missing files and one Locked collection has a
  single modified file
- **THEN** the Locked collection's section SHALL be rendered first

#### Scenario: Within a policy the newest finding leads

- **WHEN** two collections under the same policy both have issues
- **THEN** the collection whose newest issue was found most recently SHALL be rendered first

#### Scenario: A row acts on one file without leaving the page's snapshot

- **WHEN** the user accepts a single file from a fleet-wide review row
- **THEN** the accept SHALL be authorised by the fingerprint minted from the same population read
  the row was rendered from, and SHALL act on that one file only

#### Scenario: A group action acts on one collection's folder

- **WHEN** the user submits a group's bulk action from the fleet-wide page
- **THEN** only that collection's files in that folder SHALL be acted on, and the operator SHALL be
  returned to the fleet-wide page

#### Scenario: A stale fleet-wide submission is refused and returns to the fleet page

- **WHEN** the user submits an accept from the fleet-wide page whose population has since changed
- **THEN** the request SHALL be refused without mutating anything, and the operator SHALL be
  returned to the fleet-wide page with the changed-since-loaded notice rather than to a different
  page

#### Scenario: The return destination is chosen from a fixed set, never supplied

- **WHEN** an accept is submitted carrying a return-destination value that is not the fleet-page
  literal — including an absolute URL to another host, a scheme-relative or protocol-bearing
  string, or any other arbitrary text
- **THEN** the request SHALL still perform exactly the same scoped, fingerprint-guarded accept, and
  SHALL redirect to the collection's own review view; no supplied value SHALL ever become the
  redirect's host or path

#### Scenario: No collection-spanning action is offered

- **WHEN** the fleet-wide review page renders any number of affected collections
- **THEN** it SHALL offer no control that accepts across more than one collection

#### Scenario: One very large collection does not hide the others

- **WHEN** one collection holds more issues than the per-collection render budget while other
  collections also have issues
- **THEN** that section SHALL render its budgeted rows plus a count of how many more it holds and a
  link to its review view, and every other affected collection SHALL still be listed

#### Scenario: The page is scoped to the viewer's collections

- **WHEN** a user opens the fleet-wide review page in multi-user mode
- **THEN** only that user's own collections SHALL be listed, and no other user's files SHALL appear

#### Scenario: No open issues

- **WHEN** a user with collections has no missing or modified files anywhere
- **THEN** the page SHALL render an empty state saying so, distinct from the state shown to a user
  with no collections

### Requirement: The dashboard leads with what needs the operator

The dashboard SHALL open with a **needs-you** section that answers, before anything else, whether
any file is changed or missing. Where issues exist it SHALL list every affected collection — in
the fleet-wide review page's order — each with its changed and missing counts, when its newest
issue was found, the folders holding most of its issues, and a link to that collection's review
view; where more than one collection is affected it SHALL also link to the fleet-wide review page.
The section's total SHALL be the same number the sidebar badge shows. Where no file is changed or
missing the section SHALL be a single line saying so, and it SHALL NOT say so for a user whose
collections have indexed no files at all: nothing checked is not the same as nothing wrong.

Below it the dashboard SHALL show summary tiles for the files monitored, the proofs anchored and
the last activity, a card for each collection, and a summary of recent activity.

The **recent activity** summary SHALL be read-only. It SHALL aggregate the event log by day and by
collection, most recent first, stating for each line how many files were added, changed, went
missing, were restored, moved, or came back different. It SHALL carry no control, and it SHALL
include informational activity, so that a healthy system does not render an empty summary.

The **last activity** tile summarises the newest finished run of **any** kind across the user's
collections. It SHALL therefore name the operation it is describing — the collection and the kind
of run (`scan`, `stamp`, `upgrade`) — and SHALL state that run's result whenever the result is
anything other than a clean completion. It SHALL NOT describe every finished run as a scan: a
`stamp` or `upgrade` run says nothing about when the files were last checked, and presenting one as
a scan is false assurance. A `partial` run SHALL be named partial, a run that ended in `error`
SHALL be named a failure, and an `interrupted` run SHALL be named neutrally (it is the routine
record of a reclaimed claim, never an alarm).

Status indicators SHALL use visually distinct icons for a collection with changed files and a
collection with missing files.

#### Scenario: The needs-you section lists every affected collection

- **WHEN** two or more collections have missing or modified files
- **THEN** the needs-you section SHALL list each of them with its counts and a link to its review
  view, and SHALL link to the fleet-wide review page

#### Scenario: One affected collection

- **WHEN** exactly one collection has missing or modified files
- **THEN** the needs-you section SHALL list it with a link to its review view

#### Scenario: Nothing needs the operator

- **WHEN** no collection has missing or modified files and at least one file is indexed
- **THEN** the needs-you section SHALL be a single line saying no files are changed or missing,
  with no link

#### Scenario: Nothing indexed is not all clear

- **WHEN** the user's collections have indexed no files
- **THEN** the needs-you section SHALL NOT say that nothing is changed or missing

#### Scenario: Recent activity is a read-only summary by day and collection

- **WHEN** scans over the last days added files to one collection and found changes in another
- **THEN** the recent-activity summary SHALL show one line per day and collection stating those
  counts, and SHALL contain no button or form

#### Scenario: A healthy system's activity is not empty

- **WHEN** the only recent events are informational ones — files added, restored or moved
- **THEN** the recent-activity summary SHALL still list them, and SHALL NOT render an empty state

#### Scenario: The last-activity tile names a non-scan run for what it is

- **WHEN** the newest finished run across the user's collections is a `stamp` or an `upgrade` run
- **THEN** the last-activity tile SHALL name that operation's kind and SHALL NOT describe it as a
  scan

#### Scenario: The last-activity tile names a partial or failed run

- **WHEN** the newest finished run ended `partial` or `error`
- **THEN** the tile SHALL state that result alongside the collection and the kind, rather than
  presenting the run as a clean completion

#### Scenario: The last-activity tile treats an interrupted run neutrally

- **WHEN** the newest finished run ended `interrupted`
- **THEN** the tile SHALL name it neutrally and SHALL NOT present it as a failure

### Requirement: Policies and collection status are named in plain words

The panel SHALL name a collection's change policy in plain words wherever it shows it — **Locked**
for a collection where nothing should change, **Working** for one where edits are normal — and
SHALL NOT present the stored policy value as the label. Wherever the policy is shown with room for
it, the panel SHALL say what that policy flags, and SHALL say it accurately: under Locked every
change and every deletion is flagged; under Working ordinary edits are not flagged, while
deletions are, and so is a file that comes back different after going missing. It SHALL NOT
describe Working as flagging deletions only.

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

## MODIFIED Requirements

### Requirement: Accept and scan actions are available from the panel

The collection detail page SHALL offer "Scan now". It SHALL NOT offer any action that accepts a
change or a deletion: those are irreversible, and they SHALL be reachable only from a review view,
which lists the files they act on. While the collection has missing or modified files the detail
page's primary action SHALL be a link to its review view. An accept action MAY complete as an
ordinary form submission followed by a redirect to a freshly rendered page rather than an in-place
refresh.

Choosing what to render is not sufficient, because the collection can change between the page being
rendered and the form being submitted. **Every endpoint that accepts a changed file or removes a
file record SHALL be bound to the population its form was rendered for** — each of the review
view's bulk actions, at whatever folder scope it was rendered for, and the per-file action alike.
None is exempt: a rendered list is a statement about the past, so a scan that records another
missing file after the render makes "these are the files this button will accept" false, and the
operator removes a record they never saw.

Each such form SHALL carry a **fingerprint of the population it claims to act on**, and the endpoint
SHALL recompute that fingerprint from the datastore and refuse unless it still matches.

The fingerprint SHALL identify each file by a **durable identity, not by a row identifier alone**:
it SHALL cover the file's path and its status, its content digest wherever one is recorded, and the
time the record itself was created, in addition to its row identifier. A row identifier can be
reused by the datastore after the row it named is deleted, so a fingerprint built from identifiers
and statuses alone can match a population that shares no file with the one the form was rendered for
— precisely the replay this guard exists to prevent. The record's creation time is what separates
one **generation** of a record from the next: a file record removed by an accept and later
re-created at the same path with the same content — even reusing the freed identifier — is a
different record that the operator has never been shown, and it SHALL NOT validate a form minted for
its predecessor. That creation time SHALL be set when the record is inserted and SHALL NOT be
rewritten while the record survives. For the same reason the fingerprint SHALL be scoped by the
collection's creation time as well as its identifier, so that a recreated collection reusing an
identifier cannot validate a fingerprint issued for its predecessor. The fingerprint SHALL also be
scoped so that a fingerprint issued for one form cannot validate another, and its encoding SHALL be
unambiguous for every legal file path, so that two different populations cannot be encoded to the
same input.

Because the accept verb is **scoped** — a distinct action per file state, plus a per-file action —
that form-scoping is what keeps the actions apart: each form SHALL name the action it was minted
for, so a fingerprint minted for "accept the changed files" can never validate a submission that
would remove the missing ones, and a fingerprint minted for one row can never validate a submission
at another row's address. A bulk action's form SHALL additionally name the **folder scope** it was
rendered for — the directory, and whether the scope is that directory's whole subtree or only the
files directly in it — so a fingerprint minted for one folder can never validate a submission
naming another, including where the two scopes happen to hold the same files. The scope travels
with the submission, so it SHALL be treated as untrusted input: it selects nothing by itself, and a
submission whose scope does not reproduce the submitted fingerprint SHALL be refused like any other
mismatch.

The fingerprint SHALL cover the **whole population its own action acts on**, rather than any
truncated list the page displayed and rather than the whole of what the page lists: for each of the
review view's bulk actions, every file in that action's state within its folder scope; for the
per-file action, that single record. An absent or empty fingerprint SHALL be treated as a mismatch,
so the check fails closed.

The population a form's fingerprint is computed over and the population the page **renders** SHALL
be derived from a **single, consistent read** — one snapshot covering the file records, the open-event
set and any count the form asserts — rather than from separate reads that a concurrent writer can
interleave. A page that reads its visible rows in one query and computes the fingerprint in another
can publish a fingerprint for a population it never displayed, and an unchanged submission of that
form would then validate and destroy a record the operator was never shown — the exact accident the
guard exists to prevent, reintroduced inside the guard's own mint. **Where one page renders several
actions, all of their fingerprints — and every per-row fingerprint — SHALL be derived from that one
snapshot**, so no two controls on the same page can describe different states. The same single-read
derivation SHALL be used when the endpoint recomputes the fingerprint, so the two sides cannot encode
the same state differently. Any other claim the same page makes **about that population's
existence** — in particular the review view's "this collection indexes no files" state — SHALL be
derived from that same snapshot rather than from an earlier count, so that a concurrent accept
committed between the two reads cannot leave the page reporting a healthy "all clear" for a
collection it has just emptied.

Any **count the action itself states** — a button label, and in particular a confirmation that names
how many files the submit will change — SHALL be derived from that same snapshot, never from an
earlier count query. This applies to every folder group's action as much as to the page-wide ones:
a page that counts a group's files in one statement and mints the fingerprint in another lets a
file committed between them be hashed without being counted, so the operator confirms a number
smaller than the set the form authorizes. The number displayed, the population hashed and the
population eventually mutated SHALL be the same one. Counts the page uses for **pure display**
elsewhere — status tiles, totals, the file browser, a row's change history — are not claims the
action makes and MAY come from separate reads.

The fingerprint SHALL additionally cover a set of open events **by identity** — each
event's identifier, its kind and the time it was detected — recomputed inside the same guarded
transaction as the file records. Covering that set by **count** is not sufficient: an open alert may
be acknowledged and a different one opened with the same kind, returning the count to what it was
while the incident the operator is being asked to clear, or being told does not exist, is a
different one. Event identifiers may themselves be reused after a record is deleted, so identity
alone is not sufficient either — hence the detection time, which separates one generation of an
alert from the next.

**Which** events that set contains SHALL follow the action's own population: the open events
**belonging to the files in that population** — the alerts the action itself will close — so that
any change to *which* of them the action would close is a refusal. That term SHALL follow the
action's scope rather than the collection: an action closes only the events of the files it
touches, so binding it to every open event on the collection would refuse submissions over drift
the action cannot reach — an alert opening on a file in a different state, or in a different
folder, would refuse an action whose own population never moved — and refusals the operator cannot
account for are how a guard gets worked around. That set is an assertion about a population's
alerts as it stood when the form was rendered, and SHALL be recomputed from the endpoint's own read
rather than trusted from the submission.

Each action SHALL act on **only the population its own label names**, and the records it mutates
SHALL be **exactly the records its recomputed fingerprint covered** — selected by that
recomputation, never by a second, differently expressed selection of the same folder and state. Two
encodings of one rule on a destructive path are two chances to disagree. No action offered on a
review view touches a file that is merely new, so a file appearing there between render and submit
is neither hashed nor acted on.

The recomputation and the accept SHALL happen **within a single write transaction**, entered
before the recomputation reads anything it depends on, so that no concurrent scan can commit between
the check and the mutation. A check that is merely "recount, then call the service" is not a guard:
the recount and the deletion are separate statements, and a scan can claim, run and commit in the
gap. An in-flight-operation check SHALL also be made, but as a secondary precaution covering the
long window, not as the mechanism relied on for the short one.

Entering that write transaction can itself fail under contention — another writer holding the lock
past the connection's busy timeout, or having committed since this request's read snapshot. Such a
failure SHALL be handled as a **refusal**, identical to a fingerprint mismatch: nothing mutated, the
transaction rolled back, the same redirect and marker. It SHALL NOT surface as a server error, which
would leave the operator with a failed destructive POST, no account of why, and every reason to
retry it blind. Database failures that are **not** lock contention SHALL NOT be converted into a
refusal — reporting a corrupt or misconfigured datastore as "the collection changed since the page
loaded" hides a fault the operator must act on.

On refusal the endpoint SHALL mutate nothing — no file record removed, no change accepted, no
event acknowledged — and SHALL redirect to the review view **carrying a marker that the view
renders as an explanation**. That explanation SHALL lead with the consequence — the operator's
action was **not** applied and nothing was removed or accepted — before it describes the
collection, and only then state that the collection changed since the page loaded. A refusal with no
visible explanation is indistinguishable from a broken button and invites the operator to click it
again; one that opens by describing the collection reads as a status note and leaves the operator
believing the destructive action went through.

Where the refusal lands on a view with **nothing left to review**, the explanation SHALL still lead
with the action not having been applied, and SHALL account for the empty view by saying the state
may already have been resolved. It SHALL NOT tell the operator that the list shown is current when
there is no list, which reads as if their action had emptied it.

"Scan now" SHALL run the scan **asynchronously** — it SHALL start the scan in the background and
return immediately rather than blocking the request until the scan completes, so the panel can show
live operation status. A scan SHALL NOT be started for a collection that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: The destructive action is not offered beside the issues it would erase

- **WHEN** the user opens a collection that has missing or modified files
- **THEN** the page header SHALL offer a review link as its primary action and SHALL NOT contain an
  accept form

#### Scenario: The detail page offers no accept action in any state

- **WHEN** the user opens a collection with no missing or modified files but with files that are
  new
- **THEN** the page SHALL offer no accept or baseline action of any kind


#### Scenario: A scoped action is refused after its own population goes stale

- **WHEN** the user opens the review view listing one missing file, a scan then records a second
  file missing, and the user submits the already-rendered accept-deletions form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove either missing file's record or acknowledge
  either event, and SHALL redirect to the review view with the marker that makes the refusal visible

#### Scenario: Accepting deletions is not refused by drift it cannot reach

- **WHEN** the user opens the review view, a scan then records a further file **modified** and opens
  its alert, and the user submits the already-rendered accept-deletions form whose own missing
  population is unchanged
- **THEN** the endpoint SHALL perform the removal, and SHALL NOT acknowledge the newly opened
  modified alert

#### Scenario: Accepting changes is not refused by drift it cannot reach

- **WHEN** the user opens the review view, a scan then records a further file **missing** and opens
  its alert, and the user submits the already-rendered accept-changes form whose own modified population is
  unchanged
- **THEN** the endpoint SHALL accept the changed files, and SHALL NOT acknowledge the newly opened
  missing alert or remove the newly missing record

#### Scenario: A per-file action is not refused by drift on another row

- **WHEN** the user opens the review view, an alert is then opened on a **different** file — and
  that file's own state changes — and the user submits the already-rendered per-file action for a row
  that has not moved
- **THEN** the endpoint SHALL accept that one row, and SHALL neither acknowledge the other file's
  alert nor touch its record

#### Scenario: No accept form validates any action but its own

- **WHEN** the fingerprint minted for any one of the three accept actions — accept changes, accept
  deletions, per-file — is submitted to either of the other two endpoints, for every such ordered
  pair
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing, in every pair, including where the
  two actions' populations happen to be identical


#### Scenario: A per-file form does not validate a submission at another row

- **WHEN** the fingerprint minted for one row's per-file action is submitted at a different row's
  per-file address, including where the two rows share a status, a content digest and a size
- **THEN** the endpoint SHALL refuse, SHALL mutate neither row, and SHALL redirect to the review
  view with the staleness marker

#### Scenario: A refused submission is explained on the review view

- **WHEN** the review view is opened with the staleness marker a refused accept redirects to
- **THEN** the view SHALL render a dismissable notice that states first that the action was not
  applied and that nothing was removed or accepted, then that the collection changed since the
  page loaded and that the list shown is current, and SHALL NOT render that notice on an ordinary
  visit or for an unrecognized marker value

#### Scenario: A refusal landing on an all-clear view still says the action was not applied

- **WHEN** the review view is opened with the staleness marker and the collection has no missing or
  modified files left to review
- **THEN** the notice SHALL still state that the action was not applied and nothing was removed or
  accepted, SHALL say the state may already have been resolved, and SHALL NOT claim that a list
  shown below is current

#### Scenario: A submission with no fingerprint is refused

- **WHEN** an accept-family action is submitted without the population fingerprint, or with an empty
  one
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing

#### Scenario: A reused row identifier does not validate a stale fingerprint

- **WHEN** the review view is rendered for a collection, the missing file it listed is then removed
  from the datastore and a scan records a **different** file, at a different path, whose row reuses
  the removed record's identifier and which is itself missing, and the operator submits the
  originally rendered form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove the replacement file's record or acknowledge
  its event, and SHALL redirect to the review view with the staleness marker — a reused identifier
  SHALL NOT be sufficient for a fingerprint to still match

#### Scenario: Replacing one open alert with another does not validate a stale accept-deletions form

- **WHEN** the review view is rendered for a collection with one missing file and one open alert on
  it, that alert is then acknowledged and a **new** alert of the same kind is opened on the same
  file — so the count of open alerts returns to exactly what it was while the file records never
  move — and the operator submits the already-rendered accept-deletions form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  remove the file's record

#### Scenario: Replacing one open alert with another does not validate a stale accept-changes form

- **WHEN** the review view is rendered for a collection with one modified file and one open alert on
  it, that alert is then acknowledged and a **new** alert of the same kind is opened on the same
  file, so the count of open alerts returns to what it was while the file records never move, and the
  operator submits the already-rendered accept-changes form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  accept the change

#### Scenario: Replacing one open alert with another does not validate a stale per-file form

- **WHEN** a row's per-file action is rendered for a file with one open alert, that alert is then
  acknowledged and a **new** alert of the same kind is opened on that same file, so the count returns
  to what it was while the row itself never moves, and the operator submits the already-rendered
  per-file form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  accept the row

#### Scenario: A record that appears after the page's read is in neither the list nor the fingerprint

- **WHEN** an accept-family page is rendered and a scan commits a further missing file, and its
  alert, immediately after the page has read the population it renders
- **THEN** the page SHALL neither display that record nor include it in the fingerprint it
  publishes, and submitting the action whose population that record joined SHALL be refused because
  the endpoint's own read now sees it

#### Scenario: A re-created record does not validate a form minted for its predecessor

- **WHEN** an accept-family form is rendered listing a missing file, that record is then removed and
  a file is re-created at the **same path with the same content**, whose new record reuses the
  removed record's identifier and is itself recorded missing, and the operator submits the
  originally rendered form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove the re-created record or acknowledge its
  event, and SHALL redirect to the review view with the staleness marker — an identical path,
  digest and identifier SHALL NOT be sufficient for a fingerprint to still match across record
  generations

#### Scenario: A recreated collection does not validate its predecessor's fingerprint

- **WHEN** an accept-family form is rendered, its collection is then deleted and a new collection is
  created that reuses the same identifier, and the originally rendered form is submitted against it
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing in the replacement collection

#### Scenario: Lock contention is a refusal, not a server error

- **WHEN** an accept-family action is submitted while another writer holds the datastore's write
  lock, or has committed since this request's read snapshot, so that the endpoint cannot take the
  write lock its check-and-act depends on
- **THEN** the endpoint SHALL refuse exactly as it does on a fingerprint mismatch — mutating
  nothing and redirecting to the review view with the staleness marker — and SHALL NOT return a
  server error

#### Scenario: A datastore failure that is not contention is not reported as staleness

- **WHEN** an accept-family endpoint's datastore access fails for a reason that is not write-lock
  contention
- **THEN** that failure SHALL surface as the error it is, and SHALL NOT be presented to the operator
  as the collection having changed since the page loaded

#### Scenario: A file appearing in a population no action names does not refuse anything

- **WHEN** a scoped action is submitted after a scan has added a file that is merely new,
  while the action's own population is exactly what the view listed
- **THEN** the endpoint SHALL perform the action, and that file SHALL be neither promoted nor
  otherwise touched

#### Scenario: An accept is refused while an operation is in flight

- **WHEN** the user submits an accept-family form while a scan or stamp operation is running on that
  collection
- **THEN** the endpoint SHALL refuse rather than act on a population that is still changing

#### Scenario: A scoped action from the review view

- **WHEN** the user triggers one of the review view's scoped actions and that action's population
  has not changed since the page was rendered
- **THEN** that population SHALL be accepted — changed files set healthy, or missing records
  removed — the events of those files SHALL be acknowledged, and the view SHALL refresh; the guard
  SHALL NOT stand in the way of an action that is still acting on what it displayed

#### Scenario: A per-file action is bound to its own row

- **WHEN** the user submits a row's accept control after that row has been accepted from another
  session, or after it has been restored and is no longer in the state the row described
- **THEN** the endpoint SHALL refuse, SHALL mutate nothing, and SHALL redirect to the review view
  with the staleness marker

#### Scenario: A folder-scoped action acts only inside its folder

- **WHEN** the review view lists missing files in two folders and the user submits one folder's
  accept-deletions action
- **THEN** only that folder's records SHALL be removed and only their events closed, and the other
  folder's records and open alerts SHALL be untouched

#### Scenario: A folder's fingerprint does not validate another folder

- **WHEN** the fingerprint minted for one folder scope is submitted naming a different folder, the
  whole collection, or the same folder with the subtree/direct-files choice flipped — including
  where the two scopes hold exactly the same files
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing

#### Scenario: A folder-scoped action is refused by drift inside its folder only

- **WHEN** a folder's accept action is rendered, and a scan then records a further file of that
  kind **inside** that folder, and the user submits the already-rendered form
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing
- **AND WHEN** the further file is instead recorded in a **different** folder, the endpoint SHALL
  perform the action on the folder it was rendered for and SHALL leave the newcomer untouched

#### Scenario: A scope of the files directly in a folder leaves its subfolders alone

- **WHEN** a folder holds missing files both directly and in a subfolder, and the user submits the
  action rendered for the files directly in it
- **THEN** only the files directly in that folder SHALL be acted on

#### Scenario: A folder name is matched literally

- **WHEN** two sibling folders are named so that one would match the other under pattern matching —
  a name containing `%` or `_`, or one being a prefix of the other — and the user submits the action
  rendered for one of them
- **THEN** only files under exactly that folder SHALL be acted on

#### Scenario: A malformed scope is refused, not interpreted

- **WHEN** a bulk accept is submitted with a scope the page could not have rendered — one that is
  absolute, over-long, or contains a NUL byte
- **THEN** the endpoint SHALL refuse exactly as it does on a fingerprint mismatch and SHALL mutate
  nothing


#### Scenario: Scan now starts in the background and returns immediately

- **WHEN** the user clicks "Scan now" on a collection that has no operation in progress
- **THEN** the scan SHALL begin in the background, the request SHALL return without waiting for the
  scan to finish, and the collection's status SHALL begin reflecting an in-progress scan

#### Scenario: A second concurrent operation on the same collection is refused

- **WHEN** the user clicks "Scan now" on a collection that already has an operation (scan or stamp)
  in progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Add/edit corpus validates the root path

The add/edit-collection form SHALL validate the entered root path as the user types (server-side
htmx), indicating acceptance when the path is allowed and rejecting it with a clear message
otherwise, and SHALL keep the submit action disabled until the name and a valid root are present.
The server SHALL re-validate the root on submit. The form and its actions SHALL be served under the
`/collection` route prefix (e.g. `/collection/new`, `/collection/validate-root`,
`/collection/{collection_id}/edit`). Legacy `/corpus/...` URLs SHALL 308-redirect to the
corresponding `/collection/...` URL so existing bookmarks keep working.

The form SHALL name the two change policies in plain words — **Locked** and **Working** — each with
a one-line statement of what it flags, and SHALL NOT present the stored policy values as its
labels. The form SHALL NOT offer a control for baselining new files: a collection created from the
form SHALL have automatic graduation of new files enabled, and editing a collection SHALL leave
that stored setting exactly as it was.

#### Scenario: Out-of-bounds or missing root is rejected

- **WHEN** the user enters a root path that does not resolve to an allowed existing directory
- **THEN** the form SHALL show a rejection indicator and SHALL NOT allow submission

#### Scenario: Valid root accepted

- **WHEN** the user enters a name and a root that resolves to an allowed existing directory
- **THEN** the form SHALL indicate acceptance and submission SHALL create/update the collection

#### Scenario: Legacy corpus URL redirects to the collection URL

- **WHEN** a client requests an old `/corpus/{id}` (or any `/corpus/...`) URL
- **THEN** the panel SHALL respond with a 308 redirect to the equivalent `/collection/...` URL

#### Scenario: The policies are named in plain words

- **WHEN** the add- or edit-collection form is rendered
- **THEN** its change-policy control SHALL offer "Locked" and "Working", each with a description of
  what it flags, and SHALL NOT label either choice with its stored value

#### Scenario: A collection created from the form graduates new files automatically

- **WHEN** a collection is created from the form
- **THEN** its `auto_baseline_new` flag SHALL be on, and the form SHALL have offered no control for
  it

#### Scenario: Editing leaves the stored new-file setting alone

- **WHEN** a collection whose `auto_baseline_new` flag is off is edited and saved from the form
- **THEN** the flag SHALL still be off

### Requirement: Proof-state vocabulary and verification instructions match what the operator can do

The panel SHALL use one name per notarization state across every surface — badge, tiles, dashboard,
verify verdict and the explanatory documentation — so that a single state is never described by two
different words, and two different states are never described by one.

There are two states before a proof is confirmed and they SHALL be named distinctly: a proof that is
**queued for stamping and not yet submitted** to a calendar, and a proof that has been **submitted
and is awaiting Bitcoin confirmation**. Only the second is waiting on Bitcoin; describing the first
as awaiting confirmation tells an operator to wait for something that has not been started, and
hides a stalled queue behind the wording for a healthy young proof. No surface SHALL apply the
awaiting-confirmation wording to the queued state.

Summaries SHALL NOT add the two together under one label. Where both are non-zero the summary SHALL
name each with its own count; where one is zero it MAY be omitted. The documentation SHALL name each
state as the interface words it.

The notarization vocabulary SHALL NOT be borrowed for the **integrity** result. A collection summary
reporting that its files match what the last scan recorded SHALL NOT describe them as
*verified*: verification is the notary's word for a proof checked against the Bitcoin record, which
no scan performs, and reusing it credits the collection with evidence that was never gathered.

The documentation SHALL describe only verification paths an operator can actually complete. It SHALL
lead with the public drag-and-drop verifier, SHALL state that verifying requires **both** the file
and its `.ots` proof — the panel's export serves only the proof, so the file must be supplied
separately — and SHALL state plainly that the command-line path requires a reachable Bitcoin Core
node, which is why the application itself defaults to an explorer lookup. It SHALL NOT offer a
command that exits without having verified anything.

#### Scenario: One name per state

- **WHEN** a proof in either not-yet-confirmed state is displayed anywhere in the panel
- **THEN** it SHALL be described with the wording that state's name uses everywhere else, including
  in the documentation, and the queued state SHALL NOT be described as awaiting confirmation

#### Scenario: A summary does not merge the two not-yet-confirmed states

- **WHEN** a collection has both proofs queued for stamping and proofs awaiting Bitcoin confirmation
- **THEN** the summary SHALL report the two counts under their own names rather than one combined
  "awaiting confirmation" total

#### Scenario: Verification instructions are completable

- **WHEN** the operator reads the documentation's verification section
- **THEN** it SHALL lead with the drag-and-drop verifier, SHALL state that both the file and the
  `.ots` proof are required, and SHALL state that the command-line path needs a Bitcoin node

#### Scenario: No command that verifies nothing

- **WHEN** the documentation describes command-line verification
- **THEN** it SHALL NOT suggest an invocation that skips the Bitcoin check and exits without
  verifying

### Requirement: The panel remains usable at phone widths

The panel SHALL remain legible and operable on a phone-sized viewport, and no element SHALL be
allowed to squeeze a collection's identity to an unreadable width. Alert notifications deep-link
operators straight into the panel, so a phone is a first-class entry point rather than an edge case.

Where a live progress indicator cannot fit alongside the content that identifies what it is
progressing, the indicator's non-essential detail SHALL be dropped rather than the identifying
content. Fixed-width metadata cells SHALL reflow rather than clip their values.

Where dropping non-essential detail is not sufficient — the indicator remains wider than the space
its row can give up — the indicator SHALL be moved out of that row entirely so that the identifying
content occupies a full line of its own. An indicator whose width cannot be negotiated must stop
sharing a row with the name, rather than continue to consume it.

A control whose label cannot fit its container SHALL be allowed to wrap rather than overflow the
container it sits in.

On a review view the affected files SHALL come before the explanation of what the actions do: the
list SHALL be preceded by no more than the page heading, a one-line summary and the page-wide
actions, so an operator arriving from an alert on a phone reaches the first file without scrolling
past instructions.

#### Scenario: A running operation does not crush the collection name

- **WHEN** a collection with a running operation is rendered on a viewport narrower than the mobile
  breakpoint
- **THEN** the collection's name SHALL remain readable, with the progress bar omitted if necessary,
  and the progress indicator SHALL occupy its own row rather than share the name's

#### Scenario: A bulk action's label does not push its card off screen

- **WHEN** a bulk action control whose label carries a count is rendered on a viewport narrower
  than the mobile breakpoint
- **THEN** the control SHALL wrap within its container rather than overflow it

#### Scenario: A long root path is readable on a phone

- **WHEN** the collection detail page's root-path cell is rendered on a viewport narrower than the
  mobile breakpoint
- **THEN** the path SHALL wrap rather than be truncated, and SHALL also carry the full value as a
  pointer hint

#### Scenario: Detail metadata reflows instead of clipping

- **WHEN** the collection detail page is rendered on a viewport narrower than the mobile breakpoint
- **THEN** the status metadata cell SHALL reflow to fit its value rather than truncating it

#### Scenario: The files come before the instructions

- **WHEN** a review view with issues is rendered
- **THEN** the first affected file SHALL appear in the page before the recovery guidance and before
  the explanation of the accept actions

### Requirement: A file that came back different is rendered as an alarm, not as an arrival

The panel SHALL render a file whose open event is `restored_changed` — a file that was recorded
missing and reappeared with bytes that do not match the digest recorded for it — with its own
label and with the visual weight of an alarm, never with the muted styling used for informational
activity. Wherever that file is listed for review, the row SHALL show that event's recorded detail,
which carries both digests, **in full and at any viewport width** — the detail line is the only
surviving record of the digest the file had before it came back, so it SHALL NOT be truncated,
clipped or ellipsized. The row is the one surface that distinguishes "your file came back" from
"something else came back in its place"; rendering it as an ordinary change, or as an arrival,
would restore the false reassurance the underlying fix removes. A summary of recent activity that
counts such events SHALL name them as files that came back different, never as restored.

The affected file SHALL appear in the collection's review view and count toward its issue count and
status like any other `modified` file, so the collection SHALL NOT read as all clear while it is
unresolved.

#### Scenario: The review row names a changed reappearance as such

- **WHEN** a scan writes a `restored_changed` event and the operator opens the review view
- **THEN** the file's row SHALL carry a label distinct from an ordinary change, SHALL be styled as
  an alarm, and SHALL show the recorded detail carrying both digests, with both digests fully
  visible (the detail line wraps rather than truncating)

#### Scenario: Recent activity does not count it as a restore

- **WHEN** the dashboard's recent-activity summary covers a day on which a file came back different
- **THEN** that line SHALL name it as a file that came back different, and SHALL NOT fold it into
  the count of restored files

#### Scenario: A collection with an unresolved changed reappearance does not read as all clear

- **WHEN** a collection's only non-`ok` file is one that reappeared with different bytes
- **THEN** the collection SHALL NOT be presented as all clear, and the file SHALL appear in its
  review view

## REMOVED Requirements

### Requirement: Dashboard shows status and acknowledges events

**Reason**: The requirement enshrined two state machines (an event reading log and the file
status), a count and a verb for each, and a feed ordered to keep a bulk acknowledge control honest.
The acknowledge verb is removed; the dashboard's remaining guarantees (issue totals that agree, a
destination for them, the last-activity tile's wording) are restated without it.

**Migration**: Superseded by "The dashboard leads with what needs the operator" and "The panel has
one kind of open item, the issue". `POST /events/{id}/ack` and `POST /events/ack-all` are deleted;
nothing forwards. No data migration: `events.acknowledged_at` keeps being written by the accept and
restore paths.

### Requirement: Review and recover changed or missing files

**Reason**: The review view is rebuilt — files before explanation, grouped by folder, resolved by
outcome-named actions — and its mark-reviewed controls and "alerts left open by a restored file"
card are removed with the acknowledge verb.

**Migration**: Superseded by "The review view lists issues by folder with the facts needed to
decide". Every surviving guarantee (recovery guidance without a backup tool and with both outcomes
of a rescan, truncation links, clipboard failure handling, the empty states) is carried over there.
`POST /collection/{id}/review/ack-all` is deleted.

### Requirement: Accept is offered as scoped actions named for their consequence

**Reason**: Three populations become two: files that are merely new are no longer something the
operator accepts, so the baseline action is gone, and the remaining two actions are renamed for
their outcome and gain a folder scope.

**Migration**: Superseded by "Issues are resolved by actions named for their outcome". The two
forbidden reassurances are carried over verbatim in substance. `POST /collection/{id}/accept` is
deleted; `cairn accept` keeps the collection-wide behaviour for scripted use.

### Requirement: Fleet-wide review of every collection's open issues

**Reason**: The page keeps its purpose and its single-snapshot and no-spanning-action guarantees,
but its ordering (largest first), its unreviewed counts and its mark-reviewed row control are
replaced.

**Migration**: Superseded by "Fleet-wide review groups every collection's issues".

### Requirement: The collection detail page discloses the new-file count

**Reason**: The requirement existed to explain an arithmetic gap between "total" and "matching
baseline + issues". The detail page no longer shows a baselined count, so there is no gap to
explain, and presenting new files as "not yet baselined" is the to-do impression this change
removes.

**Migration**: None. New files remain listable through the file browser's `new` filter and remain
visible in the collection card's status bar.
