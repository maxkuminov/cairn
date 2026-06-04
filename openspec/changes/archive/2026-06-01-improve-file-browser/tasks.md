## 1. Service layer — sortable, deterministic query

- [x] 1.1 In `src/services/corpora.py`, define a sort whitelist mapping stable keys to columns:
      `path→relpath`, `size→size`, `modified→last_changed`, `notarized→ots_stamped_at`,
      `checked→last_checked`. Add a module constant for the default (`modified`, `desc`).
- [x] 1.2 Extend `query_files(...)` with `sort: str = "modified"` and `direction: str = "desc"`
      params; resolve unknown sort/direction to the default. Apply
      `.nulls_last()` to nullable keys (`ots_stamped_at`) in both directions so unstamped files
      never dominate a descending notarized sort.
- [x] 1.3 Always append `FileEntry.relpath` as a stable secondary order key so LIMIT/OFFSET paging
      is deterministic across requests; keep the existing `(rows, total)` return shape.

## 2. Routes — thread sort/dir/page and enrich the file view

- [x] 2.1 In `src/control_panel/routes.py`, add a single `PAGE_SIZE = 50` constant and use it in
      both `corpus_detail` and `corpus_files` (remove the duplicated literal).
- [x] 2.2 Add `sort` and `dir` query params to `corpus_files` (and seed defaults in
      `corpus_detail`); pass them into `query_files`; include `sort`/`dir`/`page` in the template
      context.
- [x] 2.3 Extend `_file_view` to add `notarized_at` (humanized absolute date of `ots_stamped_at`,
      or `None`), `modified_at` (humanized absolute date of `last_changed`), and keep `id`/`ots`
      so the template can build the verify deep-link.

## 3. Templates — sortable headers, notarized column, pager

- [x] 3.1 In `partials/file_table.html`, make each sortable column header an htmx
      `GET /corpus/{id}/files` trigger carrying `sort=<col>` and a `dir` that flips when the active
      column is re-activated; show a caret on the active column indicating direction. Use
      `hx-include` to carry the current `q`, `filter`, `sort`, `dir`.
- [x] 3.2 Add the prominent **Notarized** column: render the `notarized_at` date beside the existing
      `ots_badge`; for `f.ots == "complete"` link the cell to `/verify?file={{ f.id }}`. For
      unstamped files (and the tripwire `--no-ots` layout) fall back to showing `modified_at` so no
      row is dateless. Keep the right-aligned `Last checked` column.
- [x] 3.3 Replace the static footer with a pager: compute total pages from `files_total`/`page_size`,
      render Prev/Next as htmx triggers (`page-1`/`page+1`) disabled at the ends, plus a
      "Page X of Y" label; keep the "showing N of TOTAL" / "N matching" indicator. Ensure search,
      filter, and sort triggers reset to page 0.
- [x] 3.4 In `corpus_detail.html`, pass the new `sort`/`dir` through to the included partial and the
      search/filter `hx-include` lists so all four params compose on every interaction.

## 4. Styles

- [x] 4.1 In `src/control_panel/static/css/panel.css`, adjust `.file-grid` / `.file-grid--no-ots`
      column templates for the date-bearing Notarized column, add a sortable-header affordance
      (hover/active caret), and style the pager (Prev/Next buttons + page label) consistently with
      the existing `.table-footer`.

## 5. Tests

- [x] 5.1 In `tests/test_panel.py`, assert the default file list is ordered newest-activity-first
      (`last_changed` desc) with a stable path tiebreak.
- [x] 5.2 Assert sorting by an explicit column + direction (e.g. `sort=size&dir=asc`,
      `sort=notarized&dir=desc`) reorders rows, and an unknown sort/dir falls back to the default.
- [x] 5.3 Assert pagination navigation: page 2 returns the next slice, the page-of-total indicator
      is present, and `q`/`filter`/`sort` are preserved across pages.
- [x] 5.4 Assert a notarized file's row shows its stamp date and that a `complete` proof links to
      `/verify?file=<id>`; assert an unstamped/tripwire row falls back to the last-changed date.

## 6. Verify

- [x] 6.1 Run the test suite (`pytest`) and `ruff` (per `pyproject.toml`); fix any failures.
- [x] 6.2 Run `openspec validate improve-file-browser --strict` and confirm it passes.
- [x] 6.3 Manually exercise the corpus detail page (sort each column both directions, page through,
      confirm the Notarized date + verify deep-link and the tripwire fallback) before archiving.
