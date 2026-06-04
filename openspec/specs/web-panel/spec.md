# web-panel Specification

## Purpose
TBD - created by archiving change add-web-panel. Update Purpose after archive.
## Requirements
### Requirement: Server-rendered panel in the locked Slate design with light/dark mode

The system SHALL serve a control panel rendered server-side (Jinja2) styled with the locked Slate
design tokens, supporting both light and dark mode selectable by the user and persisted across
requests. The panel SHALL run without a login wall in single-user mode.

#### Scenario: Pages render

- **WHEN** a user opens the dashboard, a corpus detail page, the add-corpus form, the verify page,
  or settings in single-user mode
- **THEN** each SHALL return HTTP 200 with the shell (sidebar + topbar) and the screen's content

#### Scenario: Mode toggle persists

- **WHEN** the user toggles light/dark mode
- **THEN** the choice SHALL be stored (cookie) and subsequent pages SHALL render with that
  `data-mode` without a flash

### Requirement: Dashboard shows status and acknowledges events

The dashboard SHALL show summary tiles, a per-corpus card for each corpus, and a recent-events
feed. Unacknowledged events SHALL offer an Acknowledge action that, on use, removes the
call-to-action and decrements the open-issue counts without a full page reload.

The dashboard SHALL additionally offer a bulk "Acknowledge all" action, shown only while at least
one open event exists. Using it SHALL mark every unacknowledged event belonging to the current
user's corpora acknowledged (recording who and when) and SHALL refresh the recent-events feed, the
"need action" count, and the sidebar alert badge in place without a full page reload. The bulk
action SHALL be scoped to the current user's own corpora and SHALL NOT acknowledge events of other
users' corpora. It SHALL set acknowledgement only — it SHALL NOT re-baseline files (that remains
the `accept` operation).

#### Scenario: Acknowledge an event

- **WHEN** the user clicks Acknowledge on an unacknowledged event
- **THEN** the event SHALL be marked acknowledged, its row SHALL update in place, and the sidebar
  alert badge / "need action" count SHALL decrease

#### Scenario: Acknowledge all open events

- **WHEN** the user clicks "Acknowledge all" while one or more open events exist
- **THEN** every unacknowledged event in the user's corpora SHALL be marked acknowledged, the feed
  SHALL re-render with no remaining Acknowledge actions, and the "need action" count and sidebar
  alert badge SHALL drop to zero — all without a full page reload

#### Scenario: Acknowledge all is scoped to the user

- **WHEN** a user triggers "Acknowledge all" in multi-user mode
- **THEN** only events belonging to that user's corpora SHALL be acknowledged, and another user's
  unacknowledged events SHALL be left untouched

#### Scenario: Acknowledge all when nothing is open

- **WHEN** there are no unacknowledged events for the user
- **THEN** the "Acknowledge all" control SHALL NOT be shown (and the route SHALL be a no-op if
  invoked directly)

### Requirement: Accept and scan actions are available from the panel

The corpus detail page SHALL offer "Scan now" and (when there are issues) "Accept changes"
actions that mutate via the existing services and refresh the affected view without a full reload.

"Scan now" SHALL run the scan **asynchronously** — it SHALL start the scan in the background and
return immediately rather than blocking the request until the scan completes, so the panel can show
live operation status. A scan SHALL NOT be started for a corpus that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: Accept changes from the panel

- **WHEN** the user clicks Accept changes on a corpus with modified/new/missing files
- **THEN** the corpus SHALL be re-baselined (new/modified → ok, missing removed, events
  acknowledged) and the stat row + table SHALL refresh

#### Scenario: Scan now starts in the background and returns immediately

- **WHEN** the user clicks "Scan now" on a corpus that has no operation in progress
- **THEN** the scan SHALL begin in the background, the request SHALL return without waiting for the
  scan to finish, and the corpus's status SHALL begin reflecting an in-progress scan

#### Scenario: A second concurrent operation on the same corpus is refused

- **WHEN** the user clicks "Scan now" on a corpus that already has an operation (scan or stamp) in
  progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Add/edit corpus validates the root path

The add/edit-collection form SHALL validate the entered root path as the user types (server-side
htmx), indicating acceptance when the path is allowed and rejecting it with a clear message
otherwise, and SHALL keep the submit action disabled until the name and a valid root are present.
The server SHALL re-validate the root on submit. The form and its actions SHALL be served under the
`/collection` route prefix (e.g. `/collection/new`, `/collection/validate-root`,
`/collection/{collection_id}/edit`). Legacy `/corpus/...` URLs SHALL 308-redirect to the
corresponding `/collection/...` URL so existing bookmarks keep working. The form SHALL expose an
"auto-baseline new files" control whose state is persisted to the collection's `auto_baseline_new`
flag and pre-filled from it when editing.

#### Scenario: Out-of-bounds or missing root is rejected

- **WHEN** the user enters a root path that does not resolve to an allowed existing directory
- **THEN** the form SHALL show a rejection indicator and SHALL NOT allow submission

#### Scenario: Valid root accepted

- **WHEN** the user enters a name and a root that resolves to an allowed existing directory
- **THEN** the form SHALL indicate acceptance and submission SHALL create/update the collection

#### Scenario: Legacy corpus URL redirects to the collection URL

- **WHEN** a client requests an old `/corpus/{id}` (or any `/corpus/...`) URL
- **THEN** the panel SHALL respond with a 308 redirect to the equivalent `/collection/...` URL

#### Scenario: Auto-baseline toggle persists

- **WHEN** the user turns the "auto-baseline new files" control on (or off) and submits the form
- **THEN** the collection's `auto_baseline_new` flag SHALL be saved accordingly, and re-opening the
  edit form SHALL show the saved state

### Requirement: Verify a tracked file's proof from the panel without upload

The verify page SHALL let the user search files Cairn already tracks (no file upload) and verify a
selected file by re-hashing it from the read-only store and checking the stored `.ots` proof. The
result SHALL present the verdict and, when complete, the SHA-256, the existed-by date, and the
Bitcoin block, plus an option to export the portable bundle. A complete "Anchored" badge elsewhere
SHALL deep-link here and verify immediately.

#### Scenario: Verify renders a verdict

- **WHEN** the user selects an anchored file on the verify page
- **THEN** the panel SHALL run verification server-side and render the verdict (and, when complete,
  the block and existed-by date) without uploading the file

### Requirement: Stamp-all control in the corpus view

The corpus view SHALL offer an owner/admin control to stamp all currently-unstamped files in that
corpus (the on-demand backfill). The control SHALL be subject to the same authorization scoping as
the rest of the panel (in `multi` mode a user SHALL only stamp corpora they own; an admin MAY act on
any). The control SHALL NOT be offered for `none` (tripwire) corpora, which are never stamped.

Stamp-all SHALL run **asynchronously** — it SHALL start the backfill in the background and return
immediately rather than blocking the request until every file is stamped, so the panel can show live
stamping status. Stamp-all SHALL NOT be started for a corpus that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: Owner triggers stamp-all from the corpus view

- **WHEN** the corpus owner (or an admin) activates the stamp-all control for a `perfile` corpus with
  no operation in progress
- **THEN** the backfill SHALL begin in the background, the request SHALL return without waiting for it
  to finish, and the corpus's status SHALL begin reflecting an in-progress stamping operation that
  stamps every currently-unstamped file via the batched path

#### Scenario: Stamp-all is not offered for tripwire corpora

- **WHEN** the corpus's OTS mode is `none`
- **THEN** the stamp-all control SHALL NOT be shown for that corpus

#### Scenario: Stamp-all is refused while another operation runs

- **WHEN** the user activates stamp-all on a corpus that already has a scan or stamp in progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Corpus file list is searched, filtered, sorted, and paginated server-side

The corpus detail page SHALL offer two browse views of the corpus contents — a **folder tree**
(default) and a **flat list** — with a control to switch between them. Both views SHALL be rendered
server-side and SHALL NOT materialize the entire file set (corpora can hold ~186k files).

The **folder tree** SHALL present the corpus as a lazily expanded directory hierarchy derived from
each file's relative path, fetching **one directory level per request**. Expanding a folder SHALL
return that folder's immediate subfolders and the files directly within it; subfolders SHALL be
fetchable on demand and SHALL NOT be pre-expanded recursively. Each subfolder row SHALL show its file
count and a roll-up indicator when any file beneath it has status `modified` or `missing`. Files at a
level SHALL themselves be paginated when they exceed one page, so a single large folder is never
rendered in full.

The **flat list** SHALL list files via server-side search, status filtering, sorting, and pagination.
Search SHALL match the relative path; the filter SHALL offer All / Issues / New / OK.

Sorting (flat list) SHALL be server-side over a fixed whitelist of columns — relative path, size,
modified time (`last_changed`), notarization time (`ots_stamped_at`), and last-checked time — each
toggleable ascending or descending. An unrecognized sort or direction SHALL fall back to the default.
The default order SHALL be newest-activity-first (`last_changed` descending) so the most recently
changed files appear first on load. Every sort SHALL apply a stable secondary tiebreak (relative
path) so pagination is deterministic. The active sort column and direction SHALL be indicated in the
table header.

Pagination SHALL expose navigation (previous / next) and a current-page-of-total-pages indicator,
returning at most one page of rows per request. The active search query, status filter, and sort
SHALL be preserved across page changes and across one another.

Each file row SHALL prominently display a timestamp. For a notarized file the row SHALL show the
OTS stamp date (`ots_stamped_at`) together with the notarization-state badge; a `complete` proof's
notarization cell SHALL deep-link to the verify page for the block-confirmed existed-by date. The
list SHALL NOT fabricate or fetch the Bitcoin block date per row. For an unstamped file, or a
tripwire (`ots_mode='none'`) corpus that hides the notarization column, the row SHALL fall back to
showing the file's last-changed date. The footer SHALL report how many of the total are shown.

#### Scenario: Tree view is the default browser

- **WHEN** the user opens a corpus detail page
- **THEN** the folder-tree view SHALL be shown by default, listing the top-level folders and files of
  the corpus root, and a control SHALL be present to switch to the flat list view

#### Scenario: Expanding a folder fetches one level server-side

- **WHEN** the user expands a folder in the tree
- **THEN** the response SHALL contain only that folder's immediate subfolders and the files directly
  within it (not the whole subtree), fetched server-side

#### Scenario: Subfolder shows count and issue roll-up

- **WHEN** a folder in the tree contains a file with status `modified` or `missing` anywhere beneath
  it
- **THEN** that folder's row SHALL display its file count and an issue indicator

#### Scenario: Switching to the list view preserves flat-list behavior

- **WHEN** the user switches from the tree to the list view
- **THEN** the existing searched / filtered / sorted / paginated flat list SHALL be shown, defaulting
  to newest-activity-first

#### Scenario: Only a page of results is returned

- **WHEN** a corpus has more files than one page and the user loads or searches the flat list
- **THEN** the response SHALL contain at most one page of rows plus a "showing N of TOTAL"
  indicator, never the full list

#### Scenario: Filter to issues

- **WHEN** the user selects the Issues filter
- **THEN** only files with status `modified` or `missing` SHALL be listed

#### Scenario: Default order is newest activity first

- **WHEN** the user opens the flat list without choosing a sort
- **THEN** files SHALL be ordered by most recent change (`last_changed`) descending, with relative
  path as a stable tiebreak

#### Scenario: Sort by a chosen column toggles direction

- **WHEN** the user activates a sortable column header (e.g. size or notarized date)
- **THEN** the list SHALL re-query server-side ordered by that column, the chosen direction SHALL be
  indicated in the header, and re-activating the same column SHALL reverse the direction

#### Scenario: Page through results preserving search, filter, and sort

- **WHEN** the user advances to the next page with an active search, filter, and/or sort
- **THEN** the next page of the same filtered, sorted result set SHALL be returned, the
  page-of-total indicator SHALL update, and previous SHALL be disabled on the first page and next
  on the last

#### Scenario: Notarized file shows its stamp date and deep-links to verify

- **WHEN** a notarized file is listed in a `perfile` corpus
- **THEN** its row SHALL show the OTS stamp date with the notarization-state badge, and a `complete`
  proof's notarization cell SHALL link to the verify page for the block-confirmed existed-by date

#### Scenario: Unstamped or tripwire file falls back to last-changed date

- **WHEN** a file has no proof, or the corpus is tripwire (`ots_mode='none'`) and hides the
  notarization column
- **THEN** the row SHALL still display a meaningful timestamp by showing the file's last-changed date

### Requirement: Live operation status is surfaced on the dashboard and corpus view

The panel SHALL surface whether a corpus currently has a background operation in progress and which
kind it is. When a corpus has a run in progress (result `running`), the dashboard corpus card and the
corpus detail status indicator SHALL show an in-progress badge **labelled by the operation kind** —
scanning for an integrity scan, stamping for a stamp backfill, and upgrading proofs for an OTS upgrade
pass.

When the run carries a known or estimable total, the badge SHALL show progress as items processed out
of that total with a corresponding percentage and a progress bar; for a scan the total MAY be an
estimate and the percentage SHALL NOT reach 100% before the scan finishes. When no total is available
(e.g. a first-ever scan with no baseline), the badge SHALL show an indeterminate in-progress state
with the elapsed time and the running processed count, without a misleading percentage.

While an operation is in progress, the indicator SHALL refresh on its own (without a manual page
reload) and SHALL stop refreshing once the operation finishes, at which point the indicator SHALL
resolve to the corpus's normal status. A corpus with no operation in progress SHALL NOT poll. The
indicator SHALL be read-only and SHALL NOT alter scan, accept, stamp, or upgrade behavior.

#### Scenario: A scanning corpus shows a labelled progress badge

- **WHEN** a corpus has an integrity scan in progress and a prior completed scan provides a baseline
- **THEN** its dashboard card and detail status SHALL show a "Scanning…" badge with items processed of
  an estimated total and a percentage that does not reach 100% before the scan finishes

#### Scenario: A stamping or upgrading corpus shows the matching label and exact progress

- **WHEN** a corpus has a stamp backfill or an OTS upgrade pass in progress
- **THEN** the badge SHALL be labelled accordingly ("Stamping…" / "Upgrading proofs…") and SHALL show
  exact progress (processed out of the known total) since those operations know their total up front

#### Scenario: First-ever scan shows an indeterminate badge

- **WHEN** a corpus is being scanned for the first time with no completed scan to estimate from
- **THEN** the badge SHALL show an indeterminate "Scanning…" state with elapsed time and the running
  count, and SHALL NOT display a percentage

#### Scenario: The badge updates itself and stops when the operation finishes

- **WHEN** an operation is in progress and is being shown in the panel
- **THEN** the indicator SHALL update without a manual reload while it runs, and once it finishes the
  indicator SHALL stop updating and resolve to the corpus's normal status

#### Scenario: An idle corpus does not poll

- **WHEN** a corpus has no operation in progress
- **THEN** its status indicator SHALL render statically and SHALL NOT poll for updates

### Requirement: Review and recover changed or missing files

The panel SHALL provide a per-collection **review** view, reachable directly from the dashboard,
that focuses the operator on exactly the files that need attention and tells them what to do next.
The dashboard collection card's issue count (and the collection-detail "changed / missing" stat)
SHALL be a visible, clickable link to that collection's review view. The review view SHALL list
each `missing` file and each WORM `modified` file for the collection, and for each SHALL show what
happened (missing vs modified) with its last-seen / detected time, its size, and whether the file
was notarized. The review view SHALL let the operator acknowledge an individual file's event,
acknowledge all open events, and accept (re-baseline) the collection, reusing the existing
acknowledge/accept behavior and refreshing the "need action" count and sidebar alert badge in place
without a full page reload. The review view SHALL provide **recovery guidance that assumes no
particular backup tool**: a copyable list of the affected file paths and tool-neutral recovery
instructions; for files that were notarized it SHALL note that their OpenTimestamps proof of prior
existence survives. All review and recovery actions SHALL be scoped to the current user's own
collections.

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with a missing/modified indicator, its last-seen or
  detected time, its size, and whether it was notarized

#### Scenario: Acknowledge a file from the review view

- **WHEN** the user acknowledges a file's event from the review view
- **THEN** the event SHALL be marked acknowledged and the "need action" count and sidebar alert
  badge SHALL refresh in place without a full page reload

#### Scenario: Bulk accept and acknowledge-all from the review view

- **WHEN** the user triggers Accept or Acknowledge-all from the review view
- **THEN** the action SHALL reuse the existing accept/acknowledge behavior scoped to the user's own
  collections, and the view SHALL refresh to reflect the cleared issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  survives

#### Scenario: Nothing to review

- **WHEN** the user opens the review view for a collection with no missing or modified files
- **THEN** the view SHALL render an "all clear" empty state and SHALL offer no acknowledge/accept
  actions

