# Design notes — fleet review + run health

Only the decisions that go beyond what #27/#24/#25/#28/#29 already state, plus the ordering and
ownership statements an implementer needs. Where the issue text is explicit, that is the spec.

## Grounding — line references verified against HEAD (`9249795`)

| Claim | Verified at |
| --- | --- |
| `GET /review` is a 404 | no route matches `/review` in `routes.py`; only `/collection/{id}/review` exists (`routes.py:1721`) |
| The tile's placeholder destination | `routes.py:596-602` — `"/collection/{id}/review" if len(affected) == 1 else "/collections"`, with the comment *"NEVER `/review` — it is a 404 until #27"* |
| The feed ignores acknowledgement | `routes.py:526-533` — `select(Event).where(collection_id.in_(…)).order_by(Event.detected_at.desc()).limit(20)`; no `acknowledged_at` term |
| The pill already says "unreviewed" | `partials/_events_controls.html:6` — `{{ open_events }} unreviewed` |
| The health pill is an inert `<div>` | `partials/health_pill.html:3-10`, duplicated at `base.html:93-98`; the fake static pill at `settings.html:206` |
| `CollectionHealth` has no `id` | `scheduler.py:75-79` — `name`, `state`, `last_scan_age_seconds` |
| `run.result` is in no template | `grep -rn "\.result" src/control_panel/templates/` → no hits; `_collection_view` filters `result.in_(("ok","partial"))` at `routes.py:363-372` and exposes only `last_scan` |
| The detail page's "Last scan" tile is `ots_mode == "none"` only | `collection_detail.html:117-123` — it is the `{% else %}` of "Anchored to chain" |
| `summary.errors` exists and is never persisted | `scanner.py:125` (the field), `:413`, `:424`, `:563` (three increments), `:638` (`result = "partial" if summary.errors else "ok"`) — no `runs` column receives it |
| `interrupted` is written by claim reclamation, not by scans | `collections.py:173` (in-band `reclaim_stale_claim`), `scheduler.py:224` (startup reaper) |
| Alembic head | `alembic/versions/0011_proof_provenance_and_restored_changed.py` → this change is **0012** |
| #24's verify work already landed | `routes.py:2300-2380` (the reason-ordered verdict ladder), `verify_result.html:10-92` (one arm per reason), `:146` + `:163` + `:177` (`lookup_made` gating) |

---

## D1 — The fleet page is a **read** surface with per-row verbs, not a new mutation surface

`GET /review` renders, per collection the user owns and that has open issues:

```
┌ Group header:  <collection name> · N missing · M modified · <N unreviewed pill> · Review all in X →
└ Rows:          partials/review_row.html, missing first, then modified, path-ordered within each
```

Every row is produced by the **existing** `_review_item(pop_file, root, event, fp=…)` from the
**existing** `_read_population(session, collection, "review")` — one statement per collection, the
same single-snapshot property the per-collection review page depends on (a fingerprint must describe
the population its row was rendered from; two reads is the hole the guard exists to close). The page
therefore issues *N* population reads for *N* affected collections, which is the same shape the
dashboard already pays for (`_collection_view` per collection).

**The row's open event comes out of the same snapshot too.** `collection_review` renders its rows
from `_read_population` but fetches their events with a *second* statement
(`_latest_events_by_file`). On the fleet page that second read is not taken: each rendered file's
open event is `max(by id)` of the `pop.open_events` entries whose `file_id` matches — the same
"highest id = newest generation" rule `ack_event` already applies to `narrowed.open_events[-1]`, and
`_review_item` already accepts a `_PopEvent` in place of an ORM `Event`. Two consequences, both
wanted:

- A row's *displayed* reviewed state and the fingerprint it *authorizes* are then slices of one
  statement. With two reads, a scanner insert or a concurrent acknowledgement between them lets the
  row offer "Mark reviewed" for an event that is already acknowledged, or hide the control for one
  that is open — the guard's own failure mode, reintroduced beside the guard.
- `pop.open_events` holds **open** events only (the read selects `acknowledged_at IS NULL`), whereas
  `_latest_events_by_file` returns the newest event acknowledged or not. So a fleet row whose events
  are all acknowledged shows no open event (correct — the collection page would too, via
  `_review_item`'s `acknowledged_at` test) and takes its "detected" time from `last_changed` rather
  than from that acknowledged event. A slightly different relative timestamp on an already-reviewed
  row is the whole cost, and it is paid for a snapshot property that authorizes a destructive verb.

**No new route, no new fingerprint form scope.** The per-row forms post to the routes that already
exist — `POST /collection/{cid}/file/{fid}/accept` and `POST /events/{eid}/ack` — because a row's
fingerprint is already per-collection *and* per-row (`_FP_FORMS["accept-file"]`), minted here from
this page's own snapshot of that collection.

## D2 — Coming back to the fleet page after a per-row accept

`_guarded_accept` hardcodes its refusal redirect to `/collection/{id}/review?stale=1`, and
`collection_file_accept` hardcodes its success redirect to `/collection/{id}/review`. Sending a
fleet-page operator to a *different page* on every click — success or refusal — is a worse dead end
than the one this change removes.

**Chosen:** a `from` query parameter on those two routes, **whitelisted to exactly two literals**
(`"fleet"` → `/review`, anything else → the collection's review page), threaded into
`_guarded_accept` as the base of both its success and its stale redirect. It is never an
operator-supplied URL and never widens what the POST *does*: one URL still means one consequence,
and the verb is still a route constant.

**Rejected — a `Referer`-derived redirect.** Attacker-influenced, and silently wrong behind a proxy.

**Rejected — duplicating the accept route under `/review`.** A second URL for the same mutation
doubles the destructive surface for a redirect target.

## D3 — Why the fleet page carries **no** bulk verbs

Each bulk verb is authorized by `_population_fingerprint(collection, _narrow(pop, form, …))` — a
hash over **one collection's entire population in one state**. A fleet-wide "Stop tracking all" would
need:

- a new form scope whose preimage spans N collections (the header currently carries one collection's
  identity — the thing that makes a fingerprint un-replayable at another collection's address);
- a new cross-collection mutation route with no counterpart in `accept_collection`, which takes one
  collection;
- and a confirm dialog reading *"permanently remove 412 file records across 6 collections"*.

That last line is #12 R2 — *one unscoped irreversible verb* — rebuilt one level up, on the page whose
whole purpose is to make counts land somewhere honest. **Triage is fleet-wide; the irreversible verbs
stay collection-scoped**, one click away via each group's "Review all in X →". Per-row verbs *are*
offered on the fleet page precisely because the row is the honest unit: it names one file, one
consequence, one fingerprint.

## D4 — #25: ordering, not a toggle

#25 offers two fixes. **Chosen: ordering** —
`ORDER BY (acknowledged_at IS NULL) DESC, detected_at DESC`.

- A toggle is a new stateful control with two empty states, a default that has to be argued for
  anyway, and (because it is per-request state) a URL parameter to whitelist. Ordering needs none of
  that and cannot be left in the wrong position by the last person who touched it.
- Ordering **preserves the rail's activity-log character** — every kind is still present, the
  born-acknowledged informational rows are still there, just below what needs a human. That is what
  #12's rejected fix 2 protects; a filter would break it, and a toggle would break it on whichever
  setting the operator leaves it in.
- It closes the `user-representative` finding exactly: the events the bulk-ack would clear are now
  the ones at the top of the feed.

**Accepted consequence:** with more than 20 unreviewed events the feed becomes 20 unreviewed rows and
no activity log at all. That is the correct emphasis (a 20-row rail is not the place to read history
while 300 files are missing), and this change ships the proper home for the long list in the same
breath — `/review`, linked from the pill's own card. Bulk-ack keeps its count and its confirm
because it still reaches past the cap.

**Not changed:** the pill's `open_events` count and the badge's `alert_count` remain **real COUNT
queries over the whole population**, never over the 20 rendered rows.

## D5 — #28: one health source, one destination, and no fabricated status

Three renders of health exist today; two of them are hand-written duplicates.

1. **`partials/health_pill.html`** — the real one (fed by `compute_health`). It becomes an
   `<a href="/collections">` with a **visible** label: `Healthy` / `Degraded · N collections` /
   `Error`. Visible, not a `title`: `panel.css:710` hides the hint at phone widths and touch never
   fires a tooltip, so a `title` is the same dead end in different markup.
2. **`base.html:93-98`** — a hand-copied pill hardcoded to green **Healthy**, rendered on every page
   load before the `hx-trigger="load"` poll answers. It asserts a computed-from-nothing verdict on
   the one surface that means "the dead-man's switch is fine". Replaced by an `{% include %}` of the
   partial in a **neutral `Checking…` state** (`status=None`). Not by a server-side
   `compute_health()` call in `_base_context`: that would run a per-collection query loop on *every*
   page render to fill a pill that repolls 30 s later anyway.
3. **`settings.html:206`** — a static `pill--ok` "Healthy" beside the health-endpoint documentation,
   computed from nothing at all. **Removed** rather than made live: that card documents *the
   endpoint*, and a second live health surface is a second thing to keep in sync.

**Destination: always `/collections`.** Not "one stale → link straight to it, several → the list"
(the rule the #18 tile uses), because the two are answering different questions: the tile's
destination is where you *act*, and its population is a file count that a single collection can own;
the pill's job is *diagnosis*, and the per-collection **stale markers** it is sending you to read
live on the cards in the list. One rule, one destination, and the marker does the naming.

**`CollectionHealth` gains `id`** (`scheduler.py:75`) so a card can be matched to its health row
without joining on a name that is not unique by constraint. `/healthz`'s per-collection objects gain
`id` alongside `name` — additive; no existing key changes.

**Panel health is owner-scoped; `/healthz` is not.** `compute_health` currently iterates
`list_collections(session)` — every collection in the installation. That is right for `/healthz`,
which is the machine monitor of the *deployment*: a dead-man's switch that skipped another user's
collections would have a hole in it exactly where nobody is looking. It is wrong for the pill, whose
entire fix here is *"say which collection and take me to it"*: in `multi` mode a fleet-global count
renders **Degraded · 1 collection** above a `/collections` list that is `list_collections(user_id=…)`
— owner-scoped — so user A is told a collection is stale and then shown a page where no such
collection exists. That is the defect class of all five issues (compute one thing, show another),
manufactured by the fix for one of them.

**Chosen:** `compute_health(session, settings, user_id: int | None = None)`. `None` = fleet
(`/healthz`, unchanged behaviour and unchanged signature at that call site); the panel's three call
sites (`health_pill`, `dashboard`, `collections_list`) pass `user.id`. The scoping term is
`list_collections`' existing `user_id` filter — no new query. In `single` mode there is one user, so
the two are identical and nothing changes.

**Not chosen — filtering the fleet report in the route.** The pill route would then read every
collection's runs to render one user's number, and the "one `compute_health` call per render"
property the cards depend on would quietly become "one fleet-wide query loop per render".

**Not chosen — scoping `/healthz` too.** It is unauthenticated and machine-facing; there is no user
to scope it to, and per-user monitoring endpoints are a Phase-2 auth question, not this change's.

**Where the stale marker goes:** `_collection_card.html`'s "Last scan" meta cell (used by both the
dashboard and `/collections`), fed by a `health_state` key that `dashboard()` and `collections_list()`
attach from **one** `compute_health` call per render.

## D6 — #29's schema: `runs.errors` + `runs.error_sample`

**Two additive columns, no rebuild:**

- `runs.errors` — `INTEGER NOT NULL DEFAULT 0`. `RunSummary.errors` already exists and already
  decides `partial`; this is the column it was always missing.
- `runs.error_sample` — `TEXT NULL`, a **JSON array of strings**. JSON because it is this codebase's
  established shape for a blob column (`exclude_globs_json`, `alert_json`) and it is SQLite-friendly;
  a count alone would tell the operator that three files were skipped without telling them *which*,
  which is the whole ask.

**The sample's exact bounds.** "Capped at 20 entries" is not a bound: one entry is a rendering of a
*path*, `repr()` of `os.fsencode` output escapes each bad byte to four characters, and a deep tree of
long names makes 20 entries arbitrarily large — in a column read on every collection card render.
Three numbers, all enforced at write time:

| Bound | Value | Enforcement |
| --- | --- | --- |
| entries | `RUN_ERROR_SAMPLE_MAX = 20` | stop appending past 20 |
| bytes per entry | `RUN_ERROR_SAMPLE_ENTRY_BYTES = 256` | truncate the rendering to 253 UTF-8 bytes and append `...` |
| bytes serialized | `RUN_ERROR_SAMPLE_TOTAL_BYTES = 4096` | append entries only while the serialized array stays within budget |

- **Truncation is deterministic and marked**: cut on a byte boundary at 253 and append the ASCII
  marker `...`, so a truncated rendering can never be mistaken for a whole name. The marker is ASCII
  (`...`, not `…`) for the same reason the entries are: this column's invariant is that its stored
  bytes cannot fail to bind for the class of reason the sample exists to report.
- **Dropped entries are counted, not silent.** Whichever bound bites, the array's **last** element is
  a marker string `"+N more skipped (sample truncated)"` — still a JSON string, so the column's shape
  is unchanged and no reader needs a second type. `N` is `summary.errors` minus the number of real
  entries stored, so it is the true remainder, not the remainder of the cap.
- **Serialization is `json.dumps(entries)`** with the default `ensure_ascii=True`, which escapes any
  non-ASCII to `\uXXXX` — belt and braces over the ASCII-safe rendering, and the reason the byte
  budget can be measured on the encoded form.

**Every skip is also logged, which is what makes the downgrade honest.** Today only the un-storable
site logs (one batched WARNING); `stat` and hash `OSError` skips are **silent** (`scanner.py:424`,
`:563` — bare `summary.errors += 1; continue`). So "no data is lost that is not derivable from the
logs" would have been false for two of the three causes. The scan therefore emits **one** WARNING at
finalize whenever `summary.errors > 0`, naming the count and the same capped sample across all three
causes. It is bounded by construction (the sample is already capped, so the line is cheap), it runs
once per run rather than once per file, and it means the `0012` downgrade drops only a *convenience*
copy of information the operator's log already holds. The existing un-storable-specific WARNING stays
as it is — it names its own cause and its own count.

**The sample entries are a diagnostic rendering, not paths.** This matters and is easy to get wrong:
the headline case of a skipped file is a **name that could not be stored as TEXT in the first place**
(`_db_storable` rejects a lone surrogate — that is literally why the row does not exist). Writing the
raw name into `error_sample` would reproduce the `UnicodeEncodeError` that `tolerate-unencodable-paths`
fixed, in the column added to report it. So each entry is the ASCII-safe rendering the existing
WARNING already logs (`os.fsencode(relpath)` → `repr`/`backslashreplace`), and the UI labels the list
as a diagnostic sample. It must never be fed back to a filesystem call or offered as a
"Copy paths" list.

**Three error sites, three reasons, one column.** `scanner.py:413` (un-storable name), `:424`
(`stat` OSError), `:563` (hash OSError) all already `summary.errors += 1`; the sample records the
path for each, prefixed with its reason (`unstorable-name: …`, `stat: …`, `hash: …`), capped as
above. Nothing about classification changes.

**The finalize path's last-ditch fallback stays intact.** `tolerate-unencodable-paths` guarantees a
run always reaches a terminal state, ending in a raw `UPDATE runs SET result='error', finished=…`.
That fallback may leave `errors` at 0 — accepted: its job is to guarantee terminality, and adding a
second value to that statement is a second thing that can fail on the path that exists because
everything else failed.

## D7 — `interrupted` is a normal outcome and is rendered neutrally

#29 warns: *"do not drop the `IN ('ok','partial')` filter without labelling `interrupted`, which
every deploy produces."* Since the operation-claim lease (`0011`), that sentence needs restating:

`interrupted` is now written **only** by claim reclamation — `collections.reclaim_stale_claim` in
band, and `scheduler.reap_orphaned_runs` at startup — and **only** for a run whose heartbeat has
aged out past `RUN_HEARTBEAT_TIMEOUT_SECONDS`. So restarting the app mid-scan still produces exactly
one `interrupted` run, routinely, on every deploy that lands during a scan; and what it means is
*"this run's claim was abandoned and has been reclaimed"*, not *"this scan failed"*. `scheduler.py`
says so in as many words: the terminal state is `interrupted` "**not `error`** so a legitimate
restart is not conflated with a genuine scan failure".

Therefore:

- **`_collection_view`'s query keeps its `IN ('ok','partial')` filter.** `last_scan` continues to
  mean "when a scan last *completed*", which is the only thing a sentence beginning "Last scan" can
  honestly mean. Widening it would let an abandoned run refresh the display — the exact
  false-negative #29 is complaining about, introduced by its own fix. This is a *narrower* rule than
  the dead-man's-switch freshness in D13, deliberately: the switch asks "is this collection still
  being watched" (a live in-flight scan answers yes), the panel line asks "when did a scan last
  finish" (only a finish answers that). Two questions, two queries, both stated in the specs.
- The run-health line is a **second, separate** read: the newest `kind='scan'` run in any terminal
  state. It renders a note **only when that newest run is `interrupted` or `error`** — i.e. only when
  something happened *after* the last completed scan that the operator cannot otherwise see.
- `interrupted` renders **muted/neutral**, never red, in the vocabulary of what it is: *"a later scan
  was interrupted (app restart or reclaimed claim); it will re-run on the next cadence."* An
  `error` run renders as a real failure. A `partial` completed run renders as
  `partial — N files skipped`, warn-toned, with the sample.
- A collection whose only runs are `interrupted` still reads **"Last scan: never"** — accurate: no
  scan has completed.

## D8 — What #24 reduces to, and why not `{{ message }}`

`proposal.md` §6 has the full audit. The implementation remainder:

1. **Render the transport reason.** The muted diagnostic line prints the **typed**
   `transport_error` field, on the `unavailable` card and inside the existing failed-lookups note.
   **Not** the generic `message`: `message` is `result.message` on every branch, so rendering it
   generically would print backend text under a `mismatch_blame`-attributed verdict whose whole
   design is that the card says *exactly* what was established and no more — the one place in this
   codebase where an extra sentence is a correctness bug. `ctx["message"]` becomes unused and is
   **deleted**, with a comment naming this reason, so the next reader does not re-derive #24's
   original wording.
2. **Retry**, offered only where retrying can change the answer: `transport_error` and
   `inconclusive`. It re-posts the same `file_id` to `POST /verify` with the page's CSRF token and
   swaps the result container in place. It is **not** offered on `Not notarized yet`, `Queued to
   stamp`, `Proof file could not be read`, or any digest-mismatch verdict: those re-run to the same
   answer, and a Retry button beside them implies the result is provisional when it is settled.

Nothing else in #24 is open; the "one identical red panel" it describes has not existed since
sprint 1, and the gating it asks for is in place at all three sites (`lookup_made`).

## D9 — File ownership across the two slices

`routes.py` is the one file both slices touch, so ownership is **by function**, as
`guard-proof-and-restore-integrity` did:

| | Slice A (#27/#25/#18) | Slice B (#28/#29/#24) |
| --- | --- | --- |
| `routes.py` | `dashboard`, `_event_feed`, `ack_event`, new `fleet_review`, `REVIEW_*` limits, `collection_file_accept` + `_guarded_accept` (the `from` param only) | `_collection_view`, `_base_context`, `health_pill`, `collections_list`, `verify_run` |
| templates | new `review_fleet.html`, `dashboard.html`, `partials/review_ack_row.html`, `partials/review_row.html` | `base.html`, `settings.html`, `collection_detail.html`, `partials/health_pill.html`, `partials/_collection_card.html`, `partials/verify_result.html` |
| services | — | `scheduler.py`, `scanner.py`, `main.py`, `models/db.py` |
| macros | `_macros.html` is **Slice B's** (it adds the run-health macro); Slice A must not edit it | |

Neither slice reformats the file; neither edits the other's functions. The migration belongs to
**shared prep**, written and committed on the base branch before fan-out — worktrees branch from the
committed base, so a migration written inside a slice is invisible to the other one and a second
slice inventing `0013` is the numbered-migration collision this project has hit before.

## D10 — The per-row acknowledge OOB target on a page with N collections

`partials/review_ack_row.html` OOB-swaps a span with the hardcoded id `review-open-pill` — the
collection review page's single "N unreviewed" pill. The fleet page has one such pill **per group**,
so a single hardcoded id would refresh whichever group happened to render first.

**Chosen:** parameterize the id. `review_ack_row.html` takes `pill_id`, defaulting to
`"review-open-pill"` so the collection review page's markup is unchanged byte-for-byte; the fleet
page renders `review-open-pill-{{ c.id }}` and `ack_event(view="fleet")` passes it. The
`#sidebar-alert-badge` OOB swap stays in both responses (#12 rejected fix 8).

**No fleet-level unreviewed pill is refreshed**, because nothing else on the page changes: marking an
event reviewed writes to the reading log only, so the group's file-derived counts (`N missing ·
M modified`) are *correct to stay put*. That is #12 R1's model, applied literally.

## D11 — Bounding the fleet page

Two caps, for two different failure modes:

- `FLEET_COLLECTION_ROW_LIMIT` (100) — per group, so one collection with 40 000 missing files cannot
  crowd every other collection off the page. Over the cap, the group shows "+N more" and its
  "Review all in X →" link.
- `FLEET_ROW_LIMIT` (500) — total rows rendered, matching `REVIEW_ROW_LIMIT`. Groups past it are
  still **listed with their counts and their link**, just with no rows: a collection must never
  vanish from a fleet-wide issue list because of a render budget.

Counts and group ordering come from each collection's population snapshot, never from the truncated
render list. Groups are ordered `missing DESC, modified DESC, name` — worst first, matching
"missing first" within a group.

## D12 — Scoping and the known multi-user limitation

The page is built from `list_collections(session, user_id=user.id)`, so it is owner-scoped by
construction — no `_get_owned_collection` 404 path is needed because a non-owned collection is never
in the list. This inherits, and does not worsen, `add-alert-deep-links`' accepted limitation: in
`multi` mode an alert recipient who is not the collection's owner still reaches "not found" at the
per-collection review page. `/review` shows such a user *their own* collections' issues, which is the
correct answer for a page keyed on the session, not on the alert.

## D13 — Freshness has two legs, and the running leg must prove liveness

`compute_health` today (`scheduler.py:118-131`) selects the newest `kind='scan'` run in
`('ok','partial','running')` and dates a `running` one from `started`. The Codex pass read the
proposal's "no change to `compute_health`'s classification" against the app-runtime requirement's
"a successful scan run within the window" and found them contradictory — and it is the *requirement*
that was wrong, not the code.

**History (do not silently undo it).** The `running` leg is the deliberate fix for audit issue **#5**:
before it, a scan that legitimately took longer than `max(2 × cadence, floor)` aged out **its own**
freshness while it was still working, and `/healthz` flapped `degraded` for a collection that was
being scanned at that very moment. A false `degraded` on a dead-man's switch is not a harmless
conservative default: it is the alarm that teaches the operator to ignore the alarm. Restoring
"completions only" would reintroduce it.

**But the leg as written trusts `result='running'`, which is not evidence of life.** A process killed
mid-scan leaves a `running` row behind until something reclaims it; if it was started recently, that
row currently reports **fresh** — a crashed scanner reading healthy, which is precisely the
false-negative the switch exists to prevent. Reclamation already knows better: `reclaim_stale_claim`
and `reap_orphaned_runs` both test `coalesce(heartbeat_at, started) <= now - RUN_HEARTBEAT_TIMEOUT_SECONDS`
(`collections.py:33`, `:160`; `scheduler.py:209`, `:222`).

**Chosen — state both legs, and gate the second on the same liveness test the reaper uses:**

- **(a)** newest `kind='scan'` run with `result IN ('ok','partial')` **finished** within
  `max(2 × cadence, floor)` → fresh;
- **(b)** a `kind='scan'` run is `running` **and** `coalesce(heartbeat_at, started)` is within
  `RUN_HEARTBEAT_TIMEOUT_SECONDS` → fresh, regardless of the cadence window;
- neither → `pending` inside the startup grace, else `stale`.

Leg (b) is bounded by the **lease**, not by the cadence: a live scan is fresh for as long as it keeps
heartbeating, and goes stale within one lease interval of the process dying. Reusing
`RUN_HEARTBEAT_TIMEOUT_SECONDS` (already imported into `scheduler.py`) is not a convenience — if the
switch and the reaper used different thresholds there would be a window in which a run is dead to one
and alive to the other, which is a state nobody can reason about.

**This IS a classification change**, so the proposal's non-goal and task 3.1's "no classification
change" prohibition are both withdrawn. What it changes, concretely: an abandoned `running` scan with
no recent completion now reads `stale` where it read `fresh`. That is the direction this product
errs in — toward reporting, never toward reassurance.

**`last_scan_age_seconds` follows the completion, not the run.** With two legs, "age" had two
possible meanings; the honest one is the age of the newest *completed* scan, `None` when there is
none. A collection fresh only by leg (b) therefore reports `state:"fresh"` with a `null` age, which
is exactly true: it is being scanned, and no scan has finished yet. This is the one `/healthz` field
whose *value* can change for an existing key (never its name or type) — an external monitor keying on
`status` or `state` is unaffected, and one keying on the age already had to handle `null`.

## D14 — The dashboard's "last activity" tile is generic, so it must say what it is describing

`dashboard()` selects the newest **finished run of any kind** across the user's collections
(`routes.py:618-624`) and labels it `f"{collection} scan"`. Three ways that lies, all live today:

1. the run may be a `stamp` or an `upgrade` — the tile says "scan", and a stamp pass says nothing
   about when the files were last *checked*;
2. the run may have finished `partial` — the tile reads exactly as it does for a clean scan, which is
   #29's defect in the one tile #29's task list did not name;
3. the run may have finished `error` or `interrupted` — same.

**Chosen: keep the tile generic and make it honest**, rather than restricting it to completed scans.
Restricting it would silently drop the upgrade/stamp activity it exists to surface (a fleet whose
only recent activity is the nightly upgrade would read "no scans yet"), and the collection cards
already carry the per-collection scan story. So the sub-line becomes
`"<collection> <kind>"` plus `" · <result>"` for any result other than `ok`:

- `Photos scan` (ok) · `Photos scan · partial` · `Photos scan · failed` ·
  `Photos scan · interrupted` · `Max Docs upgrade` · `Bob Tax stamp`.
- `interrupted` is worded neutrally here for the same reason as everywhere else (D7): it is what a
  deploy during a scan produces.
- The existing `· N moved` suffix is kept.

**Ownership:** the tile is built in `dashboard()`, which is **Slice A's** function (D9), so the tile
is Slice A's task even though it closes a #29-shaped hole. It is a plain text sub-line and must
**not** reach for Slice B's `run_health_note` macro — that would make `_macros.html` a shared file
and put a merge conflict on the one seam D9 exists to keep clean.
