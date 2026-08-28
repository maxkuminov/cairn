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
  - `downgrade()` drops both. Nothing is lost that the logs do not also hold — **conditional on task
    3.9's finalize WARNING**, which is what makes that sentence true for all three skip causes (two
    are silent today). So the downgrade does **not** need `0011`'s refusal guard. If 3.9's log line
    is dropped, this claim must be dropped with it.
- [ ] 1.4 `src/models/db.py`: add `Run.errors: Mapped[int]` (default 0, not nullable) and
  `Run.error_sample: Mapped[str | None]`, each with a comment stating (a) that `errors` is the count
  that already decides `partial` and (b) that `error_sample` is a **bounded JSON array of ASCII-safe
  diagnostic renderings, not paths** (20 entries / 256 B per entry / 4096 B serialized — see design
  D6) and must never be fed to a filesystem call.
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
  remaining share of `FLEET_ROW_LIMIT`; one `_review_item(..., fp=_population_fingerprint(collection,
  _narrow(pop, "accept-file", …, file_id=f.id)))` per row. **No new `_FP_FORMS` entry** —
  `accept-file` is already per-row and per-collection.
- [ ] 2.3a **Do NOT call `_latest_events_by_file` on this page** (design D1). Each rendered file's
  open event is taken from `pop.open_events` — the newest (`max` by `id`, matching `ack_event`'s
  `open_events[-1]` "highest id = newest generation" rule) entry whose `file_id` matches; build the
  `{file_id: _PopEvent}` map once per group in Python from that one list. `_review_item` already
  accepts a `_PopEvent`. A second statement here would let the row's *displayed* reviewed state and
  the fingerprint it *authorizes* come from two snapshots — the guard's own failure mode beside the
  guard. Comment it with that reason and with the accepted consequence: a row whose events are all
  acknowledged shows no open event and takes its "detected" time from `last_changed`.
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
- [ ] 2.12a **The "last activity" tile** (`dashboard()`, `routes.py:617-624` — Slice A's function,
  design D14): it selects the newest **finished run of any kind** and labels it `"<collection>
  scan"` with no result, so a `stamp` run, an `upgrade` run, a `partial` scan, a failed scan and a
  reclaimed one all render as a clean scan. Fix the sub-line to name the run it actually found:
  `"<collection> <run.kind>"`, plus `" · <result>"` whenever `run.result != "ok"` — `partial`,
  `failed` (for `error`), `interrupted` (worded neutrally, never as a fault — design D7). Keep the
  existing `· N moved` suffix. **Plain text built in `dashboard()`** — it must NOT call Slice B's
  `run_health_note` macro, which would make `_macros.html` a shared file (design D9/D14).
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
- [ ] 2.21 **Per-group cap:** a collection with more issues than `FLEET_COLLECTION_ROW_LIMIT`
  renders exactly that many rows, a "+N more" note whose N is the *snapshot* remainder (not the
  render remainder), its "Review all in X →" link, and the other affected collections still render
  their rows.
- [ ] 2.22 **Total cap:** with more affected collections than `FLEET_ROW_LIMIT` can seat, the groups
  past the budget are still rendered — header, missing/modified counts and link — with zero rows,
  and the total rendered row count does not exceed `FLEET_ROW_LIMIT`.
- [ ] 2.23 **Both empty states:** a user with collections but no missing/modified files gets the
  "No open issues" state and **no** `/collection/new` link; a user with no collections at all gets
  the distinct no-collections state that does link to `/collection/new`.
- [ ] 2.24 **Hostile `from=`:** posting a per-file accept with `from=https://evil.example/x`,
  `from=//evil.example`, `from=/collection/99/review` and `from=` (empty) each redirects to the
  *collection's own* review page (never to the supplied value, never off-host), and each performs
  exactly the same single-file accept. Assert the `Location` header equals the route constant.
- [ ] 2.25 **Single-snapshot event derivation:** acknowledge one of the rendered files' events, then
  assert the fleet page rendered from a population read taken **before** that ack does not offer a
  second, contradicting state for that row — i.e. the row's `event_id`/`acked` and its `fp` both
  derive from `pop`, and no `_latest_events_by_file` call is made by `fleet_review` (assert by
  monkeypatching it to raise).
- [ ] 2.26 **Last-activity tile** (2.12a), one assertion per case: newest finished run is a clean
  `scan` → `"<name> scan"` with no result suffix; `partial` → names partial; `error` → names a
  failure; `interrupted` → neutral wording, not the failure wording; `kind='stamp'` → says "stamp",
  not "scan"; `kind='upgrade'` → says "upgrade"; and the `· N moved` suffix survives.

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
  `compute_health` populates it.
- [ ] 3.1a `compute_health` gains `user_id: int | None = None` and passes it to `list_collections`
  (design D5). `None` = fleet-wide — `/healthz`'s call site is **unchanged**. The panel's three call
  sites (3.3, 3.7) pass `user.id`, so the pill's count and the cards it sends the operator to read
  the same population. No new query: `list_collections` already takes the filter.
- [ ] 3.1b **The `running` freshness leg is kept but gated on liveness** (design D13 — read it, and
  the issue-#5 history in it, before touching this). Today the query is
  `result.in_(("ok","partial","running"))` ordered by `started`, dating a `running` row from
  `started` against the cadence threshold. Replace the classification with the two legs, explicitly:
  - **(a)** newest `kind='scan'` run with `result IN ('ok','partial')` whose `finished` is within
    `max(2 × hash_cadence_seconds, freshness_floor)` → `fresh`;
  - **(b)** a `kind='scan'` run with `result = 'running'` whose
    `coalesce(heartbeat_at, started)` is within **`RUN_HEARTBEAT_TIMEOUT_SECONDS`** → `fresh`,
    regardless of the cadence window (a live scan is fresh while it keeps heartbeating). Reuse the
    constant already imported at `scheduler.py:41` — the switch and the reaper MUST apply the same
    threshold, or a run is dead to one and alive to the other.
  - neither leg → `pending` ONLY while `created_at` is within the threshold **and no `kind='scan'`
    run exists at all for the collection** (grace covers never-scanned, nothing else); a terminal
    `error`/`interrupted` first scan inside the grace window is therefore **`stale`**, matching the
    normative requirement. An abandoned `running` run (stale heartbeat) with no recent completion
    is **`stale`**, not fresh.
  - `last_scan_age_seconds` is the age of the newest **completed** scan, `None` when there is none —
    so a collection fresh only by leg (b) reports `fresh` with a `None` age.
  Comment the function with both legs and with why (b) cannot simply trust `result='running'`.
  **This is a deliberate classification change** — the earlier "no classification change"
  prohibition is withdrawn; `proposal.md`'s non-goals now say so.
- [ ] 3.2 `main.py`: `/healthz`'s per-collection objects gain `"id"` alongside `name`/`state`/
  `last_scan_age_seconds`. Additive — no key is renamed, retyped or removed. **`/healthz` stays
  fleet-global**: it calls `compute_health` with no `user_id`, because it monitors the installation
  and a machine monitor that skipped one owner's collections is a dead-man's switch with a hole in
  it (design D5). Its `state`/`last_scan_age_seconds` *values* do change per 3.1b — note it in the
  route's docstring.
- [ ] 3.3 `health_pill` route: call `compute_health(session, settings, user_id=user.id)` (the route
  gains the `current_user` dependency the other panel routes use) and return `status` **and** the
  stale-collection count, both derived from that one `HealthReport` — never a second query.
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
- [ ] 3.7 `dashboard()` and `collections_list()`: one `compute_health(session, settings,
  user_id=user.id)` call per render; attach `health_state` (`fresh`/`pending`/`stale`) to each
  collection view by **id** — never by name, which no constraint makes unique across owners.
- [ ] 3.8 `_collection_card.html`: a **stale** marker in the "Last scan" meta cell when
  `health_state == "stale"`, naming the state in words ("scan overdue"), not by colour alone.

### #29 — partial / failed / interrupted runs

- [ ] 3.9 `scanner.py`: persist `summary.errors` to `runs.errors` and a bounded JSON array to
  `runs.error_sample` at the same finalize site that sets `result`. Entries are the **ASCII-safe
  diagnostic rendering** already used by the un-storable WARNING (`os.fsencode(relpath)` →
  `repr`/`backslashreplace`), prefixed with the reason — writing the raw name reproduces the very
  `UnicodeEncodeError` this column exists to report (design D6). Record a sample entry at all three
  `summary.errors += 1` sites (`:413` `unstorable-name`, `:424` `stat`, `:563` `hash`).
- [ ] 3.9a **The three bounds, enforced at write time** (design D6 — "20 entries" alone is not a
  bound: `repr` of `os.fsencode` output escapes each bad byte to four characters). Add the constants
  beside `ALARM_PATH_CAP`:
  - `RUN_ERROR_SAMPLE_MAX = 20` entries;
  - `RUN_ERROR_SAMPLE_ENTRY_BYTES = 256` — an over-long rendering is cut on a byte boundary at 253
    UTF-8 bytes and gets the **ASCII** marker `...` appended (ASCII, not `…`: this column's whole
    invariant is that its bytes cannot fail to bind for the class of reason it reports);
  - `RUN_ERROR_SAMPLE_TOTAL_BYTES = 4096` — append entries only while the serialized array stays
    within budget.
  Whichever bound bites, the array's **last** element is the marker string
  `"+N more skipped (sample truncated)"`, where `N` = `summary.errors` minus the real entries
  stored (the true remainder, not the cap's). Serialize with `json.dumps(...)` at the default
  `ensure_ascii=True`, and measure the budget on the encoded form. `runs.errors` always carries the
  **true** total, never the capped one.
- [ ] 3.9b **Log the sample once at finalize** whenever `summary.errors > 0`: one WARNING naming the
  collection, the count and the same bounded sample, covering all three causes. Today only the
  un-storable site logs; `stat` and hash skips are silent, so without this line task 1.3's
  "derivable from the logs" downgrade justification is false for two of three causes (design D6).
  Leave the existing un-storable-specific WARNING as it is — it names its own cause and count.
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
- [ ] 3.23 Verify — **retry offered**: a transport failure and an `inconclusive` result each render
  the reason (transport) and a Retry control that posts the same `file_id` to `/verify/run`.
- [ ] 3.24 Verify — **retry NOT offered**, one case each: never-notarized, queued-to-stamp,
  unreadable proof, and a digest mismatch under each `mismatch_blame` attribution. Assert
  `message` appears nowhere in any of these responses.
- [ ] 3.25 **`compute_health` legs** (`tests/test_scheduler.py`), one test per leg:
  (a) newest completed scan inside the window → `fresh`; outside → `stale`;
  (b) a `running` scan whose `heartbeat_at` is seconds old, `started` **well past** the cadence
  window, and **no** completed scan → `fresh` (issue #5: a long live scan must not age out its own
  freshness), with `last_scan_age_seconds is None`;
  (c) a `running` scan whose `coalesce(heartbeat_at, started)` is older than
  `RUN_HEARTBEAT_TIMEOUT_SECONDS`, no completed scan → **`stale`** (an abandoned claim is not
  evidence of a scan);
  (d) no scan run of any kind, past the startup grace → `stale`; inside it → `pending`; AND a
  first scan that terminated `error`/`interrupted` **inside** the grace window → `stale` (grace is
  gated on the absence of every `kind='scan'` run, per the normative requirement);
  (e) a recent `stamp`/`upgrade` run alone → still `stale`.
- [ ] 3.26 **Panel health is owner-scoped, `/healthz` is not:** user B owns a stale collection and
  user A owns only fresh ones → A's `/health-pill` renders healthy and none of A's cards carry a
  stale marker, while `/healthz` (unauthenticated, fleet-global) returns 503 `degraded` and lists
  both collections.
- [ ] 3.27 **Duplicate names across owners:** two collections with the *same name*, different owners,
  one stale → the stale marker attaches to the right card by `id` (assert the fresh owner's
  identically-named card carries none).
- [ ] 3.28 **Sample bounds** (`tests/test_scanner.py`): (a) a single skipped path whose rendering
  exceeds 256 B is stored truncated, ends with `...`, and is ≤ 256 B encoded; (b) many skipped files
  produce a stored value ≤ 4096 B encoded with ≤ 20 real entries plus the
  `"+N more skipped (sample truncated)"` marker, whose N + real entries == `runs.errors`; (c) the
  stored value is pure ASCII (`value.encode("ascii")` does not raise) and `json.loads` returns a
  list of `str`; (d) the finalize WARNING (3.9b) is emitted for a `stat`-OSError-only skip, i.e. for
  a cause that logs nothing today.
- [ ] 3.29 **Run-result rendering at every site**, matrixed: for each of `partial`, `error`,
  `interrupted` (newer than the last completed scan) and a clean `ok`, assert the rendering at all
  three sites of 3.13 — the collection card, the collection-detail header (**both** `ots_mode`
  values), and the `ots_mode == 'none'` "Last scan" tile sub-line. Plus the combination the audit
  named: a `partial` completed scan followed by a **newer** `error` run → the page states the last
  completed scan was partial **and** discloses the later failure, without either erasing the other.

## 4. Integration (on the merged result, after both slices land)

- [ ] 4.1 Merge both branches; resolve `routes.py` by function boundary (design D9). Nothing outside
  the owned functions may differ.
- [ ] 4.2 Grep for production callers of everything new: `/review` is reachable from the dashboard
  tile; `run_health_note` is called from all three render sites; `health_state` is attached by both
  page routes; `runs.errors` is written by the scanner and read by `_collection_view`. Fan-out ships
  green-but-unwired code — verify each seam by hand.
- [ ] 4.3 Full gates on the merged tree: `PYTHONPATH=. pytest -q`, `ruff check .`,
  `alembic upgrade head` against a copy of a populated DB, `make audit`.
- [ ] 4.4 `openspec validate add-fleet-review-and-run-health --strict` green, **and** §6's
  scenario ↔ test matrix walked row by row: every scenario in the four spec deltas has a named test
  that exists and passes. A scenario with no test is a requirement nothing checks.
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
  it should not; can a crashed scanner's leftover `running` run still read *fresh* (3.1b); can the
  owner-scoped pill hide a stale collection from the person who owns it, or `/healthz` omit one; can
  a bounded `error_sample` under-report `runs.errors`; can the last-activity tile present a `stamp`,
  `upgrade`, `partial`, `error` or `interrupted` run as a clean scan.
- [ ] 5.3 Deploy (`make deploy`, then `make migrate` — this change adds revision 0012).
- [ ] 5.4 `user-representative` pass on the live panel: the fleet page as an operator with issues in
  several collections, the tile→`/review` path, the feed's new top rows, the health pill on a phone
  width, and a collection showing a `partial` scan.
- [ ] 5.5 Archive + push, closing the five issues (`Closes #27, #24, #25, #28, #29`) and noting the
  #18 leftover is now shipped.

## 6. Scenario ↔ test matrix

Every scenario in this change's four spec deltas, and the test that proves it. "existing" = the
scenario is unchanged by this change and already covered; the merged gate (4.3) must keep it green,
and no new test is owed. Anything else names a task above. **A scenario with no entry here is a
gap** — add the test, do not delete the row.

### `web-panel` — dashboard (MODIFIED)

| Scenario | Test |
| --- | --- |
| Mark an event reviewed | existing (`test_panel.py` ack tests) |
| Bulk mark-reviewed states its scope and confirms | existing |
| Bulk mark-reviewed is scoped to the user | existing |
| Bulk mark-reviewed when nothing is open | existing |
| Open issues tile links to a single affected collection | 2.19 |
| Open issues tile with several affected collections | 2.19 |
| Open issues tile at zero | 2.19 |
| Unreviewed events are visible in the feed that offers to clear them | 2.18 |
| A healthy system's feed is not emptied | 2.18 |
| The badge and the tile agree | existing |
| Watched-but-not-baselined files are explained | existing |
| The last-activity tile names a non-scan run for what it is | 2.26 (`stamp`, `upgrade` cases) |
| The last-activity tile names a partial or failed run | 2.26 (`partial`, `error` cases) |
| The last-activity tile treats an interrupted run neutrally | 2.26 (`interrupted` case) |

### `web-panel` — fleet-wide review (ADDED)

| Scenario | Test |
| --- | --- |
| Issues in several collections are listed together | 2.14 |
| A row's event and its fingerprint come from the same read | 2.25 |
| A row acts on one file without leaving the page's snapshot | 2.16 (valid-fingerprint half) |
| A stale fleet-wide row is refused and returns to the fleet page | 2.16 (stale half) |
| The return destination is chosen from a fixed set, never supplied | 2.24 |
| No collection-spanning bulk verb is offered | 2.15 |
| Marking a row reviewed does not change the file counts | 2.17 |
| One very large collection does not hide the others | 2.21 (per-group cap), 2.22 (total cap) |
| The page is scoped to the viewer's collections | 2.20 |
| No open issues | 2.23 (both empty states) |

### `web-panel` — health pill (ADDED)

| Scenario | Test |
| --- | --- |
| A degraded pill names the number and links to the list | 3.19 |
| The pill claims nothing before it has been computed | 3.19 (`Checking…`, no "Healthy" in `base.html`) |
| The stale collection is identified where the link lands | 3.19 + 3.27 |
| The panel's health is scoped to the viewer | 3.26 |
| A card is matched to its freshness by identity, not by name | 3.27 |
| No page shows a fabricated health status | 3.19 (settings assertion) |

### `web-panel` — scan result visibility (ADDED)

| Scenario | Test |
| --- | --- |
| A partial scan says what it skipped | 3.20, 3.29 (`partial` row, all three sites) |
| A partial scan is not presented as a clean one | 3.29 (`partial` vs `ok` rows differ at every site) |
| An interrupted run is disclosed neutrally | 3.21, 3.29 (`interrupted` row) |
| An interrupted run does not refresh the last-scan claim | 3.21 |
| A collection with no completed scan says so | 3.22 |
| A failed run is rendered as a failure | 3.29 (`error` row, and the partial-then-error combination) |

### `web-panel` — verify retry (ADDED)

| Scenario | Test |
| --- | --- |
| An unreachable backend offers a retry and names the reason | 3.23 (transport + inconclusive) |
| A settled outcome offers no retry | 3.24 (never-stamped, queued, unreadable proof, each mismatch blame) |
| The backend's general message is not printed under an attributed verdict | 3.24 (`message` absent) |

### `app-runtime` — `/healthz` (MODIFIED)

| Scenario | Test |
| --- | --- |
| Healthy and fresh | 3.25(a) |
| A stale corpus trips the switch | 3.25(a) |
| A failed first scan inside grace is stale, not pending | 3.25(d) |
| A long scan that is still alive keeps its corpus fresh | 3.25(b) |
| An abandoned in-flight scan confers no freshness | 3.25(c) |
| No completed scan and no running scan is stale | 3.25(d) |
| The reported age describes a completed scan | 3.25(b) (`last_scan_age_seconds is None`) |
| A stamp or upgrade run does not refresh freshness | 3.25(e) |
| Datastore unreachable | existing |
| Each freshness record identifies its corpus | 3.18 |
| The endpoint reports every owner's corpora | 3.26 (`/healthz` half) |

### `integrity-scanning` — each scan records a run (MODIFIED)

| Scenario | Test |
| --- | --- |
| Successful scan records counts | existing |
| In-progress scan exposes a growing processed count | existing |
| First-ever scan has no progress estimate | existing |
| Unreadable file does not abort the scan | existing |
| A partial run records how many files it skipped and which | 3.20 |
| An un-storable filename is recorded in a storable form | 3.20 (ASCII round-trip), 3.28(c) |
| An oversized sample entry is truncated, not stored whole | 3.28(a) |
| A flood of skipped files does not produce an unbounded sample | 3.28(b) |
| A skipping run logs what it skipped | 3.28(d) |

### `datastore` — schema (MODIFIED)

| Scenario | Test |
| --- | --- |
| Run-error migration adds the columns without altering existing rows | 1.5 (upgrade + downgrade against a populated DB) |
| Every other scenario in the delta | existing (unchanged by this change) |

