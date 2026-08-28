# Design: fix-ux-audit-sprint2

## Context

Five display-layer honesty gaps from the Aug-2026 audits (#36, #37, #38, #40, #41). Current
state per surface:

- `base.html` topbar: `<input placeholder="Search files, paths, hashes…">` with no form, no
  name, no handler — a silent no-op on every page.
- `/verify` (`routes.py`): `verify_page` supports `?file=<id>` preselect; `verify_search`
  (`GET /verify/search`, htmx partial) does an owner-scoped, escaped-LIKE, `limit(50)` relpath
  search via `_anchored_query` — which filters `ots_state IN ('incomplete','complete')`, so
  never-stamped (`none`) and queued (`pending`) files are invisible to search. `verify_page`'s
  "recent" list uses the same helper (limit 5).
- `_macros.html:107`: the proof badge is an `<a href="/verify?file=…">` only for
  `ots == "complete"`; all other states are plain pills.
- `collection_detail.html` stat row: Total / Matching baseline / Changed-missing /
  Anchored-to-chain. `c.counts.new` is computed and used elsewhere (baseline button confirm)
  but never displayed.
- `dashboard.html:13`: `POST /scan` form, button text "Run scan now", no confirm. The route
  scans every collection owned by the user.
- `partials/_collection_card.html:33`: the four-segment file-status bar has no `title` /
  `aria-label` / visible caption; the anchored line below it reads
  `"{complete_active:,} / {stampable:,}" anchored` where `stampable` deliberately counts only
  `status != 'missing'` files in `perfile` collections (sprint-1 design D5) — the denominator's
  meaning is stated nowhere on the card.

Constraints: server-rendered Jinja2 + htmx, minimal JS; server-side bounded search is mandatory
(collections hold ~200k files); sprint-1's typed verify outcomes and coverage identities
(D5/D13) must not be disturbed; no schema change.

## Goals / Non-Goals

**Goals:**

- Every control in the chrome either works or does not exist; every ratio names its denominator;
  every count the operator can act on is visible where they act.
- Reuse the existing bounded search and the existing verify card for all five fixes — no new
  query primitives beyond widening one filter.

**Non-Goals:**

- A real global-search feature (hash lookup, content search, results page). #36 resolves to
  "wire it to the verify search"; a richer search is future work.
- Any change to scan/stamp/verify behavior, coverage identities, or alert logic.
- Restyling beyond what the labels require.

## Decisions

### D1 — The topbar search is a plain GET form to `/verify?q=…` (no htmx, no live-search)

The box gains `<form action="/verify" method="get">` + `name="q"`. `verify_page` accepts
`q: str = Query("")`; when non-empty it runs the same `_anchored_query`-family search the htmx
partial uses and passes the results + query into `verify.html`, which seeds the existing search
input and results region (the htmx live search then takes over for refinement).

The placeholder / aria-label change from "Search files, paths, hashes…" to a wording that
promises only what the backend does (file names and paths — e.g. "Search files and paths…"):
advertising hash search over a relpath-LIKE backend teaches an operator that a pasted digest
"isn't tracked" when it was simply never looked up. A bounded digest lookup is future work.

*Why not htmx from the topbar?* The topbar exists on every page; a live-search dropdown there is
a new component with focus/keyboard/empty-state semantics — real design work for another change.
A GET navigation is honest, bookmarkable, and lands the user on the page whose whole purpose is
finding-and-checking a file. *Why /verify and not the collection file browser?* The issue names
it: the verify search works, is owner-scoped, and has an empty state; the file browser is
per-collection so a global box can't target one.

### D2 — Search covers every tracked file; the state filter moves from the query to the render

`_anchored_query`'s `ots_state IN ('incomplete','complete')` filter is correct for the *recent
anchored* list (its name and heading say "anchored") but wrong for *search*: it silently hides
exactly the files whose verify card carries actionable advice ("Not notarized yet — use Stamp
all"). Search (both `verify_search` and D1's initial results) drops the state filter and
searches all tracked files owned by the user; each result row shows its proof-state badge
(reusing the existing badge macro), so an unstamped row is visibly unstamped in the list. The
recent list keeps the anchored filter — its meaning is unchanged. Result rows stay clickable to
the same per-file verify flow for every state (consistent with D3).

Three properties close the holes the first spec round left open:

- **Blank stays default.** A blank or whitespace-only query (`GET /verify/search?q=` included)
  renders the anchored-only recent listing, never the widened search — the widened population is
  reachable only via a non-blank query, so clearing the input restores the page's default state
  instead of leaking unstamped rows under the "Recent proofs" heading.
- **Deterministic path order + disclosed truncation.** Search orders by `relpath ASC` (with a
  collection tiebreaker), not `ots_stamped_at DESC` — a recency order plus a silent 50-row cap
  makes an unstamped file that shares a searchable name with ≥50 stamped files unreachable by
  any query. The result partial states the true total (`COUNT(*)` alongside the capped page):
  "showing 50 of N matches — narrow your search" whenever total > cap. No pagination; a bounded
  search whose truncation is disclosed and whose order is path-deterministic makes every target
  reachable by refining, which is this page's job (finding one file to verify, not browsing).
- **State-neutral search copy on both render paths.** The searchable-count line, search heading,
  and no-match copy in `verify.html` + `partials/verify_results.html` describe *tracked files*,
  not anchored files/proofs — an operator whose files are all unstamped must not read "0 files
  with proofs" over a working search. Anchored wording survives only on the recent list.

*Alternative considered*: a second "include unstamped" toggle — rejected; the operator doesn't
know the distinction exists (that's the bug), so a toggle re-hides the same files behind a
control nobody will discover.

### D3 — The proof badge links in every state, to the same destination

`_macros.html` renders the `<a href="/verify?file={{ f.id }}">` wrapper unconditionally; the
`title` attribute names the state-appropriate action ("Verify this proof…" for complete;
"Check this file's notarization status" otherwise). `/verify?file=` + `POST /verify` already
handle all states with typed honest cards (sprint 1), so no route change is needed. Badge visual
styling is untouched — only the wrapper and title change.

### D4 — The `new` count is a first-class stat on the detail page

The stat row gains a "New files" mini-stat between "Matching baseline" and "Changed / missing":
value `c.counts.new`, sub-line "watched, not yet baselined" (the dashboard tile's vocabulary,
verbatim), accent color when > 0, muted zero otherwise (matching the row's existing zero
treatment). It renders for both `ots_mode` values (the count is a baseline concept, not a notary
one). No layout mechanism changes — one more `.mini-stat` card in the existing `stat-row` flex.

### D5 — The dashboard scan button states scope in its label and its confirm

Label becomes "Scan all collections"; the form gets a
`onsubmit="return confirm('Scan all N collections now?')"`-style light confirm where N is the
user's collection count (already available in the dashboard context; if not, the template
receives it — it is one `len()` of an already-fetched list, never a new query loop). This is the
same confirm mechanism the baseline button already uses, so no new JS pattern. Behavior of
`POST /scan` is unchanged.

The count is explicitly a **render-time snapshot**, not a binding: the POST scans the
collections owned at execution time, and no drift check is added. Binding the confirmed set to
the submission (a fingerprint, as the accept family does) would be ceremony without a hazard —
scans are read-only detection, so scanning a collection added since render is exactly what the
operator wants. The spec states this weakening so it is a decision, not an oversight.

### D6 — The segbar is labelled; the anchor ratio names its denominator

- The segbar `<div class="segbar">` gains `role="img"` + an `aria-label`/`title` of the form
  "File status: X ok, Y new, Z modified, W missing" (counts from the same `c.counts` the
  segments render from — one source, no drift).
- The anchored line's wording changes from `"{a} / {b} anchored"` to
  `"{a} / {b} present files anchored"`, and the `all_confirmed` branch keeps its existing
  identity (D5 of sprint 1: `complete_active == stampable > 0`) — only words change, no
  computed value changes.
- No second segbar (per Non-goals): the misread is "unlabelled bar adjacent to a ratio";
  labelling the bar and the ratio's denominator removes the ambiguity without adding a second
  visual channel to maintain.
- **A nonzero count always gets pixels**: segments get a CSS `min-width` (~3px) so a status
  whose share rounds to 0.00% (one modified file in 200k) still shows a sliver. Without it the
  new label and the bar can contradict each other — a fully-green bar whose own aria-label
  admits a missing file. Widths already only approximate proportions (2-dp rounding), so a
  minimum width changes nothing semantic.

## Risks / Trade-offs

- [Widened search (D2) surfaces `none`-state rows whose card suggests "Stamp all" on
  tripwire (`ots_mode='none'`) collections, where stamping isn't offered] → the verify card's
  never-stamped copy already branches on collection mode from sprint 1; verify in tests that the
  tripwire-mode card doesn't advise an impossible action, and adjust the card copy if it does.
- [Topbar GET search lands mid-workflow users on /verify, losing page context] → acceptable and
  honest: the box's placeholder says "Search files, paths, hashes…" and that is where files are
  searched; a silent no-op is strictly worse.
- [Confirm dialog on scan-all (D5) adds one click to a previously one-click action] →
  deliberate; the issue asks for a scope statement, and the action fans out a 1.6 TiB walk on
  the live host.
- [Aria/title counts on the segbar go stale if counts change while the page is open] → the whole
  card is a static render already; the badge poll only refreshes the op-status fragment. Same
  staleness as every other number on the card — not new.

## Migration Plan

Display-layer only; no migration. Deploy via the standard `make deploy`; no `make migrate`
needed (no revision). Rollback = redeploy previous image.

## Open Questions

_None — all five issues name their acceptable resolutions and this design picks one per issue._
