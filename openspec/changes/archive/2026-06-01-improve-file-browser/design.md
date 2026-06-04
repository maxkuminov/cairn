## Context

The corpus file browser is server-rendered Jinja2 + htmx (`corpus_detail.html` +
`partials/file_table.html`), backed by `corpora_svc.query_files` and the `/corpus/{id}/files` htmx
endpoint. Today it:

- orders strictly by `relpath` ascending (`query_files` has no sort parameter);
- renders no pagination controls — the `/files` route accepts `page`, but nothing in the template
  drives it, so only page 0 (the alphabetically-first 50 rows) is ever reachable;
- shows the notarization **state** as a badge only, with no date — even though `files.ots_stamped_at`
  is stored.

Constraints (DESIGN.md §1, §3, §5): a corpus can hold ~186k files, so search/sort/pagination MUST
stay server-side (never materialize the full set); SQLite is the single index and the scanner is the
single writer; the panel is minimal-JS htmx. The Bitcoin block "existed-by" date is **not** stored —
it is only obtainable via an `ots verify` call (DESIGN.md §6), so it cannot be shown per row.

Relevant stored timestamps on `files`: `first_seen`, `last_checked` (every scan), `last_changed`
(creation + every content change — verified always-set for tracked files), `ots_stamped_at` (set when
stamped). `last_checked` updates on *every* scan, so it is a poor "recency" key (all rows cluster);
`last_changed` reflects genuine file activity and is the right default-sort key.

## Goals / Non-Goals

**Goals:**
- Let the user sort the file list by the columns that matter (path, size, modified, notarized,
  last-checked), ascending or descending, server-side.
- Default to newest-activity-first so recently-changed files are visible on load.
- Add real pagination navigation that preserves search + filter + sort.
- Surface each file's notarization timestamp prominently, deep-linking complete proofs to verify.
- Keep the whole thing server-side, additive (no schema change), and within the existing htmx flow.

**Non-Goals:**
- Fetching/showing the Bitcoin block date per row (verify-only; deep-linked instead).
- Client-side sorting/pagination or infinite scroll.
- New DB columns; new indexes are considered but deferred (see Risks).
- Changing search semantics, the status-filter set, or accept/scan/stamp behaviour.

## Decisions

### 1. Sort as a server-side whitelist on `query_files`, not free-form column names

`query_files` gains `sort: str` and `direction: str` params mapped through a fixed dict to ORM
columns — e.g. `{"path": FileEntry.relpath, "size": FileEntry.size, "modified":
FileEntry.last_changed, "notarized": FileEntry.ots_stamped_at, "checked": FileEntry.last_checked}`.
Unknown keys fall back to the default (`modified`/desc). This keeps the query injection-proof and the
URL/query-param surface small and stable.

**Stable tiebreak:** every ordering appends `FileEntry.relpath` (and the column itself is already
unique-ish per corpus) as a secondary key so LIMIT/OFFSET paging is deterministic — without it,
rows with equal `last_changed`/`size`/`ots_stamped_at` could shuffle between pages. Nullable sort
keys (`ots_stamped_at`) use `.nulls_last()` regardless of direction so unstamped files don't
dominate the top of a descending notarized sort.

*Alternative considered:* clickable headers POSTing arbitrary `ORDER BY` — rejected (injection
surface, and overkill for five columns).

### 2. Default order changes to `last_changed` DESC

The proposal's headline fix. `last_changed` is set at file creation and on every content change
(never null for tracked files), so "newest first" is well-defined and needs no COALESCE. Path-A→Z
remains one click away via the Path header. This is the only user-visible behavioural change to an
existing view.

### 3. Clickable column headers as the sort UI (htmx), with a direction indicator

Each sortable header is an htmx `GET /corpus/{id}/files` trigger carrying `sort=<col>` and a
`dir` that flips when the active column is re-clicked (`asc`↔`desc`). The active column shows a
caret (`chevronD`/an up variant) using the existing icon set; inactive headers are plain. This
reuses the same `#file-table` `outerHTML` swap the search/filter already use, and `hx-include`
pulls the current `q`, `filter`, `sort`, `dir`, and `page` so all four compose. Headers are real
links/buttons (keyboard-reachable), not JS-only click handlers.

*Alternative considered:* a separate "Sort by ▾" dropdown — rejected; clickable headers are more
discoverable and map 1:1 to the columns already shown.

### 4. Pagination: explicit Prev/Next + "Page X of Y" in the footer

The footer computes `pages = ceil(total / page_size)` and renders Prev/Next as htmx triggers with
`page-1`/`page+1`, disabled at the ends, plus a "Page X of Y" label. `page_size` stays 50 (a
module constant, single source of truth shared by route + template). On a new search/filter/sort the
page resets to 0 (those triggers omit `page`, which defaults to 0). Offset pagination is fine here —
page depth is operator-driven browsing, not a hot loop.

*Alternative considered:* "Load more"/infinite scroll — rejected (non-goal; harder to deep-link and
to reason about "page N of M").

### 5. Notarization column shows date + badge; complete proofs deep-link to verify

`_file_view` adds `notarized_at` (humanized absolute date of `ots_stamped_at`, e.g. "30 May 2026"),
`modified_at` (humanized `last_changed`), and the existing `ots`/`id`. The template's notarization
cell renders the stamp date next to the state badge; for `ots == "complete"` the cell links to
`/verify?file={id}` (the existing deep-link target that verifies immediately and shows the
block-confirmed existed-by date). Unstamped rows and tripwire corpora (which hide the notarization
column entirely) fall back to a `modified_at` timestamp so no row is dateless. The list never calls
`ots verify` — honesty over completeness (DESIGN.md §6; we do not invent provenance).

We keep `last_checked` as its own right-aligned column (operational "freshness"), so the row carries
both the integrity-freshness signal and the notarization/identity timestamp.

## Risks / Trade-offs

- **[OFFSET pagination cost on deep pages over 186k rows]** → Acceptable: browsing is operator-driven
  and shallow in practice; the indexed `corpus_id` filter bounds the scan, and the default newest-first
  sort puts the interesting rows on page 0. If deep paging ever proves slow we can add keyset
  pagination later without changing the spec.
- **[No index on `last_changed` / `ots_stamped_at`]** → SQLite sorts these in memory per query. At
  186k rows within a single corpus this is tolerable for an interactive panel; adding composite
  indexes (`corpus_id, last_changed` etc.) is a cheap follow-up *if* profiling shows it, but it is a
  migration and is deferred out of this change (keeps it migration-free).
- **[`ots_stamped_at` is the calendar-submission date, not the block date]** → By design (DESIGN.md
  §6). The column labels/links make clear the confirmed existed-by date lives on the verify page;
  pending/incomplete proofs show the stamp date with the pending badge, not a confirmed date.
- **[Default-order change surprises existing users]** → Minor and desirable (the requested fix); Path
  A→Z is one click away and the active sort is always indicated.

## Migration Plan

No data migration — purely additive query params + template/CSS. Deploy is the standard flow
(commit → push → `make deploy`); **no `make migrate`** needed. Rollback is a code revert; nothing
persisted changes shape.

## Open Questions

None blocking. (Deferred, non-blocking: whether to add covering indexes for the sort columns — gated
on real profiling, tracked as a possible follow-up.)
