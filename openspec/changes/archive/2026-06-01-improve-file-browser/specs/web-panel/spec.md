## RENAMED Requirements

- FROM: `### Requirement: Corpus file list is searched, filtered, and paginated server-side`
- TO: `### Requirement: Corpus file list is searched, filtered, sorted, and paginated server-side`

## MODIFIED Requirements

### Requirement: Corpus file list is searched, filtered, sorted, and paginated server-side

The corpus detail page SHALL list files via server-side search, status filtering, sorting, and
pagination — it SHALL NOT render the entire file set (corpora can hold ~186k files). Search SHALL
match the relative path; the filter SHALL offer All / Issues / New / OK.

Sorting SHALL be server-side over a fixed whitelist of columns — relative path, size, modified time
(`last_changed`), notarization time (`ots_stamped_at`), and last-checked time — each toggleable
ascending or descending. An unrecognized sort or direction SHALL fall back to the default. The
default order SHALL be newest-activity-first (`last_changed` descending) so the most recently
changed files appear first on load. Every sort SHALL apply a stable secondary tiebreak (relative
path) so pagination is deterministic. The active sort column and direction SHALL be indicated in
the table header.

Pagination SHALL expose navigation (previous / next) and a current-page-of-total-pages indicator,
returning at most one page of rows per request. The active search query, status filter, and sort
SHALL be preserved across page changes and across one another.

Each file row SHALL prominently display a timestamp. For a notarized file the row SHALL show the
OTS stamp date (`ots_stamped_at`) together with the notarization-state badge; a `complete` proof's
notarization cell SHALL deep-link to the verify page for the block-confirmed existed-by date. The
list SHALL NOT fabricate or fetch the Bitcoin block date per row. For an unstamped file, or a
tripwire (`ots_mode='none'`) corpus that hides the notarization column, the row SHALL fall back to
showing the file's last-changed date. The footer SHALL report how many of the total are shown.

#### Scenario: Only a page of results is returned

- **WHEN** a corpus has more files than one page and the user loads or searches the file list
- **THEN** the response SHALL contain at most one page of rows plus a "showing N of TOTAL"
  indicator, never the full list

#### Scenario: Filter to issues

- **WHEN** the user selects the Issues filter
- **THEN** only files with status `modified` or `missing` SHALL be listed

#### Scenario: Default order is newest activity first

- **WHEN** the user opens a corpus file list without choosing a sort
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
