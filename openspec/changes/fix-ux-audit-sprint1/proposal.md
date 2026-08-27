# Sprint 1 of the Aug-2026 UX audit: stop the panel asserting things it has not checked

## Why
Six independent auditors walked the control panel; an adversarial pass then tried to refute every
finding. 50 survived, deduplicated into ~28 defects (GitHub #12 is the meta issue and carries the
root causes). This change implements the **sprint-1** set: the small blocking items plus every
false-reassurance string that can be fixed without new routes, new tables, or new vocabulary.

Cairn's product is a trust claim — *this file existed, unaltered, at this time*. The audit found
that claim being over-stated by the UI in ways the backend never intended (root cause **R4** in #12:
*a positive claim computed over a subset of the data it appears to summarize*):

- **`/verify` tells the operator a changed file's proof is "pending confirmation"** (#13). The
  route branches on the proof's `state` *before* asking why verification failed, so the exact event
  this product exists to detect — bytes no longer matching the notarized digest — is rendered as
  "usually settles within a few hours". The same branch swallows the `OtsError` fallback, so an
  unreachable block explorer is also reported as a young proof. This is the single most dangerous
  string in the panel.
- **`/verify`'s "Recently anchored" list hardcodes the green "Anchored" badge on every row** (#19).
  The real per-file state is fetched and then discarded. The list is newest-first, so the
  *least*-confirmed proofs sit on top wearing a green pill. Observed live: every row green while
  every row was actually `incomplete`.
- **The proof tiles say "all confirmed" over collections where 93% of files have no proof at all**
  (#20), and a **zero-file collection reads "All clear · All files verified · 0 · all confirmed"**
  (#31) — including one whose root is a typo or a failed bind mount, which scans `ok` forever with
  a fresh "Last scan: 3 min ago". That is Cairn watching nothing and reporting green.
- **The most destructive control in the product is its most inviting** (#14): `accept_collection`
  rewrites baselines and *deletes* rows for missing files, and on the collection page it is the
  `btn--primary` in a plain form with no confirmation at all — while the review page's copy of the
  same call has a chip, an explanatory card and a `confirm()`.
- **A restored file leaves its "missing" alert permanently open on a page with no control to clear
  it** (#22): the scanner writes `restored` but never closes the original `missing` event, and the
  review page renders its clearing control only when there are *current* issues. Red pill above an
  "All clear" body, no way out from the UI.
- **Every red count is a dead end** (#12 R3): the biggest, reddest number on the panel — the
  dashboard "Open issues" tile — is an inert `<div>`, while the visually identical stat on the
  collection page is a link (#18). The sidebar badge next to it has no accessible label and counts
  a *different population* (`missing` only vs `missing + modified`), so the two disagree.
- Plus the smaller honesty and reachability defects: three words for two proof states (#23), `/learn`
  handing the operator an `ots verify` command that cannot work without a Bitcoin node (#26), a
  batch of copy/deep-link/empty-state items (#32), a mobile layout where the running-operation badge
  squeezes the collection name to literally zero width (#33), and a Settings tab rendering dead
  radio cards that the rest of the app has taught the operator are clickable (#34).

Everything here is copy, links, CSS, and small code fixes. **No `.ots` proof is deleted by any code
path in this app** (#12's headline) — these are failures of Cairn's *description* of the evidence,
not of the evidence. The two places where real irreversible loss is possible (#15, #21) are
deliberately **not** in this change.

DESIGN.md references: §5 "Web panel (pages)" (the panel surfaces), §6 "OpenTimestamps handling"
(the verify backends and what a proof state means), §3 "Locked decisions" (watched folders are
read-only; the DB is an index, the guarantee is bytes + `.ots`).

## What Changes

### Verify path (#13, #19, #23, #34-part, #32-part)
- **`VerifyResult` gains `digest_mismatch: bool = False`**, set at both mismatch sites in
  `src/services/ots.py` (the file-digest-vs-proof mismatch and the Bitcoin merkle-root mismatch).
- **`verify_run` tests `digest_mismatch` *before* the pending branch** and renders a failure card
  whose copy states plainly that the bytes no longer match the notarized digest.
- **The `OtsError` fallback stops inheriting `fe.ots_state`.** A transport failure (explorer or node
  unreachable) is reported as *verification unavailable* — never as a proof that is merely young,
  and never as a green verdict.
- **`partials/verify_results.html` renders `m.ots_badge(f.state, "sm")`** instead of the literal
  `"complete"`. (`state` is already in the row dict from `_anchored_view`.)
- **`ots_badge`'s `"Incomplete"` becomes `"Pending confirmation"`**, so the badge, the tiles, the
  `/verify` verdict and `/learn` all use the same two words for the same two states.
- **`verify_result.html` derives its closing "verified via" sentence from `verified_via`** rather
  than asserting "Verified by explorer lookup" regardless of the configured backend.
- **`/verify` gets a real empty state** when nothing is anchored yet (today the empty state only
  renders when a search returned nothing).

### Dashboard & collection surfaces (#14, #18, #20, #31, #32-part)
- **The "Open issues" tile becomes a link when `issues > 0`**, with the `· Review ›` CTA that the
  collection page's `.mini-stat--link` already uses. Exactly one affected collection →
  `/collection/{id}/review`; several → `/collections`. At zero it stays an inert `<div>`.
- **The sidebar badge gets a `title`/`aria-label` and counts `missing + modified`**, so it stops
  disagreeing with the tile beside it. All four OOB swap sites are updated in lockstep, fed by one
  shared count helper so they cannot drift again.
- **A fourth "New — watched, not yet baselined" tile**, so `Total ≠ OK + issues` stops being
  unexplained; **"Verified OK" is renamed "Matching baseline"**; **`attention` and `alert` get
  distinct icons** (they currently share one).
- **The proof coverage claim becomes a ratio, not an assertion.** "all confirmed" is emitted only
  when there is something to confirm (`complete > 0`) *and* nothing outstanding (`pending`,
  `incomplete` and unstamped all zero). Otherwise the tile shows `complete / stampable` plus an
  amber "N not stamped — Stamp all" line. The unstamped count **excludes `status='missing'`** —
  that is what `mark_unstamped_pending` actually queues, so counting missing files would ship a
  permanent, un-clearable warning.
- **A zero-file collection reads "No files indexed yet"**, on the card legend and on the detail
  tiles, instead of "All files verified · all confirmed".
- **The collection-detail header stops offering Accept while there are issues.** When `issues > 0`,
  **Review issues** is the `btn--primary` and Accept is not in the header at all — the destructive
  path is reachable only from the page that explains it. When `issues == 0 and new > 0`, the
  harmless **Baseline new files** button stays, with a light confirm. Any remaining Accept form
  carries a confirm.
- **`collection_detail` honours `view` and `filter` query parameters**, threading `status_filter`
  into the *initial* `query_files` call and templating the Tree/List `is-active` class, so a deep
  link into the filtered list actually lands filtered.

### Review, acknowledgement vocabulary, and the restore ack (#17, #22, #32-part)
- **"Acknowledge" becomes "Mark reviewed"** on every surface, with hint copy that says what it does
  and does not do. The bulk actions carry their count (**"Mark all N reviewed"**), and the
  dashboard's all-collections variant — which reaches events not visible in the 20-row feed — gains
  an `hx-confirm`.
- **The review row's button styles are un-inverted**: the harmless per-file action is `btn--subtle`,
  the destructive Accept-all is the loud one.
- **The scanner's restore branch system-acknowledges the file's open `missing` events**
  (`kind="missing"`, `acknowledged_by=NULL`, matching the born-acked convention). Scoped to that
  kind — never a blanket `WHERE file_id = …`, which would also clear an open WORM `modified` event
  on the same file.
- **The review page renders the "Clear these alerts" half whenever `review_open > 0`**,
  independently of `total_issues`, with copy naming the case ("N alerts from files that have since
  been restored"). **Accept is not surfaced in that branch** — from an otherwise-empty page it would
  baseline every pending `new` file.
- Review step 3 copy → "Run **Scan now** on the collection page"; the truncation notice deep-links
  to `?view=list&filter=issues`; the Copy-paths buttons get a `.catch()` and a textarea fallback.

### Docs, settings and mobile (#26, #33, #34)
- **`/learn`'s verification instructions lead with the opentimestamps.org drag-and-drop**, state
  that the auditor needs **both** the file and the `.ots` (the export route serves only the proof),
  and say plainly that the CLI path requires a Bitcoin Core node. Both proof states are named in
  the pending-vs-anchored explanation.
- **Settings → Verification stops rendering dead radio cards.** The backend is env-only, so it is
  rendered as descriptive text naming `CAIRN_VERIFY_BACKEND` / `CAIRN_NODE_RPC_URL` and noting that
  a restart is required.
- **`.op-bar` is hidden below 768px** (345 − 92 = 253px, which fits inside a 320px row) and the
  detail page's status meta-cell stops clipping. This matters more now that alert emails deep-link
  operators straight into the panel from a phone.

## Non-goals
Sprint 1 is deliberately bounded. Out of scope, each tracked by its own issue:

- **The `accept_collection` scope-split and the full vocabulary rewrite** (#16, #17's deeper half).
  Sprint 1 does the renames, counts, confirms and button styles only; it does not add a scope
  parameter, a per-file variant, or new verbs.
- **A fleet-wide `/review` page** (#27). `GET /review` is a 404 today; nothing here links to it.
  The multi-collection case of the "Open issues" tile points at `/collections` until #27 lands.
- **The proof-overwrite and restored-file-digest guards** (#15, #21) — the only two places where
  real, silent, irreversible loss is possible. They need their own change and their own adversarial
  pass.
- **Any schema change or migration.** No new column, no new status, no CHECK rebuild. In particular
  **no terminal `status='gone'`** (#12's rejected fix 3): it needs a batch rebuild of a ~186k-row
  table, `uq_files_collection_relpath` then blocks re-adding the file at that path, and the
  missing-sweep would re-alarm the row on every scan.
- **DB-backed persistence for the verify backend** (#34). Persisting it in the panel without also
  overlaying it in the CLI would let the panel and the CLI disagree about how an integrity claim was
  verified — the worst possible disagreement in this product.
- **Restic / live "find in backup" integration** — still deferred so the repo can go public.

### Explicitly rejected fixes (from #12; do not re-derive them)
1. **Making `alert_count` / `_collection_status` ignore acknowledged-missing files.** One click on
   Mark-all-reviewed would then render a collection with eight permanently missing tax documents as
   "All clear" with a zero badge — a false negative on the tool's top-level signal.
2. **Default-filtering the event feed to unacknowledged events.** `added`/`restored`/`moved` are
   deliberately born-acknowledged so the rail reads as an activity log; an open-only default renders
   "No events recorded yet." on a healthy system.
3. **A new terminal `status='gone'`.** See above.
4. **`ots --no-bitcoin verify` in the `/learn` docs.** It exits 1 having verified nothing, which is
   worse than the current visible error.
5. **"Alert only — reversible by a rescan"** as hint copy for adopted changes. Not reversible: the
   scan already updated `row.sha256`, so a rescan matches and re-raises nothing.
6. **Labelling a proof download "existed by `<ots_stamped_at>`".** That is submission time, not
   block-confirmed time; the codebase deliberately refuses to invent it.
7. **A blanket `UPDATE events SET acknowledged_at WHERE file_id = …` in the restore branch.** It
   would also acknowledge an open WORM `modified` event on the same file. Scope to `kind="missing"`.
8. **Dropping the sidebar badge from the ack response's OOB swap.** It is what keeps a long-open
   page's badge current after a background scan.

### Correct as built — do not "fix" these
- The review page's **Acknowledge-vs-Accept contrast card**: the only place in the product where the
  distinction is explained, and it is explained well. This change re-labels its buttons and adds the
  restored-only branch; it does not replace the card.
- The **recovery panel** — tool-neutral, explicit that Cairn never restores files itself.
- The **collection card** (`partials/_collection_card.html`) — segbar + legend + red `Review →`
  deep-link. The pattern the dashboard tile is being made to copy.
- **Born-acknowledged informational events** (`added`/`restored`/`moved`).
- **`verify_run` refusing to fall back to the stored digest** when the file is gone from disk. It
  returns "File unavailable — cannot verify" rather than inventing a green result. This change fixes
  the copy *around* it and must not touch that behaviour.
- **`accept_collection` detaching events before deleting**, so history survives the CASCADE.
- **The dead-man's-switch machinery** — `/healthz` keyed on `kind='scan'` runs only, the startup
  reaper, the guarantee a scan always reaches a terminal state, `active_run()` as the single
  concurrency guard.
- **Move reconciliation** and **per-file "Last checked"** on every row.

## Impact
- **Affected specs:** `web-panel` (the verify verdict, the proof-state vocabulary, the dashboard
  tile/badge, the coverage claim, the collection-detail action hierarchy, the review page's
  restored-only branch, the deep-link parameters, `/learn`'s verification instructions, the
  Verification settings tab), `ots-notarization` (`VerifyResult.digest_mismatch` — a digest mismatch
  is reportable, not merely "not verified"), `integrity-scanning` (a restore closes the file's open
  `missing` events).
- **Affected code:** `src/services/ots.py`, `src/services/scanner.py`,
  `src/control_panel/routes.py`, `src/control_panel/templates/` (`base.html`, `dashboard.html`,
  `collection_detail.html`, `collection_review.html`, `learn.html`, `settings.html`,
  `_macros.html`, and the partials `_collection_card.html`, `_event_row.html`,
  `_events_controls.html`, `event_ack.html`, `events_feed.html`, `review_ack_row.html`,
  `review_row.html`, `verify_result.html`, `verify_results.html`),
  `src/control_panel/static/css/panel.css`, `tests/`.
- **Data migration:** none. Zero schema changes, zero Alembic revisions — so no `make migrate` step
  after deploy.
- **Backward compatibility:** total. No route is removed or renamed; `view`/`filter` on
  `collection_detail` are new *optional* parameters whose defaults reproduce today's render.

## Issue index
| Issue | Audit ref | Slice | Tasks |
|---|---|---|---|
| #13 verify says "pending" for a digest mismatch | A1 | A | 2.1–2.6 |
| #14 collection-detail Accept is primary + unconfirmed | A2 | B | 3.7–3.9 |
| #17 rename Acknowledge → Mark reviewed; scope/confirm bulk acks; un-invert row colours | A5 | C | 4.1–4.5 |
| #18 inert "Open issues" tile; unlabelled sidebar badge | A6 | B | 3.1–3.4 |
| #19 `/verify` hardcodes the green Anchored badge | A7 | A | 2.7 |
| #20 proof tiles claim "all confirmed" with no proofs | A8 | B | 3.5–3.6 |
| #22 restored file leaves its missing alert open | A10 | C | 4.6–4.8 |
| #23 "Incomplete" vs "pending" — three words for two states | A11 | A (badge) + D (learn) | 2.8, 5.3 |
| #26 `/learn` teaches an `ots verify` that needs a node | A14 | D | 5.1–5.2 |
| #31 zero-file collections report "All clear / All files verified" | A19 | B | 3.6 |
| #32 copy and deep-link batch (7 items) | A20 | A/B/C/D | 2.9, 3.10–3.12, 4.9–4.11 |
| #33 mobile: op badge squeezes the collection name to zero width | A21 | D | 5.6 |
| #34 Settings → Verification renders dead radio cards | A22 | D (settings) + A (verify_result) | 5.4–5.5, 2.10 |
