# Give every count a destination, and make a scan's real result visible

## Why

This is the last batch of the Aug-2026 UX audit (**#12**). Five issues remain — **#27**, **#24**,
**#25**, **#28**, **#29** — plus one leftover that **#18** deliberately left open. Four of the five
are the same defect wearing different clothes:

> **The panel computes a fact, and then shows the operator something else.**

- **#27** — `GET /review` is a 404, so the fleet-wide counts (the dashboard "Open issues" tile, the
  sidebar badge) have nowhere honest to point. #12 R3 names this as *structural*: "the alarm layer
  and the action layer are not linked", and the reason they cannot be linked is that the page a
  global count would link to does not exist.
- **#18 leftover** — because of that, #18 shipped the tile as a link with a *placeholder*
  destination: one affected collection → its review page, several → `/collections`, and an explicit
  spec scenario forbidding a link to the not-yet-existing `/review`. The tile is still a dead end in
  exactly the case it matters most — several collections in trouble.
- **#25** — the "N unreviewed" pill counts open events; the feed beneath it shows the 20 most recent
  events *regardless of state*. On a healthy-but-alarmed system that feed can contain **zero** of
  the events the pill is counting. The `guard-proof-and-restore-integrity` `user-representative`
  pass caught the sharp end of this live: the dashboard offered *"Mark all 8 reviewed (all
  collections)"* while the feed showed **none** of those 8 — every one pushed off the bottom by
  newer born-acknowledged informational rows. The operator is invited to clear, in one click, eight
  alerts the page never showed them.
- **#29** — a scan that skipped files records `result='partial'`, and `run.result` appears in **no
  template**. A collection can sit in that state permanently (CLAUDE.md documents exactly that
  scenario for a non-UTF-8 filename) while the panel reports a reassuring "Last scan: 3 min ago".
  This is #12 R4 — *a positive claim computed over a subset of the data it appears to summarize* —
  applied to the scan itself. In a product whose proposition is "these bytes are the ones you had",
  "I checked" must never be shown where the truth is "I checked most of them".
- **#28** — the health pill says **Degraded** and names nothing. It is a non-interactive `<div>`
  whose only elaboration is a `title` tooltip that touch devices never fire, and `panel.css:710`
  hides the hint on mobile. One word, no collection, no way to learn more. Its own status line is
  duplicated by hand in two other places — `base.html` renders a hardcoded green **Healthy** on
  every page load before the poll answers, and `settings.html` renders a static **Healthy** pill
  that is computed from nothing at all.

**#24** is the exception, and it is mostly **already done** — see below.

## What changes

### 1. `GET /review` — the fleet-wide review page (#27)

A new page listing every open issue across the user's collections, grouped by collection, worst
first. It reuses the existing machinery wholesale: `_read_population(collection, "review")` for the
snapshot, `_review_item` for the row, `partials/review_row.html` for the render, and the existing
per-row guarded verbs. It introduces **no new query primitive and no new mutation route**.

**It carries per-row actions but no bulk verbs.** Each bulk verb ("Adopt N changes", "Stop tracking
N files") is authorized by a fingerprint minted over *one collection's entire population*; a fleet
bulk would need a new cross-collection fingerprint scope and a new cross-collection mutation, and
would put "stop tracking 412 files across 6 collections" behind a single button — #12 R2's unscoped
accept, rebuilt at fleet scale. Each group header links into its collection's own review page, where
the scoped bulk verbs live with their counts. See design **D3**.

### 2. The "Open issues" tile's multi-collection destination flips to `/review` (#18 leftover)

One line in `dashboard()` and one spec scenario. It is the reason #27 was filed.

### 3. The event feed surfaces what the pill counts (#25)

The feed's ORDER BY becomes `(acknowledged_at IS NULL) DESC, detected_at DESC` — unreviewed events
first, newest-first within each group. **Not** a filter (that is #12's rejected fix 2 — `added` /
`restored` / `moved` are deliberately born-acknowledged, so an open-only default renders "No events
recorded yet." on a healthy system), and **not** a toggle (design **D4**). The pill's vocabulary is
already "N unreviewed" — sprint 1 shipped that half; this is the other half.

### 4. The health pill names what is degraded, and stops being fabricated (#28)

`CollectionHealth` gains `id`; the pill becomes a link to `/collections` with a **visible** label
("Degraded · 2 collections" — visible, because touch never shows a `title`); each affected
collection's card carries a **stale** marker naming its own state. `base.html`'s hardcoded green
pill is replaced by the shared partial in a neutral "Checking…" state — it must not assert *Healthy*
before anything has been computed — and `settings.html`'s fake static Healthy pill is removed.

### 5. Partial, failed and interrupted runs become visible (#29)

Additive migration **0012** adds `runs.errors` and `runs.error_sample`; the scanner writes both;
`_collection_view` exposes them; one shared macro renders the same honest line everywhere a scan
result is shown: **"Last scan 2 h ago · partial — 3 files skipped"** with a capped path sample.

`interrupted` gets its own, deliberately **neutral** rendering. Since the operation-claim lease
landed, `interrupted` is what a *reclaimed abandoned claim* writes — the ordinary outcome of
restarting the app during a scan. It is not a failure and must never be drawn as one (design **D7**).

### 6. What is actually left of #24

**Read this before implementing #24.** Sprint 1 and `guard-proof-and-restore-integrity` already
implemented nearly all of it, and the live `web-panel` spec already requires it. Verified against
`routes.py::verify_run` and `partials/verify_result.html` at `9249795`:

| #24 asks for | Status |
| --- | --- |
| Split the one red panel into typed outcomes | **Done.** `verify_run` selects a verdict by *reason*, in a specified order, across ~14 branches. |
| Distinct copy per outcome | **Done.** `verify_result.html` has one `{% elif %}` arm per reason with no wording shared between them. |
| Unreachable explorer → neutral, **never red** | **Done.** Its own `verdict="unavailable"` style with an `info` glyph, distinct from both `warn` and `danger`. |
| "never stamped" reads neutral, not a failure | **Done** (`Not notarized yet`). |
| "file gone" keeps its distinct title, sub-line fixed | **Done** (`File unavailable — cannot verify`, with the "nothing was compared" copy and a review deep-link). |
| Gate the "Checked using / Verified by explorer lookup" strip on a lookup having happened | **Done** — `lookup_made` gates the detail row, the copyable report and the info strip. |

**Two things remain:**

1. **The transport reason is computed and still not rendered.** `verify_run` puts
   `result.transport_error` in the context; the template's transport branch says "Cairn could not
   reach the Bitcoin record … try again in a moment" and never prints *why*. The live spec already
   requires the reason ("SHALL report that verification is unavailable, in the neutral style, **with
   the transport reason**"), so this is an implementation gap against an existing requirement, not
   new scope. The fix renders the **typed** `transport_error` field as a muted diagnostic line —
   **not** the generic `message` string #24 originally proposed, which on a digest-mismatch branch
   would print backend text alongside (and possibly contradicting) the card's carefully-attributed
   verdict. See design **D8**.
2. **The retry affordance.** The card tells the operator to "try again in a moment" and offers no
   way to do it — the only control is "Verify another", which goes back to the search box. A
   **Retry** button is added, and *only* on the outcomes where retrying can change the answer
   (transport failure, inconclusive). It is not offered on never-notarized, queued, or
   unreadable-proof, where the same check would return the same result and a retry button implies
   otherwise.

`ctx["message"]` is then dead and is removed, so the next reader does not re-derive "just render
`message`".

## Non-goals

- **No cross-collection mutation of any kind** — no fleet bulk accept, no fleet bulk acknowledge, no
  new fingerprint form scope. The dashboard's existing `POST /events/ack-all` is untouched.
- **No change to `_collection_status`, `_alert_badge_count` or `compute_health`'s classification.**
  A `partial` run still counts as a successful scan for freshness (it is one — the collection *was*
  walked), and acknowledged-missing files still keep a collection red (#12 rejected fix 1).
- **No alerting on `partial`.** Notifying an operator that a filename could not be encoded is a
  different product decision, and the alert channel is reserved for file-integrity alarms.
- **No recovery panel / copy-paths on the fleet page.** Full paths are root-prefixed per collection;
  a single fleet clipboard mixing roots is ambiguous. Each group links to its collection's review
  page, where recovery already lives.
- **No re-litigation of #12's "Do NOT implement these" or "correct as built" lists.** In particular
  the feed is never default-filtered to open events (rejected fix 2), the sidebar badge stays in
  every OOB swap (rejected fix 8), and the Acknowledge-vs-Accept contrast card is not touched.
- **No widening of `_collection_view`'s freshness query to include `interrupted`/`error`** — #29's
  own caution. The new run-health line is a *separate* read (design **D7**).

## Impact

- **Specs:** `web-panel` (1 MODIFIED, 4 ADDED), `integrity-scanning` (1 MODIFIED),
  `datastore` (1 MODIFIED), `app-runtime` (1 MODIFIED).
- **Schema:** one additive Alembic revision, **0012** (head is `0011`): `runs.errors` (INTEGER NOT
  NULL DEFAULT 0) and `runs.error_sample` (TEXT NULL). No table rebuild, no CHECK change.
- **Code:** `src/control_panel/routes.py` (new `/review` route; `dashboard`, `_event_feed`,
  `ack_event`, `_collection_view`, `health_pill`, `collections_list`, `verify_run`),
  `src/services/scheduler.py` (`CollectionHealth.id`), `src/services/scanner.py` (persist
  `errors`/`error_sample`), `src/main.py` (`/healthz` per-collection `id`), `src/models/db.py`.
- **Templates:** new `review_fleet.html`; edits to `base.html`, `settings.html`, `dashboard.html`,
  `collection_detail.html`, `partials/health_pill.html`, `partials/_collection_card.html`,
  `partials/review_ack_row.html`, `partials/verify_result.html`, `_macros.html`.
- **`/healthz` JSON** gains an `id` per collection — additive; no existing key changes type or
  meaning, so an external monitor's parse is unaffected.
- **Operator-visible:** a new nav-reachable page; a tile that finally lands somewhere; a feed that
  leads with what needs action; a health pill that says which collection; and collections that stop
  claiming a clean scan they did not have.
