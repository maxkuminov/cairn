# Tasks: fix-ux-audit-sprint2

## 1. Verify search covers every tracked file (#41 backend, prerequisite for #36)

- [x] 1.1 In `src/control_panel/routes.py`, split the search query from the recent-proofs
  query: search (used by `GET /verify/search` and the new `?q=` initial render) drops the
  `ots_state IN ('incomplete','complete')` filter and matches all tracked files owned by the
  user (same escaped-LIKE, same 50-row cap), ordered by the unique key `relpath ASC,
  collection_id ASC`; it also returns the true total match count. The `verify_page` "recent"
  list keeps its `incomplete`/`complete` filter, its `ots_stamped_at DESC` order, and its
  meaning — and its heading/copy says recent *proofs*, never "anchored" (its population
  includes unconfirmed proofs; sprint-1 vocabulary).
- [x] 1.2 Blank stays default: a blank or whitespace-only query — including
  `GET /verify/search?q=` — renders the default recent-proofs listing, never the widened
  search.
- [x] 1.3 In `partials/verify_results.html`: render each result row's proof-state badge (reuse
  the existing badge macro) so unstamped rows are visibly unstamped; every row remains
  selectable into the per-file verify flow regardless of state; when total > cap, state
  "showing 50 of N matches", invite a narrower query, and name the per-collection file browser
  as the escape hatch; each row names its collection.
- [x] 1.4 State-neutral search copy on both render paths: the searchable-count line, the search
  heading, and the no-match copy in `verify.html` + `partials/verify_results.html` describe
  tracked files, not anchored files/proofs; proof-oriented wording ("recent proofs") is
  permitted only on the default recent-proofs listing, and anchored wording nowhere an
  unconfirmed proof can appear.
- [x] 1.5 Tests (`tests/test_ux_verify.py`): search returns `none`/`pending` files with state
  visible; blank query (both routes) renders the recent list only; truncation line appears with
  the true total when matches exceed the cap and results follow the unique
  relpath-then-collection order (including identical relpaths across collections); copy is
  state-neutral for an all-unstamped owner; recent list still excludes unstamped and is not
  captioned "anchored"; owner scoping unchanged in multi mode.

## 2. Top-bar global search (#36)

- [x] 2.1 Wrap the topbar input in `base.html` in a plain `GET` form to `/verify` with
  `name="q"` (no htmx); change the placeholder + aria-label to advertise only the supported
  search (file names and paths — no "hashes").
- [x] 2.2 `verify_page` accepts `q: str = Query("")`; when non-blank, run the task-1 search and
  seed `verify.html`'s search input + results region with the query, results, and total (the
  existing htmx live search takes over from there). Blank `q` renders the default page.
- [x] 2.3 Tests: `GET /verify?q=…` renders matching results and pre-fills the input; no-match
  query renders the empty state; blank `q` behaves as the default page; the topbar form and
  honest placeholder are in the base chrome; results are owner-scoped in multi mode.

## 3. Proof badge links in every state (#41 frontend)

- [x] 3.1 In `_macros.html`, render the badge's `/verify?file={{ f.id }}` link wrapper for every
  `ots` state, with a state-appropriate `title` (confirmed keeps its current wording; other
  states get a "check this file's notarization status" phrasing).
- [x] 3.2 Confirm the never-stamped verify card's guidance is honest for tripwire
  (`ots_mode='none'`) collections — it must not advise "Stamp all" where stamping is refused;
  adjust card copy if needed.
- [x] 3.3 Tests: badge is a link in all four states (file table + tree views); tripwire-mode
  never-stamped card does not advise an impossible action.

## 4. Collection detail new-count stat (#37)

- [x] 4.1 Add a "New files" mini-stat to `collection_detail.html`'s stat row (both `ots_mode`
  branches): value `c.counts.new`, sub-line using the dashboard tile's "watched, not yet
  baselined" vocabulary, accent color when > 0, muted zero otherwise.
- [x] 4.2 Tests (`tests/test_panel.py` or `tests/test_ux_dashboard.py` style): the stat renders
  with the count; zero renders muted; vocabulary matches the dashboard tile.

## 5. Dashboard scan-all labelling (#38)

- [x] 5.1 Relabel the dashboard scan button "Scan all collections" and add a light
  `confirm()` naming the user's collection count (count from data already in the dashboard
  context — no new query loop). The count is a render-time snapshot; the POST is unchanged and
  scans the collections owned at execution time (spec'd weakening, not an oversight).
- [x] 5.2 Tests: label text and confirm attribute (with the count) present in the dashboard
  render.

## 6. Collection card segbar + ratio wording (#40)

- [x] 6.1 In `partials/_collection_card.html`, give the segbar `role="img"` plus a
  `title`/`aria-label` of the form "File status: X ok, Y new, Z modified, W missing", built from
  the same `c.counts` values the segments are sized from.
- [x] 6.2 Give segments a CSS `min-width` (~3px) so a nonzero status whose share rounds to
  0.00% still renders a visible sliver — the bar must never contradict its own label.
  (Already present in `panel.css` since the initial release; now pinned by a test so the new
  label can never outlive it.)
- [x] 6.3 Reword the anchored ratio to name its denominator ("N / M present files anchored"),
  and reword the `all_confirmed` branch to name its population too ("all N present files
  anchored") — its computed identity and every other identity (sprint-1 D5/D13) unchanged.
- [x] 6.4 Tests: label present and counts agree with the rendered segments; a
  one-modified-in-many collection renders a nonzero-width segment; ratio wording names present
  files; an all-confirmed collection that also has missing files names its population and never
  reads as a bare "all confirmed".

## 7. Verification

- [x] 7.1 Full test suite green (`.venv/bin/pytest -q`).
- [x] 7.2 `openspec validate fix-ux-audit-sprint2 --strict` passes.
- [x] 7.3 Grep check: no template still renders a bare (formless) topbar search input; no badge
  render path bypasses the link wrapper; no remaining "hashes" claim in search chrome; no
  anchored-only wording on a search surface.
