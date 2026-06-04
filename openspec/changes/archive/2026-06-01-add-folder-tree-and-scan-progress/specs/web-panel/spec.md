## MODIFIED Requirements

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

## ADDED Requirements

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
