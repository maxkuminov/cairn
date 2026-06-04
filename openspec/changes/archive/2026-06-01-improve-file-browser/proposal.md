## Why

The corpus file browser (DESIGN.md §5 — "corpus detail · file list with status · per-file OTS
state") is the only place to browse what Cairn tracks, but it is effectively stuck on the
alphabetically-first 50 rows: `query_files` always orders by `relpath`, and although the
`/files` endpoint accepts a `page` param, the table renders **no pagination controls**, so nothing
ever drives it past page 0. There is no way to sort, so a recently-changed or recently-notarized
file is invisible unless its path happens to sort first. And for a tool whose whole point is
trustless *"existed-by-date"* proofs (DESIGN.md §6), the file's timestamp is reduced to a state
badge with **no date at all** — the notarization moment, the most valuable fact per row, is never
shown.

## What Changes

- **Sortable file list.** `query_files` gains a server-side `sort` + `dir` whitelist (path, size,
  modified=`last_changed`, notarized=`ots_stamped_at`, last-checked) with a stable secondary
  tiebreak so pagination is deterministic. The table column headers become click-to-sort controls
  with an active-direction indicator. The **default order changes to newest-activity-first**
  (`last_changed` descending) so the latest files are visible on load — directly fixing "no way to
  sort by latest".
- **Real pagination.** The file-table footer gains Prev / Next controls and a "Page X of Y"
  indicator that page through the full result set server-side (the existing `page` param, now
  driven by UI), preserving the active search, filter, and sort across page changes. Corpora can
  hold ~186k files (DESIGN.md §1, §5), so paging stays server-side — the full list is never
  rendered.
- **Prominent notarization timestamp.** A new **Notarized** column shows each file's OTS stamp date
  (`ots_stamped_at`) as an absolute date alongside the existing anchored/pending badge. A complete
  proof deep-links to the verify page (DESIGN.md §6) for the block-confirmed existed-by date — the
  list never fabricates or fetches the Bitcoin block date per row. Unstamped files and tripwire
  (`ots_mode='none'`) corpora fall back to showing the file's last-changed date, so every row
  carries a meaningful timestamp.
- **Everything composes.** Search + status filter + sort + page are all preserved together across
  every htmx interaction.

## Capabilities

### New Capabilities
<!-- none — this extends an existing panel capability -->

### Modified Capabilities
- `web-panel`: the "Corpus file list is searched, filtered, and paginated server-side" requirement
  is extended with server-side sorting (default newest-activity-first), explicit pagination
  navigation, and a prominent per-file notarization timestamp column that deep-links to verify for
  the block-confirmed date.

## Impact

- **Code**: `src/services/corpora.py` (`query_files` gains `sort`/`dir` params, a column whitelist,
  and a stable tiebreak); `src/control_panel/routes.py` (`corpus_detail` + `corpus_files` thread
  `sort`/`dir`/`page`; `_file_view` adds the notarized + modified dates and a verify deep-link
  flag); `src/control_panel/templates/partials/file_table.html` (sortable headers, Notarized
  column, pagination footer); `corpus_detail.html` (pass-through of the new params + hx-includes);
  `src/control_panel/static/css/panel.css` (file-grid column tweak, sort-header affordance, pager).
- **Data**: none — purely additive over existing `files` columns (`last_changed`, `ots_stamped_at`,
  `ots_state`). No schema change, **no Alembic migration**, no `make migrate` step.
- **Dependencies / config**: none.
- **Behaviour**: the initial sort changes from path-A→Z to newest-first; all existing search/filter
  behaviour is unchanged. Sorting/paging are read-only and cannot affect scan/accept/stamp.

## Non-goals

- **No per-row Bitcoin block date.** The block-confirmed existed-by date requires an `ots verify`
  call (DESIGN.md §6); running it per row over ~186k files is infeasible. The list shows the stored
  stamp date and deep-links complete proofs to the existing verify page for the confirmed date.
- **No client-side sort/paginate.** Server-side stays mandatory (DESIGN.md §5, §3 — the DB is the
  index; never materialize the full set).
- **No infinite scroll** — an explicit, accessible Prev/Next pager instead.
- **No schema/index migration**, no new columns, no change to search matching, the status-filter
  set, or the accept/scan/stamp actions.
