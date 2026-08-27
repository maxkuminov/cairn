# web-panel Specification (delta)

## MODIFIED Requirements

### Requirement: Verify a tracked file's proof from the panel without upload

The verify page SHALL let the user search files Cairn already tracks (no file upload) and verify a
selected file by re-hashing it from the read-only store and checking the stored `.ots` proof. The
result SHALL present the verdict and, when complete, the SHA-256, the existed-by date, and the
Bitcoin block, plus an option to export the portable bundle. A complete "Anchored" badge elsewhere
SHALL deep-link here and verify immediately.

The verdict SHALL be chosen by *why* verification did not succeed, not by the proof's stored state.
Specifically, the panel SHALL evaluate the outcomes in this order: the live file being unavailable,
then a **digest mismatch**, then a proof that is genuinely not yet confirmed, then anything else. A
file whose bytes no longer match the digest its proof commits to SHALL be rendered as a failure that
names that fact — it SHALL NOT be rendered as a proof awaiting confirmation, and SHALL NOT be
accompanied by copy telling the operator to wait. This is the product's core detection; presenting
it as a young proof is a false negative on the one claim Cairn exists to make.

A failure to *reach* the verification backend (block explorer or Bitcoin node) SHALL be reported as
verification being unavailable, with copy stating that Cairn could not check and that this says
nothing about the file. Such a failure SHALL NOT inherit the file's stored proof state, and SHALL
NOT be reported as a pending proof or as a verified one.

Every list of already-anchored files SHALL render each row's **actual** notarization state. A row
SHALL NOT be given a fixed "Anchored" badge: the badge is what an operator reads to decide whether a
proof is usable as evidence, and the list is ordered newest-first, so a hardcoded badge shows the
least-confirmed proofs as the most confirmed. The page SHALL render a distinct empty state when
nothing is anchored yet, separate from the empty state for a search that matched nothing.

The result page SHALL describe how the proof was checked from the backend actually used, rather than
asserting a fixed backend.

#### Scenario: Verify renders a verdict

- **WHEN** the user selects an anchored file on the verify page
- **THEN** the panel SHALL run verification server-side and render the verdict (and, when complete,
  the block and existed-by date) without uploading the file

#### Scenario: A changed file is never reported as pending

- **WHEN** the user verifies a stamped file whose bytes have changed since it was stamped
- **THEN** the panel SHALL render a failure verdict naming the mismatch, and SHALL NOT render a
  "pending confirmation" title or any copy suggesting the result will settle on its own

#### Scenario: An unreachable verification backend is not a pending proof

- **WHEN** verification fails because the block explorer or node cannot be reached
- **THEN** the panel SHALL report that verification is unavailable, with the transport reason, and
  SHALL NOT present the file's stored proof state as the verdict

#### Scenario: A missing live file still refuses to fall back to the stored digest

- **WHEN** the user verifies a file that is gone from disk
- **THEN** the panel SHALL report that the file is unavailable and cannot be verified, and SHALL NOT
  verify against the stored digest

#### Scenario: The anchored list shows real proof states

- **WHEN** the verify page lists recently anchored files that include a not-yet-confirmed proof
- **THEN** that row SHALL show the not-yet-confirmed badge, not "Anchored"

#### Scenario: Nothing anchored yet

- **WHEN** the user opens the verify page and no file has been stamped
- **THEN** the page SHALL render an empty state saying so, distinct from the "no search matches"
  message

### Requirement: Dashboard shows status and acknowledges events

The dashboard SHALL show summary tiles, a per-collection card for each collection, and a
recent-events feed. Unacknowledged events SHALL offer a "mark reviewed" action that, on use, removes
the call-to-action and decrements the open-issue counts without a full page reload.

The vocabulary SHALL distinguish the two state machines the panel tracks. Marking an event reviewed
writes only to the reading log: the file's own state is unchanged, so the collection's status and
the file-derived counts SHALL remain as they were. Every control that performs this action SHALL be
labelled as reviewing/noting rather than resolving, and SHALL carry hint copy stating that the file
stays on record, keeps any existence proof, and that the collection keeps its alert status until the
file is restored or retired.

The dashboard SHALL additionally offer a bulk action, shown only while at least one open event
exists, that marks every unacknowledged event belonging to the current user's collections
acknowledged (recording who and when) and refreshes the recent-events feed, the "need action" count,
and the sidebar alert badge in place without a full page reload. Because it reaches events that are
not visible in the capped feed, that control SHALL state the number of events it will affect and
SHALL require an explicit confirmation before acting. It SHALL be scoped to the current user's own
collections and SHALL NOT acknowledge events of other users' collections. It SHALL set
acknowledgement only — it SHALL NOT re-baseline files (that remains the `accept` operation).

The **Open issues** tile SHALL be a working link to the place where those issues can be acted on
whenever the count is greater than zero, presented with the same clickable affordance as the
equivalent collection-page stat. When exactly one collection is affected it SHALL link to that
collection's review view; when more than one is affected it SHALL link to the collections list. It
SHALL NOT link to a route that does not exist. At a count of zero it SHALL remain a non-interactive
element.

The sidebar alert badge SHALL carry an accessible label naming what it counts, and SHALL count the
same population as the Open issues tile beside it (files that are missing or modified) so the two
cannot disagree. That count SHALL be computed in one place and used by every render of the badge,
including each out-of-band refresh, so a background scan keeps a long-open page's badge current.

The dashboard tiles SHALL account for every tracked file, including files that are watched but not
yet baselined, so that the total does not silently exceed the sum of the tiles that explain it.
Status indicators SHALL use visually distinct icons for the "attention" and "alert" states.

#### Scenario: Mark an event reviewed

- **WHEN** the user marks an unacknowledged event reviewed
- **THEN** the event SHALL be recorded acknowledged, its row SHALL update in place, and the "need
  action" count and sidebar alert badge SHALL refresh — while the file's own status and the
  collection's status SHALL be unchanged

#### Scenario: Bulk mark-reviewed states its scope and confirms

- **WHEN** the user triggers the dashboard's bulk mark-reviewed action while open events exist
- **THEN** the control SHALL have stated how many events it affects and SHALL have required an
  explicit confirmation, and on confirmation every unacknowledged event in the user's collections
  SHALL be acknowledged and the feed, "need action" count and sidebar badge SHALL refresh without a
  full page reload

#### Scenario: Bulk mark-reviewed is scoped to the user

- **WHEN** a user triggers the bulk action in multi-user mode
- **THEN** only events belonging to that user's collections SHALL be acknowledged, and another
  user's unacknowledged events SHALL be left untouched

#### Scenario: Bulk mark-reviewed when nothing is open

- **WHEN** there are no unacknowledged events for the user
- **THEN** the bulk control SHALL NOT be shown (and the route SHALL be a no-op if invoked directly)

#### Scenario: Open issues tile links to a single affected collection

- **WHEN** exactly one collection has missing or modified files
- **THEN** the Open issues tile SHALL be a link to that collection's review view, with a visible
  call-to-action

#### Scenario: Open issues tile with several affected collections

- **WHEN** two or more collections have missing or modified files
- **THEN** the Open issues tile SHALL link to the collections list, and SHALL NOT link to a
  fleet-wide review route that does not exist

#### Scenario: Open issues tile at zero

- **WHEN** no collection has missing or modified files
- **THEN** the tile SHALL render as a non-interactive element with no link

#### Scenario: The badge and the tile agree

- **WHEN** a user's collections contain both missing and modified files
- **THEN** the sidebar badge SHALL show the same total as the Open issues tile and SHALL expose an
  accessible label naming what the number counts

#### Scenario: Watched-but-not-baselined files are explained

- **WHEN** a collection contains files that are watched but not yet baselined
- **THEN** the dashboard SHALL show their count in its own tile, so the monitored-file total is
  accounted for by the tiles beside it

### Requirement: Accept and scan actions are available from the panel

The collection detail page SHALL offer "Scan now", and SHALL offer a re-baseline action only in the
state where that action is harmless. Both mutate via the existing services and refresh the affected
view without a full reload.

Re-baselining is irreversible: it rewrites the expected version of modified files and removes the
records of missing ones. The collection detail page SHALL NOT present it as the page's primary
action while the collection has missing or modified files. In that state the primary action SHALL be
a link to the review view, which explains the choice between noting a change and adopting it; the
re-baseline action SHALL be reachable only from there. When the collection has no missing or
modified files but does have files that are merely not yet baselined, the page MAY offer that
harmless baseline action directly, and it SHALL require a confirmation naming what it will do. Any
re-baseline form rendered on this page SHALL carry a confirmation.

"Scan now" SHALL run the scan **asynchronously** — it SHALL start the scan in the background and
return immediately rather than blocking the request until the scan completes, so the panel can show
live operation status. A scan SHALL NOT be started for a collection that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: The destructive action is not offered beside the issues it would erase

- **WHEN** the user opens a collection that has missing or modified files
- **THEN** the page header SHALL offer a review link as its primary action and SHALL NOT contain an
  accept/re-baseline form

#### Scenario: Baselining new files is offered, and confirmed

- **WHEN** the user opens a collection with no missing or modified files but with files not yet
  baselined
- **THEN** the page MAY offer a baseline action, which SHALL require a confirmation before
  submitting

#### Scenario: Accept changes from the review view

- **WHEN** the user accepts changes from the collection's review view
- **THEN** the collection SHALL be re-baselined (new/modified → ok, missing removed, events
  acknowledged) and the stat row + table SHALL refresh

#### Scenario: Scan now starts in the background and returns immediately

- **WHEN** the user clicks "Scan now" on a collection that has no operation in progress
- **THEN** the scan SHALL begin in the background, the request SHALL return without waiting for the
  scan to finish, and the collection's status SHALL begin reflecting an in-progress scan

#### Scenario: A second concurrent operation on the same collection is refused

- **WHEN** the user clicks "Scan now" on a collection that already has an operation (scan or stamp)
  in progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Review and recover changed or missing files

The panel SHALL provide a per-collection **review** view, reachable directly from the dashboard,
that focuses the operator on exactly the files that need attention and tells them what to do next.
The dashboard collection card's issue count (and the collection-detail "changed / missing" stat)
SHALL be a visible, clickable link to that collection's review view. The review view SHALL list
each `missing` file and each WORM `modified` file for the collection, and for each SHALL show what
happened (missing vs modified) with its last-seen / detected time, its size, and whether the file
was notarized. The review view SHALL let the operator mark an individual file's event reviewed, mark
all of the collection's open events reviewed, and accept (re-baseline) the collection, reusing the
existing acknowledge/accept behavior and refreshing the "need action" count and sidebar alert badge
in place without a full page reload. The review view SHALL provide **recovery guidance that assumes
no particular backup tool**: a copyable list of the affected file paths and tool-neutral recovery
instructions; for files that were notarized it SHALL note that their OpenTimestamps proof of prior
existence survives. All review and recovery actions SHALL be scoped to the current user's own
collections.

The controls SHALL be styled by consequence, not by the colour of the condition they refer to: the
per-file mark-reviewed action, which changes nothing about any file, SHALL be the quiet control, and
the bulk accept, which rewrites baselines and removes records, SHALL be the loud one. The bulk
mark-reviewed action SHALL state how many alerts it will clear and that nothing about the files
changes.

The control for clearing alerts SHALL be rendered whenever the collection has open events, whether
or not any file is currently missing or modified. A file that was missing and has since been
restored leaves its alert open while the file itself is healthy; suppressing the clearing control in
that state leaves a red count with nothing in the interface able to clear it. In that state the view
SHALL name the case and SHALL NOT offer the accept action — from a view listing no issues, accept
would silently baseline every not-yet-baselined file in the collection.

Where the list of affected files is truncated, the link to the full set SHALL open the file browser
already switched to the list view and already filtered to issues, rather than a view that discards
both. Copy-to-clipboard controls SHALL handle a clipboard write being unavailable or rejected, and
SHALL report the failure rather than silently appearing to succeed.

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with a missing/modified indicator, its last-seen or
  detected time, its size, and whether it was notarized

#### Scenario: Mark a file reviewed from the review view

- **WHEN** the user marks a file's event reviewed from the review view
- **THEN** the event SHALL be recorded acknowledged and the "need action" count and sidebar alert
  badge SHALL refresh in place without a full page reload

#### Scenario: Bulk accept and mark-all-reviewed from the review view

- **WHEN** the user triggers accept or mark-all-reviewed from the review view
- **THEN** the action SHALL reuse the existing accept/acknowledge behavior scoped to the user's own
  collections, and the view SHALL refresh to reflect the cleared issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  survives

#### Scenario: Alerts left open by a restored file can be cleared

- **WHEN** the user opens the review view for a collection with no missing or modified files but
  with open events
- **THEN** the view SHALL render the mark-all-reviewed control, naming the case, and SHALL NOT
  render the accept action

#### Scenario: Nothing to review and nothing open

- **WHEN** the user opens the review view for a collection with no missing or modified files and no
  open events
- **THEN** the view SHALL render an "all clear" empty state and SHALL offer no review/accept actions

#### Scenario: The truncation link lands filtered

- **WHEN** the affected-file list is truncated and the user follows the link to the full set
- **THEN** the file browser SHALL open in its list view with the issues filter already applied and
  reflected in the filter control

#### Scenario: A failed clipboard write is reported

- **WHEN** a copy-paths action cannot write to the clipboard
- **THEN** the panel SHALL attempt a fallback and, if that also fails, SHALL tell the user the copy
  did not happen rather than showing the success confirmation

## ADDED Requirements

### Requirement: Proof coverage is reported as a ratio, never as an unearned completeness claim

The panel SHALL NOT state or imply that a collection's proofs are all confirmed unless there is at
least one confirmed proof and nothing outstanding — no proof awaiting submission, none awaiting
Bitcoin confirmation, and no file eligible for stamping that has not been stamped. Every
completeness claim in this product is read as a statement about the collection, so one computed over
only the files that happen to have been stamped is a false assurance about the rest.

Where a collection has files eligible for stamping that carry no proof, the panel SHALL show
coverage as a ratio of confirmed proofs to stampable files, together with a distinct warning line
naming how many files are not stamped, next to the control that stamps them.

The count of unstamped files SHALL exclude files whose status is `missing`, matching the population
the stamp-all operation actually queues. Including missing files would produce a warning that no
operator action can ever clear.

A collection with no indexed files SHALL say so, and SHALL NOT report "all clear", "all files
verified" or "all confirmed". A root that is a typo or a failed mount scans clean forever; a
zero-file collection is a configuration failure to surface, not a healthy state to celebrate.

#### Scenario: Unstamped files defeat the completeness claim

- **WHEN** a collection has confirmed proofs but also files eligible for stamping with no proof
- **THEN** the panel SHALL show the confirmed-to-stampable ratio and a warning naming the unstamped
  count, and SHALL NOT say "all confirmed"

#### Scenario: No confirmed proofs

- **WHEN** a collection has no confirmed proof at all
- **THEN** the panel SHALL NOT say "all confirmed"

#### Scenario: Missing files are not counted as unstamped

- **WHEN** a collection's only files without proofs are files recorded `missing`
- **THEN** the unstamped warning SHALL NOT be shown, because the stamp-all operation would not act
  on them

#### Scenario: Everything stamped and confirmed

- **WHEN** every stampable file in a collection has a confirmed proof and none are pending
- **THEN** the panel MAY state that all proofs are confirmed

#### Scenario: A collection with no files

- **WHEN** a collection has no indexed files
- **THEN** the card and the collection view SHALL report that no files are indexed yet, and SHALL
  NOT report that all files are verified or all proofs confirmed

### Requirement: The collection file browser honours view and filter deep links

The collection detail view SHALL accept optional parameters selecting the browser view (tree or
list) and the status filter, and SHALL render the browser in the requested state on first load —
including applying the requested filter to the initial file query and marking the corresponding
controls active. A link that sets a filter but lands on an unfiltered list with the filter control
showing as checked is worse than no deep link at all, because the operator reads the list as the
filtered set.

Because the filter control is not available in the tree view, requesting a filter other than "all"
without explicitly requesting a view SHALL open the list view. Unrecognized values SHALL fall back
to the defaults, which SHALL reproduce the view rendered when no parameters are supplied.

#### Scenario: Filtered deep link renders filtered

- **WHEN** the collection view is opened with the list view and the issues filter requested
- **THEN** the initial file list SHALL contain only files with issues, the Issues control SHALL be
  marked active, and the List view SHALL be marked active

#### Scenario: A filter alone implies the list view

- **WHEN** the collection view is opened with a filter other than "all" and no view specified
- **THEN** the list view SHALL be rendered, because the filter control is not reachable in the tree
  view

#### Scenario: No parameters reproduce today's view

- **WHEN** the collection view is opened with no parameters, or with unrecognized values
- **THEN** it SHALL render the tree view with no status filter applied

### Requirement: Proof-state vocabulary and verification instructions match what the operator can do

The panel SHALL use one name per notarization state across every surface — badge, tiles, dashboard,
verify verdict and the explanatory documentation — so that a single state is never described by two
different words. In particular the not-yet-confirmed state SHALL be worded identically wherever it
appears, and the documentation SHALL name both that state and the confirmed state as the interface
words them.

The documentation SHALL describe only verification paths an operator can actually complete. It SHALL
lead with the public drag-and-drop verifier, SHALL state that verifying requires **both** the file
and its `.ots` proof — the panel's export serves only the proof, so the file must be supplied
separately — and SHALL state plainly that the command-line path requires a reachable Bitcoin Core
node, which is why the application itself defaults to an explorer lookup. It SHALL NOT offer a
command that exits without having verified anything.

#### Scenario: One name per state

- **WHEN** a not-yet-confirmed proof is displayed anywhere in the panel
- **THEN** it SHALL be described with the same wording as the documentation uses for that state

#### Scenario: Verification instructions are completable

- **WHEN** the operator reads the documentation's verification section
- **THEN** it SHALL lead with the drag-and-drop verifier, SHALL state that both the file and the
  `.ots` proof are required, and SHALL state that the command-line path needs a Bitcoin node

#### Scenario: No command that verifies nothing

- **WHEN** the documentation describes command-line verification
- **THEN** it SHALL NOT suggest an invocation that skips the Bitcoin check and exits without
  verifying

### Requirement: Environment-only configuration is presented as description, not as a control

The Settings page SHALL NOT render a control that looks interactive for configuration it cannot
change. Where a setting is supplied only by the environment, the page SHALL present it as
descriptive text that names the environment variable(s) involved, marks which value is active, and
notes that changing it requires a restart.

The verification backend SHALL remain environment-only. It SHALL NOT be persisted through the panel
unless the command-line tools read the same override: the panel and the CLI disagreeing about how an
integrity claim was verified is the one disagreement this product cannot tolerate.

#### Scenario: The verification tab is descriptive

- **WHEN** an operator opens the Verification settings tab
- **THEN** the backends SHALL be described as text naming the governing environment variables and
  the restart requirement, with the active one marked, and nothing there SHALL carry an interactive
  affordance

#### Scenario: The verification backend is not persisted from the panel

- **WHEN** the panel presents the verification backend
- **THEN** it SHALL be read-only, sourced from the environment

### Requirement: The panel remains usable at phone widths

The panel SHALL remain legible and operable on a phone-sized viewport, and no element SHALL be
allowed to squeeze a collection's identity to an unreadable width. Alert notifications deep-link
operators straight into the panel, so a phone is a first-class entry point rather than an edge case.

Where a live progress indicator cannot fit alongside the content that identifies what it is
progressing, the indicator's non-essential detail SHALL be dropped rather than the identifying
content. Fixed-width metadata cells SHALL reflow rather than clip their values.

#### Scenario: A running operation does not crush the collection name

- **WHEN** a collection with a running operation is rendered on a viewport narrower than the mobile
  breakpoint
- **THEN** the collection's name SHALL remain readable, with the progress bar omitted if necessary

#### Scenario: Detail metadata reflows instead of clipping

- **WHEN** the collection detail page is rendered on a viewport narrower than the mobile breakpoint
- **THEN** the status metadata cell SHALL reflow to fit its value rather than truncating it
