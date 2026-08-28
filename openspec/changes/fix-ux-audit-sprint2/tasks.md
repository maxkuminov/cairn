# Tasks: fix-ux-audit-sprint2

## 1. Verify search covers every tracked file (#41 backend, prerequisite for #36)

- [ ] 1.1 In `src/control_panel/routes.py`, split the search query from the anchored-recency
  query: search (used by `GET /verify/search` and the new `?q=` initial render) drops the
  `ots_state IN ('incomplete','complete')` filter and matches all tracked files owned by the
  user (same escaped-LIKE, same `limit(50)`, keep `ots_stamped_at DESC NULLS LAST` ordering);
  the `verify_page` "recent" list keeps the anchored-only filter and its meaning.
- [ ] 1.2 Render each search-result row's proof-state badge in `partials/verify_results.html`
  (reuse the existing badge macro) so unstamped rows are visibly unstamped; every row remains
  selectable into the per-file verify flow regardless of state.
- [ ] 1.3 Tests (`tests/test_ux_verify.py`): search returns `none`/`pending` files with state
  visible; recent list still excludes them; owner scoping unchanged.

## 2. Top-bar global search (#36)

- [ ] 2.1 Wrap the topbar input in `base.html` in a plain `GET` form to `/verify` with
  `name="q"` (no htmx).
- [ ] 2.2 `verify_page` accepts `q: str = Query("")`; when non-empty, run the task-1 search and
  seed `verify.html`'s search input + results region with the query and results (the existing
  htmx live search takes over from there). Empty `q` renders the default page.
- [ ] 2.3 Tests: `GET /verify?q=…` renders matching results and pre-fills the input; no-match
  query renders the existing empty state; `?q=` empty behaves as the default page; results are
  owner-scoped in multi mode.

## 3. Proof badge links in every state (#41 frontend)

- [ ] 3.1 In `_macros.html`, render the badge's `/verify?file={{ f.id }}` link wrapper for every
  `ots` state, with a state-appropriate `title` (confirmed keeps its current wording; other
  states get a "check this file's notarization status" phrasing).
- [ ] 3.2 Confirm the never-stamped verify card's guidance is honest for tripwire
  (`ots_mode='none'`) collections — it must not advise "Stamp all" where stamping is refused;
  adjust card copy if needed.
- [ ] 3.3 Tests: badge is a link in all four states (file table + tree views); tripwire-mode
  never-stamped card does not advise an impossible action.

## 4. Collection detail new-count stat (#37)

- [ ] 4.1 Add a "New files" mini-stat to `collection_detail.html`'s stat row (both `ots_mode`
  branches): value `c.counts.new`, sub-line using the dashboard tile's "watched, not yet
  baselined" vocabulary, accent color when > 0, muted zero otherwise.
- [ ] 4.2 Tests (`tests/test_panel.py` or `tests/test_ux_dashboard.py` style): the stat renders
  with the count; zero renders muted; vocabulary matches the dashboard tile.

## 5. Dashboard scan-all labelling (#38)

- [ ] 5.1 Relabel the dashboard scan button "Scan all collections" and add a light
  `confirm()` naming the user's collection count (count from data already in the dashboard
  context — no new query loop).
- [ ] 5.2 Tests: label text and confirm attribute (with the count) present in the dashboard
  render.

## 6. Collection card segbar + ratio wording (#40)

- [ ] 6.1 In `partials/_collection_card.html`, give the segbar `role="img"` plus a
  `title`/`aria-label` of the form "File status: X ok, Y new, Z modified, W missing", built from
  the same `c.counts` values the segments are sized from.
- [ ] 6.2 Reword the anchored ratio to name its denominator ("N / M present files anchored");
  the `all_confirmed` branch and all computed identities (sprint-1 D5/D13) unchanged.
- [ ] 6.3 Tests: label present and counts agree with the rendered segments; ratio wording names
  present files; "all confirmed" branch unchanged.

## 7. Verification

- [ ] 7.1 Full test suite green (`.venv/bin/pytest -q`).
- [ ] 7.2 `openspec validate fix-ux-audit-sprint2 --strict` passes.
- [ ] 7.3 Grep check: no template still renders a bare (formless) topbar search input; no badge
  render path bypasses the link wrapper.
