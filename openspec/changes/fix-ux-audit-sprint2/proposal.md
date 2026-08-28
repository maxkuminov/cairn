# Proposal: fix-ux-audit-sprint2

## Why

Five open panel issues from the Aug-2026 UX audits and the post-deploy `user-representative`
passes (#36, #37, #38, #40, #41) share one defect class the sprint-1 batch was built around:
**a panel surface that misstates — or silently withholds — what it does or what a number means.**
The panel is the operator's window onto a trust claim (DESIGN.md §1, §5), so a control that fails
silently or a bar that reads as the wrong ratio erodes exactly the confidence the product exists
to provide. All five are display-layer, schema-free, and small; batching them mirrors
`fix-ux-audit-sprint1`.

## What Changes

- **#36 — the global top-bar search box works.** The input in `base.html` (the most prominent
  control in the chrome) currently has no form, no `name`, no `hx-*` — typing and pressing Enter
  does nothing, silently. It becomes a real search: submitting navigates to the Verify page with
  the query applied to the existing server-side file search (`/verify?q=…` pre-runs the
  `/verify/search` lookup, which already has bounded results and an empty state). No new search
  engine; the box is wired to the search that exists.
- **#41 — every proof-state badge reaches the verify card.** `_macros.html` links the
  file-browser proof badge to `/verify?file=…` only when `ots == "complete"`; `pending` /
  `incomplete` / `none` render as inert pills, so the never-stamped card's own guidance ("use
  Stamp all") is unreachable except by hand-typed URL. The badge becomes a link in **every**
  state — the verify card already renders all of them honestly (sprint-1 typed outcomes).
  `/verify` search results likewise stop excluding unstamped files (today filtered to
  `incomplete`/`complete` only), so #36's global search can actually find any tracked file.
- **#37 — the collection detail page states the `new` count.** The dashboard gained a "New
  files — watched, not yet baselined" tile, but the detail page (where the operator acts) still
  shows only Total / Matching baseline / Changed-missing — the `new` remainder is visible nowhere
  except inside the baseline button's `confirm()`. A stat for `counts.new` joins the detail
  stat row, using the dashboard tile's vocabulary.
- **#38 — the dashboard scan button states its scope.** "Run scan now" POSTs `/scan` for ALL of
  the user's collections with no statement of scope and no confirmation. It is relabelled
  "Scan all collections" with a light confirm naming the count. Behavior is unchanged (scans are
  read-only detection).
- **#40 — the collection-card segbar is labelled and the anchor ratio names its denominator.**
  The unlabelled file-status segbar (title/aria-label absent) sits directly above the
  notarization ratio, so "100% green bar over 6.5% anchored" reads as a contradiction. The bar
  gains a title + aria-label naming it a file-status breakdown, and the anchored ratio is worded
  over its real denominator ("N / M present files"), so the deliberate exclusion of `missing`
  files from the denominator (design D5 of sprint 1) stops being an unexplained gap.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `web-panel`: five requirement-level deltas — the global search control must perform a search;
  proof badges must link to the verify surface in every proof state (and verify search must not
  hide unstamped files); the collection detail stat row must disclose the `new` count; the
  fleet-wide scan control must state its scope before acting; the collection-card status bar must
  be labelled and the anchor ratio must name its denominator.

## Impact

- **Templates**: `base.html` (search form), `_macros.html` (badge link-in-every-state),
  `collection_detail.html` (new-count stat), `dashboard.html` (scan button label + confirm),
  `partials/_collection_card.html` (segbar labelling, ratio wording),
  `partials/verify_results.html` (unstamped rows render honestly).
- **Routes**: `src/control_panel/routes.py` — `verify_page` accepts a `q` parameter (initial
  search results server-rendered); `_anchored_query` widens to include `none`/`pending` states
  for search (recent-anchored list keeps its current meaning). No new routes, no schema change,
  no migration.
- **Tests**: extend `tests/test_ux_verify.py` / `tests/test_ux_dashboard.py` / `tests/test_panel.py`
  in their existing style.
- **Out of band**: none — no scanner, OTS, scheduler, or CLI surface is touched.

## Non-goals

- No real "global search" engine (cross-collection hash lookup, content search, a search results
  page of its own). The box routes to the one bounded search that exists; anything more is its
  own change (#36 explicitly allows this resolution).
- No bulk or per-collection scan scoping changes on the dashboard button (#38 is a labelling
  gap; the fleet-scan behavior itself is fine).
- No second notarization segbar on the card unless the labelling alone proves insufficient —
  the minimal honest fix is labels + denominator wording (#40 offers both options).
- No proof/notary behavior changes of any kind (stamping, verify outcomes, coverage identities
  from sprint 1 stay untouched).
- Not touching #39 (moved-file `ots_path`), which is queued as its own change with the full
  adversarial gate.
