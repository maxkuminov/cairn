# Tasks — fleet review + run health

Two disjoint implementation slices — **A** = the fleet review page, the tile's destination and the
feed ordering (#27 / #18 / #25); **B** = the health pill, partial-run visibility and the #24
remainder (#28 / #29 / #24) — preceded by one **shared prep** step that owns the schema, and
followed by integration + the gates.

**Read `design.md` §D9 (file ownership) before touching `routes.py`.** It is the one file both slices
name, and ownership is **by function**. Neither slice reformats it; neither edits the other's
functions. `_macros.html` belongs to Slice B.

Standing guardrails for every slice:

- **Exactly one Alembic revision (`0012`) exists in this change, and it is written in shared prep,
  before fan-out.** No slice creates a migration. If a task seems to need another column, stop and
  escalate.
- **Honour `proposal.md`'s Non-goals and #12's "Do NOT implement these".** In particular: the event
  feed is **never** default-filtered to unacknowledged events (rejected fix 2 — order, do not
  filter); the sidebar badge stays in every OOB swap (rejected fix 8); `_collection_status` and
  `_alert_badge_count` keep counting acknowledged-missing files (rejected fix 1).
- **No cross-collection mutation.** No fleet bulk accept, no fleet bulk acknowledge, no new
  `_FP_FORMS` entry, no new destructive route (design D1/D3).
- **Do not bundle findings from other audit issues.** If you spot one, note it; do not fix it here.
- **`_read_population` is read once per collection per render** and everything the page shows or
  authorizes is a slice of that snapshot (design D1). Never re-read to fill in a count.
- **Nothing in this change asserts a status it has not computed** — that is the defect class all
  five issues belong to. A pill with no data reads "Checking…", not "Healthy".

## 1. Shared prep (once, on the base branch, before fan-out)

- [ ] 1.1 Confirm the working tree is on the intended base: `src/control_panel/routes.py` contains
  `_read_population`, `_review_item` and `mismatch_blame`; `src/services/collections.py` contains
  `reclaim_stale_claim`; `openspec/changes/archive/2026-08-27-add-alert-deep-links/` exists. If any
  is missing, the branch is stale — stop.
- [ ] 1.2 Confirm the Alembic head is `0011_proof_provenance_and_restored_changed`
  (`ls alembic/versions/`). This change's revision is **0012**; if the head has moved, renumber
  before writing it.
- [ ] 1.3 Write `alembic/versions/0012_run_error_visibility.py`, purely additive — **no table
  rebuild, no CHECK change**:
  - `runs.errors` — `op.add_column("runs", sa.Column("errors", sa.Integer(), nullable=False,
    server_default="0"))`.
  - `runs.error_sample` — `op.add_column("runs", sa.Column("error_sample", sa.Text(),
    nullable=True))`.
  - `downgrade()` drops both. No data is lost that is not derivable from the logs, so the downgrade
    does **not** need `0011`'s refusal guard.
- [ ] 1.4 `src/models/db.py`: add `Run.errors: Mapped[int]` (default 0, not nullable) and
  `Run.error_sample: Mapped[str | None]`, each with a comment stating (a) that `errors` is the count
  that already decides `partial` and (b) that `error_sample` is a **capped JSON array of ASCII-safe
  diagnostic renderings, not paths** — see design D6 — and must never be fed to a filesystem call.
- [ ] 1.5 Apply `0012` against a **populated** scratch DB: assert existing `runs` rows survive with
  `errors = 0` / `error_sample = NULL`, then `alembic downgrade` and assert both columns are gone
  with every other row intact.
- [ ] 1.6 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green, `alembic upgrade head` clean.
- [ ] 1.7 Commit shared prep on the base branch **before** creating any worktree.

## 2. Slice A — the fleet review page, the tile's destination, the feed ordering (#27 / #18 / #25)

Files owned: `routes.py` (`dashboard`, `_event_feed`, `ack_event`, the new fleet route + limits, and
the `from` parameter on `collection_file_accept` / `_guarded_accept` **only**), new
`templates/review_fleet.html`, `templates/dashboard.html`,
`templates/partials/review_ack_row.html`, `templates/partials/review_row.html`,
`tests/test_panel.py` (fleet + feed tests only).

### The page

- [ ] 2.1 Add `FLEET_ROW_LIMIT = 500` and `FLEET_COLLECTION_ROW_LIMIT = 100` beside the existing
  `REVIEW_ROW_LIMIT`, with the two-failure-modes comment from design D11.
- [ ] 2.2 Add `GET /review` → `fleet_review()`, page id `"review"`. For each collection from
  `collections_svc.list_collections(session, user_id=user.id)`: one
  `_read_population(session, collection, "review")`; skip collections whose `pop.files` is empty.
  **One read per collection, and every number the group shows comes out of it** (design D1).
- [ ] 2.3 Per group, build rows exactly as `collection_review` does: sort `missing` first then
  `modified`, path-ordered within each; truncate to `FLEET_COLLECTION_ROW_LIMIT` and to the
  remaining share of `FLEET_ROW_LIMIT`; `_latest_events_by_file` for the rendered ids; one
  `_review_item(..., fp=_population_fingerprint(collection, _narrow(pop, "accept-file", …,
  file_id=f.id)))` per row. **No new `_FP_FORMS` entry** — `accept-file` is already per-row and
  per-collection.
- [ ] 2.4 Group ordering `missing DESC, modified DESC, name`; per-group counts and `review_open`
  from the snapshot, never from the truncated row list. A group past the total cap is still rendered
  with its header, counts and link — with zero rows (design D11).
- [ ] 2.5 `templates/review_fleet.html`: the panel shell, a page header naming the totals, one
  group per collection (header = name, `N missing · M modified`, the `_review_open_pill` with id
  `review-open-pill-{{ c.id }}`, and a **"Review all in {name} →"** link to
  `/collection/{id}/review`), then `{% include "partials/review_row.html" %}` per row, and a
  `+N more in this collection` note where truncated. **No bulk-verb form anywhere on this page**
  (design D3) — one comment in the template says so and says why, so it is not "helpfully" added
  later. One line pointing at the per-collection review page for recovery guidance; no copy-paths
  control here.
- [ ] 2.6 Empty state: "No open issues across your collections" — distinct from the no-collections
  state, which links to `/collection/new`.
- [ ] 2.7 Sidebar: no new nav entry (the page is reached from the tile, the badge and the cards);
  `_base_context(..., page="review")` must not light up another nav item.

### Per-row actions from the fleet page

- [ ] 2.8 `partials/review_row.html`: the per-row accept form's `action` gains an optional
  `?from=fleet` (from a `row_from` variable defaulting to empty, so the collection page's markup is
  unchanged); the Mark-reviewed button's `hx-post` gains `?view=fleet` the same way.
- [ ] 2.9 `collection_file_accept` + `_guarded_accept`: accept a `from` query value **whitelisted to
  the two literals** `"fleet"` / anything-else (design D2), and use it as the base of **both** the
  success redirect and the `?stale=1` refusal redirect. The verb performed stays a route constant;
  `from` selects a destination and nothing else. Assert this in a test.
- [ ] 2.10 `ack_event`: add the `view == "fleet"` branch. It renders the same
  `partials/review_ack_row.html` as `view == "review"` — including the post-ack single-snapshot
  re-mint of the row and its fingerprint, unchanged — with `pill_id = f"review-open-pill-{collection.id}"`
  and `review_open` scoped to that collection.
- [ ] 2.11 `partials/review_ack_row.html`: parameterize the OOB pill id
  (`{% set pid = pill_id|default("review-open-pill") %}`) so the collection review page's rendered
  markup is byte-for-byte unchanged (design D10). Keep the `#sidebar-alert-badge` OOB swap.

### The tile and the feed

- [ ] 2.12 `dashboard()`: `issues_href` for two-or-more affected collections becomes `/review`
  (single affected collection keeps its direct review link; zero keeps `None` and the inert `<div>`).
  Delete the now-false "NEVER `/review` — it is a 404" comment and replace it with the rule.
- [ ] 2.13 `_event_feed()`: add `Event.acknowledged_at.is_(None).desc()` **before**
  `Event.detected_at.desc()`. Do **not** add a `WHERE` clause; do not change the `limit(20)`; do not
  change how `open_events` / `alert_count` are counted (real COUNTs over the whole population).
  Comment it with #12's rejected fix 2 and the `user-representative` finding it closes.

### Tests

- [ ] 2.14 `/review` with issues in two collections renders both groups, missing rows before
  modified within a group, and each row's accept form carries a non-empty `population_fp`.
- [ ] 2.15 `/review` renders **no** bulk accept/acknowledge form (assert the absence of
  `/review/ack-all`, `/review/adopt-changed`, `/review/stop-tracking` in the body).
- [ ] 2.16 A per-row accept posted with `from=fleet` and a **stale** fingerprint redirects to
  `/review?stale=1` and mutates nothing; with a valid fingerprint it accepts the one file and
  redirects to `/review`.
- [ ] 2.17 Acknowledging from the fleet view swaps the row, OOB-refreshes
  `review-open-pill-{cid}` and the sidebar badge, and leaves the group's missing/modified counts
  unchanged.
- [ ] 2.18 Feed ordering: with 25 born-acknowledged informational events newer than 3 unacknowledged
  `missing` events, all 3 unacknowledged events appear in the 20-row feed, at the top; and a feed
  with **no** unacknowledged events still renders the informational rows (no filtering).
- [ ] 2.19 Dashboard tile: two affected collections → `href="/review"`; one → that collection's
  review page; zero → a non-interactive element.
- [ ] 2.20 Scoping: in `multi` mode, `/review` for user A shows none of user B's collections or rows.

## 3. Slice B — health pill, partial-run visibility, the #24 remainder (#28 / #29 / #24)

Files owned: `src/services/scheduler.py`, `src/services/scanner.py`, `src/main.py`, `routes.py`
(`_collection_view`, `_base_context`, `health_pill`, `collections_list`, `verify_run`),
`templates/_macros.html`, `templates/base.html`, `templates/settings.html`,
`templates/collection_detail.html`, `templates/partials/health_pill.html`,
`templates/partials/_collection_card.html`, `templates/partials/verify_result.html`,
`tests/test_scheduler.py`, `tests/test_scanner.py`, `tests/test_panel.py` (health/run/verify tests
only).

### #28 — the health pill

- [ ] 3.1 `scheduler.py`: `CollectionHealth` gains `id: int` (first field or beside `name`);
  `compute_health` populates it. No classification change.
- [ ] 3.2 `main.py`: `/healthz`'s per-collection objects gain `"id"` alongside `name`/`state`/
  `last_scan_age_seconds`. Additive only — no existing key changes name, type or meaning.
- [ ] 3.3 `health_pill` route: return `status` **and** the stale-collection count (derived from the
  same `HealthReport`, never a second query).
- [ ] 3.4 `partials/health_pill.html`: becomes `<a href="/collections">` with a **visible** label —
  `Healthy` / `Degraded · N collection(s)` / `Error` — keeping the `hx-get`/`hx-swap` poll and the
  `/healthz` mono suffix. Accepts `status=None` → a neutral **`Checking…`** state with no colour
  claim (used by `base.html` before the first poll answers). Never a bare `title` as the only
  elaboration (design D5).
- [ ] 3.5 `base.html:93-98`: delete the hand-copied hardcoded-green pill; `{% include
  "partials/health_pill.html" %}` with `status=None` and the `load, every 30s` trigger. Do **not**
  call `compute_health` from `_base_context`.
- [ ] 3.6 `settings.html:205-206`: remove the static `pill--ok` "Healthy" pill from the
  health-endpoint card. Leave the endpoint documentation and the copy control as they are.
- [ ] 3.7 `dashboard()` and `collections_list()`: one `compute_health` call per render; attach
  `health_state` (`fresh`/`pending`/`stale`) to each collection view by **id**.
- [ ] 3.8 `_collection_card.html`: a **stale** marker in the "Last scan" meta cell when
  `health_state == "stale"`, naming the state in words ("scan overdue"), not by colour alone.

### #29 — partial / failed / interrupted runs

- [ ] 3.9 `scanner.py`: persist `summary.errors` to `runs.errors` and a capped JSON array to
  `runs.error_sample` at the same finalize site that sets `result`. Cap at 20 entries; entries are
  the **ASCII-safe diagnostic rendering** already used by the un-storable WARNING
  (`os.fsencode(relpath)` → `repr`/`backslashreplace`) — writing the raw name reproduces the very
  `UnicodeEncodeError` this column exists to report (design D6). Record a sample entry at all three
  `summary.errors += 1` sites (`:413` un-storable, `:424` `stat` OSError, `:563` hash OSError), each
  naming its reason.
- [ ] 3.10 Do **not** touch the last-ditch `UPDATE runs SET result='error', finished=…` fallback in
  the finalize path; it may leave `errors` at 0 (design D6, accepted).
- [ ] 3.11 `_collection_view`: keep the existing freshness query (`kind='scan'`,
  `result IN ('ok','partial')`) **exactly as it is** and expose `last_result` + `last_errors` +
  `last_error_sample` from that row. Add a **second** scalar for the newest `kind='scan'` run in any
  terminal state (`ok`/`partial`/`error`/`interrupted`) → `latest_run_result`. Comment why the two
  are separate (design D7).
- [ ] 3.12 `_macros.html`: one `run_health_note(c)` macro rendering the single line used everywhere:
  - completed `partial` → warn-toned `partial — N file(s) skipped`, with the sample (capped, shown
    as diagnostic text) behind a details/tooltip;
  - newest run `interrupted` and newer than the last completed scan → **muted, never red**: "a later
    scan was interrupted (app restart or reclaimed claim); it will re-run on the next cadence"
    (design D7);
  - newest run `error` → a real failure marker;
  - otherwise nothing.
- [ ] 3.13 Render `run_health_note` in all three places a scan result is visible: the
  `_collection_card.html` "Last scan" cell, the `collection_detail.html` header (under the status
  pill — **for both `ots_mode` values**, since the "Last scan" tile there only exists for
  `ots_mode == 'none'`), and inside that `ots_mode == 'none'` tile's sub-line.

### #24 — the remainder (read `design.md` D8 first; most of #24 is already shipped)

- [ ] 3.14 `verify_result.html`: render the **typed** `transport_error` as a muted diagnostic line on
  the `unavailable` (transport) card, and append it to the existing failed-lookups note. Do **not**
  render `message`.
- [ ] 3.15 `verify_run`: delete `ctx["message"]`, leaving a comment stating that the generic backend
  string must not be printed under a reason-attributed verdict (design D8).
- [ ] 3.16 `verify_result.html`: add a **Retry** control that re-posts the same `file_id` to
  `/verify/run` (CSRF token from the context, swapping the result container), rendered **only** when
  `transport_error` or `inconclusive` decided the verdict. Not on never-notarized, queued,
  unreadable-proof or any mismatch verdict.
- [ ] 3.17 Verify — no regression: confirm the three `lookup_made` gates (detail row, copyable
  report, info strip) and the reason-ordered verdict ladder are untouched by the above.

### Tests

- [ ] 3.18 `compute_health` returns each row's collection `id`, and `/healthz` includes it while
  every previously-asserted key is unchanged.
- [ ] 3.19 The health pill renders a link with a visible "Degraded · 1 collection" label when one
  collection is stale, and `base.html` renders **no** "Healthy" text before the poll (assert the
  `Checking…` placeholder). Assert `settings.html` no longer contains a static health pill.
- [ ] 3.20 A scan that skips an un-storable filename writes `runs.errors >= 1` and a non-empty
  `error_sample`, and the collection card renders "partial — N file(s) skipped". Assert the stored
  sample is ASCII-safe (round-trips through `str.encode("utf-8")`).
- [ ] 3.21 A collection whose newest `kind='scan'` run is `interrupted` renders the neutral note and
  **not** a failure marker, and its `last_scan` still reports the last *completed* scan.
- [ ] 3.22 A collection whose only run is `interrupted` renders "never" for last scan.
- [ ] 3.23 Verify: a transport failure renders the reason text and a Retry control; a
  never-notarized result renders neither; `message` appears nowhere in the response.

## 4. Integration (on the merged result, after both slices land)

- [ ] 4.1 Merge both branches; resolve `routes.py` by function boundary (design D9). Nothing outside
  the owned functions may differ.
- [ ] 4.2 Grep for production callers of everything new: `/review` is reachable from the dashboard
  tile; `run_health_note` is called from all three render sites; `health_state` is attached by both
  page routes; `runs.errors` is written by the scanner and read by `_collection_view`. Fan-out ships
  green-but-unwired code — verify each seam by hand.
- [ ] 4.3 Full gates on the merged tree: `PYTHONPATH=. pytest -q`, `ruff check .`,
  `alembic upgrade head` against a copy of a populated DB, `make audit`.
- [ ] 4.4 `openspec validate add-fleet-review-and-run-health --strict` green.
- [ ] 4.5 Update `CLAUDE.md`'s working notes with a paragraph for this change (fleet review page,
  feed ordering, health pill, `runs.errors`/`error_sample`, migration 0012, and the
  `interrupted`-is-normal rendering rule).

## 5. Gates

- [ ] 5.1 `openspec-verifier` subagent against the spec deltas (a non-author).
- [ ] 5.2 **Adversarial Codex** — mandatory: this change touches the scan→run-result path and the
  dead-man's-switch display. Frame it as a false-negative hunt: *can any surface here show a
  reassuring status that the data does not support?* Specifically — can a `partial` or `interrupted`
  run render as a clean scan; can the health pill claim Healthy before anything is computed; can the
  feed's new ordering hide an unreviewed event; can a fleet-page fingerprint authorize a mutation on
  a population it did not display; can the `from` parameter redirect a destructive POST somewhere
  it should not.
- [ ] 5.3 Deploy (`make deploy`, then `make migrate` — this change adds revision 0012).
- [ ] 5.4 `user-representative` pass on the live panel: the fleet page as an operator with issues in
  several collections, the tile→`/review` path, the feed's new top rows, the health pill on a phone
  width, and a collection showing a `partial` scan.
- [ ] 5.5 Archive + push, closing the five issues (`Closes #27, #24, #25, #28, #29`) and noting the
  #18 leftover is now shipped.
