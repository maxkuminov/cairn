# Design notes — UX audit sprint 1

Only the decisions that go beyond what the issues already state. Everything else follows the issue
text verbatim; where the issue is explicit, that is the spec.

## Line-reference drift found while grounding this proposal (HEAD = `9249795`)

Verified against the working tree. Two of the issue's pointers do not survive contact:

- **#33 "the detail page's 140px meta-cell"** — there is **no `140px` anywhere in `panel.css`**.
  The clip comes from `.meta-cell__value { overflow:hidden; text-overflow:ellipsis;
  white-space:nowrap }` (line 497) inside `.meta-cell--wide { flex: 0 0 auto }` (line 495), which
  measures ~140px at 390px viewport. The fix is therefore to let the *status* meta-cell take a full
  row and stop ellipsizing under the breakpoint, not to change a width literal that does not exist.
- **#13 "both mismatch sites, ~`:491` and `:527`"** — both are in `_verify_via_explorer`
  (`want != detached.file_digest`, and the `mismatch = True` merkle branch). The **node backend
  (`_verify_via_cli`) has no mismatch site at all**: `ots verify -d` reports a mismatch as a plain
  non-success exit, indistinguishable from "not yet anchored". See D1.

Everything else (`routes.py:1377`, `verify_results.html:15`, `_macros.html:141`,
`collection_detail.html:26-38`, `dashboard.html:27-31`, `base.html:40-42`, `scanner.py:373`,
`collection_review.html:18-30`, `settings.html:256-277`, `verify_result.html:57`, `routes.py:504-508`,
`_collection_card.html:45/57`, `panel.css:777`) matches HEAD.

## D1 — a mismatch is reported only where it is *established*; the node backend reads *inconclusive*, never *pending*

`_verify_via_explorer` parses the `.ots` locally, so it knows two distinct things the CLI never
sees: whether the live digest is the one the proof commits to, and whether each Bitcoin
attestation's commitment equals the real block's merkle root. Those are **different failures with
different blame**, so they carry different flags (field list in D2): `digest_mismatch` blames the
*file* (the `want != detached.file_digest` return); `proof_mismatch` blames the *proof or the
explorer's block data* (the `if mismatch:` return after the merkle comparison) — a corrupted `.ots`
or an inconsistent explorer response produces it while the file's bytes are still exactly what was
stamped. Copy that blames the file for a `proof_mismatch` is a false alarm on the product's core
signal, which is why the two are never collapsed into one flag or one sentence.

`_verify_via_cli` shells out to `ots verify -d`; a digest mismatch there produces a non-zero exit
with no success line — indistinguishable from a proof that is not yet anchored, and equally
indistinguishable from a Bitcoin node that is down. Inventing a mismatch from stderr text would be a
regex guess on the CLI's wording, and a *false* mismatch is a false alarm on the same core signal.

**Decision:** the two mismatch flags are set only where they are established — the explorer backend,
which is the configured default and the one every homelab deploy runs (the node path needs a
reachable `bitcoind`). The node backend does **not** guess. It also does not get to keep today's
reassuring reading: its non-success return sets `inconclusive=True`, and the panel and the CLI
render that as a verdict that *names every possibility it cannot separate* — **"Not yet confirmed —
or the file no longer matches, or the Bitcoin node could not be reached; the Bitcoin-node backend
cannot tell these apart."** All three are named because all three produce the identical non-success
exit: `ots verify -d` reports an unanchored proof, a changed file and a dead node the same way.
Naming only the first two would invent a file-change scare for what is usually a down node, and
classifying by regexing the CLI's stderr is the guess this decision exists to refuse — the copy
carries the ambiguity instead. No mismatch is invented and no false reassurance is given: the
operator is told exactly what that backend established, which is nothing. Teaching the node backend to distinguish the cases (verifying locally the way the explorer
path does) is follow-up work for #24-adjacent verify hardening, not sprint 1 — recorded in the spec
delta as a scoped, documented limitation of the non-default backend rather than left implicit.

## D2 — a transport failure gets its own verdict, and every swallow point sets it

Issue #13 says only that the `OtsError` fallback must stop inheriting `fe.ots_state`. Two things are
wrong with treating that as the whole fix.

First, what the fallback *should* read. Both obvious answers are wrong:

- reusing the `warn` / "Proof pending confirmation" card tells the operator to wait for a network
  error that will never settle (the bug being fixed);
- reusing "Could not verify" implies something about the *file*, when Cairn learned nothing about
  the file at all.

Second — and this is what a route-level fix alone misses — **`ots_svc.verify` mostly does not raise
on a transport failure.** It swallows it into an ordinary-looking `VerifyResult`, so the route's
`except ots_svc.OtsError` never fires:

| Swallow point | Today's return | Reads in the panel as |
|---|---|---|
| `_verify_via_explorer`: every attestation's `_fetch_block_merkleroot` raised (the `best is None` return) | `verified=False, state="complete"`, errors joined into `message` | generic red "Could not verify" |
| `_verify_via_explorer`: *some* fetches raised, at least one attestation matched | `verified=True` | verified — correctly; see below |
| `_verify_via_cli`: non-zero exit, no success line (node unreachable) | `verified=False, state=proof.state` | **"Proof pending confirmation"** |
| `_run_ots` raises `OtsError` (binary missing / timeout) and it propagates out of `verify` | route `except OtsError` | inherits `fe.ots_state` → pending |

**Decision:** the outcome is carried on the result object, not reconstructed by each caller.
`VerifyResult` gains three plain optional fields beside `digest_mismatch` — dataclass defaults, no
enum, matching the existing style:

```python
digest_mismatch: bool = False       # live digest != the digest the proof commits to   (explorer)
proof_mismatch: bool = False        # attestation commitment != the block merkle root  (explorer)
transport_error: str | None = None  # the backend could not be reached; nothing was established
inconclusive: bool = False          # this backend cannot tell pending/mismatch/unreachable apart
```

Every row of that table is populated at its source. `_verify_via_cli` catches `OtsError` from
`_run_ots` and returns `transport_error=str(exc)` rather than letting it propagate, and sets
`inconclusive=True` on the non-success exit (D1). The explorer **accumulates** every
`_fetch_block_merkleroot` failure and attaches the joined reasons to **every** terminal result it
returns after the fetch loop — the verified one, the `proof_mismatch` one and the `best is None` one
alike. A fetch error is never dropped because some other outcome was decided first: an operator told
that a proof does not check out also needs to know that two of its three attestations could not be
fetched, since that is the difference between "this proof is bad" and "this proof is bad as far as
the half of it I could see". A recorded `transport_error` never downgrades a verified result (one
attestation confirmed against a real block *is* the proof) and never outranks a mismatch (verdict
order below); on those results it is diagnostic detail, and it is the verdict itself only where
nothing else was established. The route's `except OtsError`
fallback survives as a belt-and-braces net and now constructs
`VerifyResult(verified=False, state="none", transport_error=str(exc))`, dropping `fe.ots_state`
entirely.

**Verdict order** in `verify_run`, and the same order in `cli._cmd_verify`:

1. `live_unavailable` — there are no bytes to check
2. `digest_mismatch` → `danger`, "File no longer matches its proof"
3. `result.verified` → `ok`
4. `proof_mismatch` → `danger`, "This proof does not check out" (blames the proof, not the file)
5. `transport_error` → `unavailable`, "Couldn't check right now"
6. `inconclusive` → `unavailable`, "Couldn't confirm — pending, changed, or unreachable"
7. `state == "incomplete"` → `warn`, "Pending confirmation"
8. `state == "pending"` → `warn`, "Queued to stamp"
9. otherwise → `danger`, "Could not verify"

**A verified result outranks `proof_mismatch` — and the flag is defined so that conflict cannot
arise in the first place.** OpenTimestamps verification is *existential*: one Bitcoin attestation
confirmed against its real block is proof, and a proof may legitimately carry more than one
attestation. So `_verify_via_explorer` sets `proof_mismatch` **only when no attestation validated
and at least one mismatched**; a bad sibling beside a good one is kept as diagnostic detail in
`message` and never changes the verdict. That inverts today's aggregation, which tests `if mismatch:`
before it looks at `best` and so lets one bad sibling override a valid attestation with a red "this
proof does not check out". Crying mismatch over a proof that is genuinely anchored is the same class
of error as calling a changed file pending, pointed the other way — and on this product's core
signal, a false alarm is what teaches the operator to dismiss the real one. Ordering `verified`
above `proof_mismatch` in the verdict chain is belt-and-braces on top of that source-level rule.

Mismatch is evaluated **before** transport deliberately: a mismatch established before the network
failed is knowledge, and discarding it because a later fetch timed out would throw away the one
finding that matters. `digest_mismatch` stays above `verified` for the mirror reason: it is only ever
set on a not-verified return, and if both were ever set, "these are not the bytes that were stamped"
is the finding that must win.

**Losing the verdict is not losing the disclosure.** Precedence decides the *headline*, not what the
card and the CLI line are allowed to say. Whenever a `transport_error` rides along with a verdict
that outranks it — `verified` or `proof_mismatch` — the consumer appends it as a diagnostic line
under the headline: on a verified result, *"Note: N attestation lookups failed; the verdict is based
on the attestations reached."*; on a proof mismatch, the same note, qualifying the mismatch as
established only over the attestations that could be fetched. Attaching the reason to the result
(above) and then rendering only the winning branch would leave the operator with a categorical
"this proof does not check out" over a proof half of which was never checked — the swallow moved one
layer out rather than being closed. The other verdicts need no such line: on `transport_error` and
`inconclusive` the transport failure *is* the headline.

`incomplete` and `pending` are **two branches, not one** (D13): `incomplete` is submitted and waiting
on Bitcoin, so it reads "Pending confirmation"; `pending` is queued locally and never submitted, so
it reads **"Queued to stamp"** and carries no awaiting-confirmation wording anywhere in its copy.
Collapsing them tells an operator to wait for a confirmation that nothing has asked for, which is
exactly how a stuck stamping queue hides behind the wording for a healthy young proof.

`unavailable` is a **fourth, neutral verdict style** (one small CSS addition) — not `warn`/"pending"
(the bug being fixed), and **not `danger`**: issue #24, the fuller verify-failure-card split queued
for a later change, is explicit that an unreachable explorer must never render red, because red for
a network blip either panics the operator or, by crying wolf, teaches them to dismiss the red card
that means a genuine mismatch. Adding the neutral style now is a deliberate small pull-forward from
#24 so the transport branch is not styled twice. (Supervisor override of the drafter's original
`danger` choice, for exactly that reason — the override governs *every* transport and inconclusive
surface in this change, tasks and spec deltas included.)

## D3 — one helper owns the sidebar badge count

The badge is currently computed in three places (`_base_context`, `_event_feed`, and `ack_event`'s
inline query) and rendered in four (`base.html`, `events_feed.html`, `event_ack.html`,
`review_ack_row.html`). #18 changes what it counts (`missing` → `missing + modified`); three
independent edits is how it drifted from the tile in the first place.

**Decision:** add one `async def _alert_badge_count(session, collection_ids) -> int` in `routes.py`
and call it from all three sites. The four templates render from the same `alert_count` key, so the
label copy lives in exactly one macro-free place per template but the *number* has a single source.

Label copy (used for both `title` and `aria-label`): `"{n} files missing or changed — open dashboard"`
(singular `1 file missing or changed — open dashboard`).

## D4 — the multi-collection "Open issues" href is `/collections`, not `/review`

`GET /review` is a 404 today and is #27's work. Linking to it now would ship a red tile pointing at
an error page — strictly worse than the inert `<div>` it replaces.

**Decision:** exactly one affected collection → `/collection/{id}/review`; two or more →
`/collections`. Computed in `dashboard()` as `issues_href`, so when #27 lands only that one
expression changes. At zero issues the tile stays a `<div>` (no href, no hover, no pointer).

## D5 — what "coverage" means, where the counts come from, and over which population

#20 and #31 both need a count of files that *could* be stamped but are not — and the arithmetic has
to survive a missing file and a tripwire-only collection.

**Decision:** every component of a coverage claim is counted over the **same population**: files
whose `status != 'missing'`, i.e. exactly what `mark_unstamped_pending` queues and what "Stamp all"
acts on. `_ots_counts` therefore returns four extra keys from one additional grouped query carrying
that predicate — `complete_active`, `incomplete_active`, `pending_active`, `none_active` — beside
the existing unqualified totals. With `stampable = file_count - counts.missing`, the identity

```
complete_active + incomplete_active + pending_active + none_active == stampable
```

always holds. Mixing populations is precisely how a tile lies: one *missing* file with a complete
proof plus one present unstamped file would otherwise read `1 / 1` confirmed while nothing stampable
is confirmed at all. The unqualified `complete`/`pending`/`incomplete`/`none` keys stay for callers
that want raw totals, but **no coverage claim may be computed from them**.

Derived, in `_collection_view`:

- `anchored_ratio = f"{complete_active:,} / {stampable:,}"`
- **"all confirmed" is emitted only when `complete_active == stampable > 0`.** Anything else shows
  the ratio. Given the identity above, that single comparison is equivalent to "nothing queued,
  nothing awaiting confirmation, nothing eligible-but-unstamped", and cannot drift out of step with
  the four counts the way four separate conditions can.
- `none_active > 0` adds an amber `"{none_active:,} not stamped"` line next to the existing
  Stamp-all affordance. Counting missing files here would ship a warning the operator can never
  clear (#20's stated caution).
- The two not-yet-confirmed states are named separately and **never summed** (D13):
  `"{pending_active:,} queued · {incomplete_active:,} pending confirmation"`, dropping whichever
  half is zero.
- **`file_count == 0` short-circuits every surface**, not just the legend and the tiles but the
  shared status pill: `_collection_status` returns a new `"empty"` status — its `counts` dict sums
  to zero exactly when the collection has no files, so no extra argument is needed — and
  `_STATUS_META["empty"]` is a muted, non-green
  `("No files indexed", "var(--text-3)", "folder", "muted")` — `folder` is already in the icon set
  and is not spoken for by any status, so it does not collide with the `minusCircle` D6 assigns to
  `alert`. Because `_op_status_c`,
  `_collection_view` and `_base_context` all read that one map, the dashboard card pill, the detail
  header pill, the `partials/op_status.html` fragment and the sidebar dot change together. Fixing
  only the legend is how "All clear" survives a fix aimed at it. A root that is a typo or a failed
  bind mount is a configuration failure to surface, not a clean bill of health.
- The **review page's zero-issue card** is the fourth surface, and it does not go through
  `_STATUS_META`: it branches on `total_issues == 0` and prints a green "All clear — nothing is
  missing or changed in this collection right now". For an empty collection that is the same false
  green, one page further in — "nothing is missing or changed" is a claim about files that were
  checked, and none were. `collection_review.html` therefore splits that branch on the view's
  `is_empty` and renders the muted "No files indexed yet" card (same `folder` icon, `--text-3`)
  instead, pointing at the collection page rather than congratulating the operator.

**Global (dashboard) proof coverage is computed strictly over `ots_mode == 'perfile'` collections.**
Tripwire (`none`) collections stamp nothing and their stamp route rejects them, so folding their
files into the ratio produces a "not stamped" warning no operator action can ever clear — while
excluding them from the numerator alone would restore a false "all confirmed". They are out of the
numerator *and* the denominator, on every surface, and the tile's sub-line names the population it
summarises so the exclusion is visible rather than silent. Per collection nothing changes: a `none`
collection renders the Last-scan tile, not the proof tile.

## D6 — distinct `attention` / `alert` icons without inventing an SVG

`_STATUS_META` gives both `attention` and `alert` the `"alert"` warning triangle. The icon set
already distinguishes these two populations elsewhere: `status_badge` uses `alert` for `modified`
and `minusCircle` for `missing`.

**Decision:** `attention` (WORM modified) keeps the `alert` triangle; `alert` (missing) becomes
`minusCircle`, matching every other place a missing file is drawn. No new SVG, and the pill icon
now agrees with the row icon for the same file.

## D7 — the collection-detail action hierarchy

Per #14, with the header's button set made explicit so no state is left ambiguous:

| State | Header buttons (left → right) |
|---|---|
| `issues > 0` | Edit · Scan now · Stamp all · **Review issues (`btn--primary`)** — no Accept |
| `issues == 0 and open_events > 0` | Edit · Scan now · Stamp all · **Review issues (`btn--primary`)** — no Accept |
| `issues == 0 and open_events == 0 and new > 0` | Edit · Scan now · Stamp all · **Baseline new files (`btn--primary`, `onsubmit` confirm)** |
| `issues == 0 and open_events == 0 and new == 0` | Edit · Scan now · Stamp all |

"Baseline new files" keeps `btn--primary` because in that state it is the only call to action and it
is harmless (`new → ok`, no deletion, no baseline rewrite). Its confirm is deliberately *light*
("Baseline N new files as the expected version?") — a heavy warning on a harmless action trains the
operator to click through the heavy warning on the destructive one.

**"Harmless" is a claim about two populations, so the gate tests both.** `accept_collection` is one
unscoped verb: it promotes `new → ok` *and* acknowledges every open event on the collection. Zero
issues alone does not make it harmless — a file that went `missing` and has since been restored is
back to `ok` while its alert stays open (D9's restored-but-unreviewed state), so a collection can
read `issues == 0` with a real, unread alert outstanding. Offering "Baseline new files" there would
clear that alert under a light confirm describing a new-file promotion. Hence the button renders
**only when `issues == 0` and the open-event count is 0**, and both zeroes are asserted inside the
D14 fingerprint rather than only at render time.

That gate does not strand the restored-file case: its outlet is the review page, which this same
change makes reachable whenever the collection has open events regardless of file state (#22 / D9) —
the operator marks the restored file's event reviewed there, the open-event count falls to zero, and
"Baseline new files" appears on the next detail-page render. The clearing of an alert stays on the
page that shows the operator what they are clearing.

**The destructive Accept survives only on the review page**, where the contrast card explains it and
its `onsubmit` confirm already exists. The side benefit #14 notes holds: a deletion landing between
page render and click can no longer be swept up by a stale header label.

*Overridable:* demoting "Baseline new files" to `btn--subtle` is a one-class change if Max wants
`btn--primary` reserved for navigation only.

**Render-time visibility is not the guard.** The header is decided when the page is built, and the
form it renders posts to the same unscoped `/collection/{id}/accept` route as before. A scheduled
scan landing between render and submit turns a button labelled "Baseline 40 new files" into a
deletion of `missing` rows the operator has never seen — and the light confirmation they clicked
described something else entirely. Visibility rules cannot fix that; only the route can.

**Decision:** the detail-page form is bound to the population it was rendered for by the shared
**population fingerprint** guard specified in **D14**, which the review page's Accept uses
identically. A submit-time recount alone would not do it: the recount is a separate statement from
the accept, and the same scan can claim, run and commit between the two.

**The review page is not exempt.** Its list is a *render*, and the claim that makes it the explained
path — "these are exactly the files this button will adopt" — stops being true the moment a scan
records another missing file. The operator would then delete a record they never saw, from the one
page whose entire purpose is that they saw it. So `collection_review_accept` carries the same hidden
field, runs the same check inside the same transaction, and refuses the same way.

This is a **guard, not the vocabulary split.** A scoped `accept_collection` (a new-files-only
variant, per-file accept) is #16 and a later change; until it exists, the only safe rule is that an
accept-family form acts on the population it was rendered for and refuses when that population has
moved. #16's scoped verbs subsume this guard — a per-file accept carries its own scope and needs no
fingerprint — which is why D14 is deliberately the minimum honest mechanism rather than a new
abstraction to keep alive.


## D8 — which button styles get swapped

#17's inversion is between two *different* controls, so name them precisely:

- per-file **Mark reviewed** (`review_row.html`, and the same control in `_event_row.html`) →
  `btn--subtle` in all cases, dropping the `btn--danger if status == 'missing'` conditional. It is a
  reading-log write; it changes nothing about the file.
- the review panel's **Accept all changes** (`collection_review.html`) → `btn--danger`, inside the
  amber `resolve__opt--accept` card it already sits in. It rewrites baselines and deletes rows.

## D9 — the review page's restored-only branch

`collection_review` already computes both `total_issues` and `review_open`; **no route change is
needed** — the template's outer `{% if total_issues == 0 %}` is what suppresses the control. Three
states after this change:

| `total_issues` | `review_open` | Render |
|---|---|---|
| 0 | 0 | today's "All clear" card, unchanged |
| 0 | > 0 | "All clear" card **plus the Acknowledge half only** of the resolve panel, headed *"{n} alerts from files that have since been restored"* |
| > 0 | any | today's full page (recovery panel + both halves + list) |

**Accept is never rendered in the middle row.** From an otherwise-empty page it would silently
baseline every pending `new` file in the collection — the exact class of unscoped irreversible verb
#12 R2 is about.

## D10 — the restore ack rides inside the batch that commits the restore

The walk buffers rows and commits in batches (`_drain`); issuing an `UPDATE` per restored file would
autoflush mid-walk once per restore and scale badly on a mass restore. But deferring the
acknowledgement to the end of the scan is worse than either: `_drain` is called every `BATCH` files
*and* unconditionally after the walk, so by then it has already committed the restored rows'
`status='ok'` and their `restored` events. A failing acknowledgement would then leave exactly the
half-updated state the spec forbids — a healthy file carrying an open `missing` alert, and no run
that will ever revisit it.

**Decision:** the restore branch appends `row.id` to a `restored_ids: list[int]` buffer, and
`_drain` — the function that owns the batch transaction — drains that buffer **before its own
`await session.commit()`**, over the ids accumulated since the previous drain, one statement per
≤500-id chunk (SQLite's bound-parameter ceiling is 999):

```python
update(Event)
  .where(Event.file_id.in_(chunk),
         Event.kind == "missing",
         Event.acknowledged_at.is_(None))
  .values(acknowledged_at=now, acknowledged_by=None)
```

The buffer is cleared **only after that commit returns**, the same discipline `added_buffer` already
follows, so nothing is dropped by a rollback. If the UPDATE raises, it raises *inside* `_drain`,
before the commit: the batch's restores do not commit either, the exception reaches the scan body's
`except`, the session is rolled back and the run finalizes `error` — the ordinary scan-failure path,
with nothing half-updated. That is the failure-injection scenario in the spec delta.

`kind == "missing"` is load-bearing (#12's rejected fix 7): a blanket `file_id` predicate would also
clear an open WORM `modified` event on the same file. `acknowledged_by=None` marks it a *system*
ack, matching the born-acked convention already used for `added`/`restored`/`moved`.

Ordering notes. Restores are classified during the walk, while every `missing` event of this run is
written by the missing-sweep *after* the walk's final `_drain` — so this can never acknowledge an
alert the same scan just raised (a file also cannot be both `missing` and `restored` in one pass).
Restored rows come from the pre-scan `existing` snapshot, so their `id` is already assigned and the
ack needs no flush beyond the one `_drain` performs anyway.

## D11 — `view` / `filter` on `collection_detail`

New optional query parameters, both validated against a whitelist and both defaulting to today's
behaviour (`view="tree"`, `filter="all"`).

- `filter` is threaded into the **initial** `query_files` call for the list view. #32 is explicit
  about why: a checked "Issues" radio over an unfiltered list is worse than no radio at all.
- The tree view's `query_files(prefix=…)` stays **unfiltered**. Filtering a directory listing would
  make folder counts and issue roll-ups disagree with the rows beneath them, and the filter control
  is CSS-hidden in tree view anyway (`panel.css:777`).
- Because of that, **a `filter` other than `all` with no explicit `view` implies `view="list"`** —
  otherwise the deep link lands on a tree with an invisible, inapplicable filter. An explicit
  `view=tree` is honoured as given.
- The template's `data-view` and the Tree/List `is-active` classes are templated from `view` instead
  of being hardcoded.

## D12 — file ownership across the parallel slices

Three files are touched by more than one slice. Ownership is by **function / region**, and the
boundaries are chosen so the regions are far apart in the file:

**`src/control_panel/routes.py`**

| Region | Owner |
|---|---|
| `verify_run` (and only it) | A |
| `_STATUS_META`, `_collection_status`, `_ots_counts`, `_op_status_c`, `_collection_view`, `_base_context`, `_event_feed`, `dashboard`, `ack_event`, `collection_detail`, `collection_accept`, `collection_review`, `collection_review_accept`, new `_alert_badge_count`, new `_read_population` + `_population_fingerprint` | B |
| — (C owns no routes.py region; `review_open` and `total_issues` are already in the review context, and D14's `population_fp` / `stale` keys are published by B's `collection_review`) | C |
| `settings_page` context only | D |

**`src/control_panel/static/css/panel.css`**

| Region | Owner |
|---|---|
| the two existing `@media (max-width: 768px)` blocks, and a new appended `/* --- ux-audit sprint 1: mobile --- */` section | D |
| a new appended `/* --- ux-audit sprint 1: tiles & links --- */` section at EOF **only** | B |
| a new appended `/* --- ux-audit sprint 1: verdict --- */` section at EOF **only** (the four `.verdict--unavailable` rules mirroring `--warn`, D2) | A |

Each slice appends its own marked section at EOF and edits no other slice's region; on merge the
three appended blocks land in whatever order the merge produces, which is fine because they share no
selector.

`src/cli.py` belongs to **A**: `VerifyResult` is a contract with two consumers, and the CLI's
`_cmd_verify` branches on `result.state == "incomplete"` before anything else, so leaving it out of
the slice that changes the contract ships the #13 false negative on the command line while fixing it
in the panel. `src/services/scanner.py` remains **C**'s alone.

**Templates** are single-owner throughout, including the two that look like they straddle:

- `partials/review_ack_row.html` → **B** (only its `sidebar-alert-badge` OOB span changes; the
  `review_row.html` it includes is C's).
- `partials/verify_result.html` → **A** (its #34 half — the `verified_via`-derived sentence — travels
  with the verify work, not with `settings.html`).
- `learn.html` → **D** (both #26's verification section and #23's pending-vs-anchored addition;
  A owns only the `_macros.html` badge label).
- `collection_detail.html` → **B** (the #14 header, the #32 browser toolbar, and D14's hidden
  `population_fp` field in the baseline form).
- `collection_review.html` → **C**, *including* the two lines D14 needs there: the hidden
  `population_fp` field inside the Accept form (the same hunk C already edits to give it
  `btn--danger`, D8) and the `stale` banner. The alternative — a B-owned partial that C has to
  `{% include %}` — still puts a cross-slice edit in C's file, so it buys nothing but an extra file.
  The seam is a **context contract** instead: B's `collection_review` publishes `population_fp` and
  `stale`; C renders them. Both halves land in the same integration merge (§6.1 checks it), and the
  route **fails closed** on an absent or empty field, so a half-merge refuses accepts rather than
  accepting them unguarded.

`partials/op_status.html` → **B** (the zero-file status pill, D5). It is *included* by
`_collection_card.html` and `collection_detail.html`, both of which are also B's, so the three move
together.

**Tests**: each slice owns one new module. A shared `tests/conftest.py` (the `cairn_env` fixture and
a `seed_collection` helper, lifted from `tests/test_panel.py`) is created **once in section 1,
before fan-out**; `tests/test_panel.py` keeps its own module-level copies untouched, so nothing
existing changes behaviour. Slices A and C additionally append to `tests/test_ots.py` and
`tests/test_scanner.py` respectively — one slice each, no sharing. A's CLI regression test lives in
A's own new `tests/test_ux_verify.py`, so no slice creates a `tests/test_cli.py` the others might
also reach for.

## D13 — two not-yet-confirmed proof states, two names

`ots_state` has two pre-confirmation values and they mean different things to an operator:
`pending` = queued locally and **not yet submitted** to a calendar (there is nothing to wait for on
Bitcoin yet — a backlog, possibly a stuck one); `incomplete` = **submitted, awaiting Bitcoin
confirmation** (a few hours, then the daily `upgrade` pass completes it). Today the badge calls them
"Pending" and "Incomplete" while the card footer, the detail tile and the dashboard tile *sum* them
and label the total "pending confirmation" — so a file that was never submitted is reported as
awaiting confirmation, and a stuck queue is indistinguishable from a young proof.

**Decision:** one name per state, on every surface:

| `ots_state` | Name | Surfaces |
|---|---|---|
| `pending` | **Queued to stamp** | `ots_badge`, card footer, detail tile, dashboard tile, `/learn` |
| `incomplete` | **Pending confirmation** | the same, replacing "Incomplete" |
| `complete` | **Anchored** | unchanged |

Summaries never add the first two together. Where both are non-zero the line reads
`"{pending:,} queued · {incomplete:,} pending confirmation"`; where one is zero its half is dropped
and the other reads on its own. This is #23's requirement taken one level deeper than the issue
states it: the fix is not to spend one word on both states, it is to stop presenting them as one
state.

*The same two names on the verify card when there is no proof at all.* `verify_run` builds its
verdict from a `VerifyResult`, and there is no result to build one from when the file has no stored
`ots_path` — which is exactly the state a `pending` (queued, never submitted) or `none` (never
stamped) row is in. Those fell through the ladder to the red **"Could not verify"** fallback, whose
copy says the file's contents "may have changed since it was recorded". Nothing was recorded and
nothing was checked, so that is a false accusation in the one direction this product must never get
wrong. Two branches sit before the fallback, reading the state off the **file row** (the only honest
source when there is no result): `ots_state == "pending"` → the same amber **"Queued to stamp"** as
the result-carrying branch, and `ots_state == "none"` → a neutral **"Not notarized yet"** that says
no proof has been made and the file is still watched for changes. The red fallback keeps only the
case that deserves it: a row claiming `incomplete`/`complete` while nothing verifiable came back.
The card's proof-download button is gated on a stored proof for the same reason — the export route
409s without one, and a download button under "no proof yet" is a promise the page cannot keep.

## D14 — the accept-family population fingerprint (one guard, two routes)

Two routes call the unscoped `accept_collection`: `collection_accept` (the detail page's
Baseline / Accept form, D7) and `collection_review_accept` (the review page's "Accept all changes").
Both render a page and then act on whatever the collection looks like at *submit* time, and the
scheduler can complete a scan in between. Neither a render-time visibility rule nor a submit-time
recount closes that: the recount is a separate statement from the accept, so a scan can claim, run
and commit in the gap between them and the accept still deletes `missing` rows the operator never
saw. `active_run()` has the same shape of hole — a collection with no run in flight when it is
checked can have one a millisecond later.

**Decision — one mechanism, used identically by both routes.**

*The fingerprint.* Each accept-family form renders a hidden `population_fp` field: the SHA-256 of a
canonical description of the population that button claims to act on.

| Form | Scope tag | Population hashed |
|---|---|---|
| detail-page **Baseline new files** | `baseline-new` | `open_events=<the open-event set, by identity>` (empty whenever this form is rendered at all) + `issues=<missing+modified count>` (zero — both assertions travel *inside* the hash) then the collection's `new` files |
| review-page **Accept all changes** | `review-accept` | `open_events=<the open-event set, by identity>` + the collection's `missing` + `modified` files |

*Canonical encoding.* Four ASCII control characters do the framing: **US** = `\x1f` between fields,
**RS** = `\x1e` between records, and **GS** = `\x1d` / **FS** = `\x1c` one level down, inside the
header's open-event component.

- **Header** —
  `f"{scope}\x1f{collection_id}\x1f{collection.created_at.isoformat()}\x1fopen_events={events}"`,
  where `events` is the GS-joined, id-ordered `f"{id}\x1c{kind}\x1c{detected_at.isoformat()}"` of
  every event with `acknowledged_at IS NULL` (empty string when there are none); and for
  `baseline-new` only, a further `f"\x1fissues={n}"`.
- **One record per file** —
  `f"{id}\x1f{len(relpath.encode('utf-8'))}\x1f{relpath}\x1f{status}\x1f{sha256 or ''}\x1f{first_seen.isoformat() if first_seen else ''}"`.
- **Order** — records sorted by `relpath`, which is unique within a collection
  (the ORM's `uq_files_collection_relpath` unique constraint on `(collection_id, relpath)`), so the
  order is total and deterministic.
  Deliberately **not** sorted by `id`: `id` is the field this encoding distrusts.
- **Preimage** — `header + RS + RS.join(records)`; the fingerprint is the hex SHA-256 of its UTF-8
  encoding. (Every stored `relpath` round-trips through UTF-8 — `tolerate-unencodable-paths` skips
  the ones that do not before a row is ever created — so the encode cannot raise.)

The byte-length prefix on `relpath` is what makes the encoding injective: a filename may legally
contain any byte but `/` and NUL, US and RS included, so without the length the record stream would
be ambiguous and two different populations could be made to hash equal. Framing by length removes
that by construction rather than by escaping.

*Why each field is in the preimage.* The scope tag and `collection_id` stop a fingerprint minted for
one form or one collection validating another. `status` rides beside the id because a file flipping
`new → modified` between render and submit changes what accepting it *means* without changing the id
set. **`relpath` and `sha256` are there because a SQLite integer id is not a durable identity.**
`files.id` and `collections.id` are `INTEGER PRIMARY KEY` **without** `AUTOINCREMENT`, so after a
delete SQLite may hand the freed rowid to the next insert: an accept deletes the highest-id `missing`
row, a later scan inserts a different file that reuses that id, and a scan marks it `missing` again —
and an id-and-status-only preimage is byte-identical for two populations that share no file. Adding
`relpath` pins the *logical file* (the unique key within the collection), `sha256` pins the
*content generation* (a same-path file restamped with different bytes is a different record to
accept), and **`first_seen` pins the *row generation***. `first_seen` is `NOT NULL` and is written
**at row insertion** (the scanner sets it on first sight; `_reconcile_moves` deliberately carries it
across a rename precisely because it is the row's birth mark) and is never rewritten in place — so
the one case `id + relpath + sha256` cannot separate is separated: an accept deletes
`archive/a.pdf` (id 17, digest D), the identical bytes are restored at the same path, a scan inserts
a fresh row that reuses the freed rowid 17 and a later scan marks it `missing` again. Every other
field of that record is byte-identical to the stale form's, and without `first_seen` that form would
validate and delete a generation of the record the operator never saw. All three are already loaded
wherever the guard runs: `collection_review` and `query_files` both
select whole `FileEntry` entities, so the columns are on the ORM object and the fingerprint's own
`SELECT` adds no join and no new column. A NULL `sha256` (an unhashed row) encodes as the empty
string and simply contributes no content pinning — `id + relpath + status` still pin the logical
file. The collection's own `created_at` (NOT NULL; `_get_owned_collection` already loads the whole
row) does the same job one level up: a deleted-and-recreated collection that reuses `collection_id`
gets a different creation timestamp, so an old fingerprint cannot validate against the replacement.
(`first_seen` is a `datetime` that is never NULL on a stored row; it is encoded defensively as the
empty string if it ever reads NULL, which contributes no generation pinning rather than raising.)

*The event population is inside the guard too, by identity.* `accept_collection` does not only touch
file rows — it acknowledges **every open event on the collection**, and that population is not
derivable from the file rows. A file can go `modified`, then `missing`, then be restored between
render and submit: its status returns to `ok` (so the protected `missing` + `modified` set can come
back to exactly what the form was rendered for) while its `modified` event stays open by design —
D9's restored-but-unreviewed state. Without an event term the fingerprint matches and the accept
silently clears an alert that was never on the rendered page. So the header carries `open_events=`
**the open set itself, by identity**, produced by the same encoder on both sides: at **mint** while the page is rendered, and at **check** inside the
write-locked transaction — in both cases read in the same statement as the file rows (see *one read*
below), so the whole preimage describes one consistent state and any change to which alerts are open
between render and submit is a refusal.

A **count** is the wrong granularity, and this is the one place the first implementation was wrong.
The count is stable under a real, reachable ABA: the single open `missing` event on the listed file
is acknowledged — by the scanner's restore-ack, or by the operator in another tab, or by
`review/ack-all` — and a fresh incident then opens another `missing` event on the same file. The
file record can be back to exactly its rendered value (same id, path, status, digest, `first_seen`)
and the count is 1 both times, so a count-based preimage matches and the accept silently closes an
alert nobody has seen. That is not a theoretical replay; it is a file going missing twice.
`id` + `kind` name the incident, and `detected_at` pins its **generation** for the same reason
`first_seen` does for a file row: `events.id` is a rowid without `AUTOINCREMENT`, and an event *can*
be deleted (`events.file_id` is `ON DELETE CASCADE`, and `_reconcile_moves` deletes `added` file
rows), which frees the id for reuse. The set is ordered by `id` so the encoding is deterministic;
`kind` and the ISO timestamp cannot contain GS or FS, and the CHECK constraint keeps `kind` inside a
fixed vocabulary, so the component is unambiguous by construction rather than by escaping.

The review page's fingerprint is computed **over the whole protected population, not over the
rendered rows**: the list is capped at `REVIEW_ROW_LIMIT` (500) while accept acts on all of it, so
hashing the visible rows would leave every issue past the cap outside the guard — the exact
collections (a whole deleted folder) where the guard matters most.

*One read feeds the render and the mint.* Not two queries with the same `WHERE` clause — **one**.
Python's `sqlite3` runs in legacy transaction mode: a `SELECT` does not open a transaction, so two
consecutive `SELECT`s on one connection are two independent snapshots and a scanner can commit
between them. A review page that read its visible rows in one statement and minted the fingerprint
in another could therefore publish a fingerprint for a population it never displayed; an unchanged
POST would then validate and delete a `missing` row the operator never saw — the guard's own mint
reintroducing the accident the guard exists to prevent. So `_read_population(session, collection,
scope)` returns one `_Population` snapshot — the protected file rows (uncapped, with the columns the
page renders), the open-event set, and for `baseline-new` the `missing + modified` count — read as a
**single `UNION ALL` statement** across the two tables, which SQLite evaluates inside one implicit
read transaction. Everything downstream is derived from that value in Python: the rendered rows are
a *slice* of it (re-sorted missing-first), the recovery copy list is a slice, "N issues" is its
length, the "need action" pill is the length of its event set, and `_population_fingerprint(collection,
pop)` — now a pure function over a snapshot — hashes exactly those rows. The detail page's D7 gate is
derived from the same snapshot as its mint, so the form can never be *shown* for one state and
*signed* for another (`_collection_view`'s numbers are kept only as a cheap pre-filter that decides
whether reading the `new` set is worth it at all, so an ordinary render of a large collection still
does not materialize it). The POST side calls the same `_read_population` + `_population_fingerprint`
pair under the write lock, so mint and recount cannot encode the same state differently — there is
one encoder, not two.

One read *that feeds a hashed claim*, that is. The per-row "Mark reviewed" lookup
(`_latest_events_by_file`) stays a separate query: it decorates rendered rows with the event id
behind each row's button and contributes nothing to the fingerprint, and `ack_event` is idempotent
(it no-ops on an already-acknowledged event), so a stale row button cannot destroy anything. The
rule is that everything the guard *hashes* — and everything the page *claims* about that population
— comes from the one snapshot.

The union is the price of atomicity across two tables: file rows, open events and the issue count are
padded to one 10-column row shape and tagged in the first column. The alternative — forcing an
explicit read transaction on every GET — was rejected for the same reason `BEGIN IMMEDIATE` was:
it puts transaction machinery on ordinary page renders to fix a problem that one statement already
solves.

*The check is atomic with the act.* The POST does not "check, then call". It opens **one write
transaction** and does both inside it:

1. **take the write lock first**, before the reads the guard depends on — one no-op write against
   the collection's own row (`UPDATE collections SET name = name WHERE id = :id`). SQLite escalates
   a transaction to a writer on its first write statement and holds that lock until commit or
   rollback, so from here no other connection can commit anything;
2. recompute the fingerprint — file rows **and** the open-event count read inside this
   transaction — and, for the detail form, re-assert `issues == 0` and `open_events == 0`;
3. compare it with the submitted value — an absent or empty field counts as a mismatch, so the
   guard **fails closed**;
4. only on a match, call `accept_collection(session, …)`, whose own trailing `commit()` is this
   transaction's single commit.

SQLite's single-writer serialization under WAL is what makes this a guard rather than a narrower
race: the recount and the accept's own `UPDATE`/`DELETE`s sit in the same transaction as the write
lock, so a scan cannot interleave between them — it blocks on the lock (`busy_timeout=5000` is
already set on every connection) and commits strictly before or strictly after, never during.
`collections_svc.active_run()` is still checked, but it is now **belt-and-braces**: it catches the
*long* window (an operation in flight that is going to change the population) rather than being the
guard against the short one.

The engine-wide alternative — `BEGIN IMMEDIATE` via a driver-level `begin` hook — is rejected: it
would make every read-only page render take the write lock and queue behind a running scan.

*Taking the lock can fail, and a failure is a refusal.* Step 1 is where contention lands: the no-op
`UPDATE` is the statement that acquires or upgrades the writer transaction, and it can raise
`sqlalchemy.exc.OperationalError` wrapping SQLite's `SQLITE_BUSY_SNAPSHOT` (another writer committed
since this session's read snapshot — that *is* drift), `SQLITE_BUSY` (the writer lock was still held
when `busy_timeout=5000` expired — a long scan or another large accept) or `SQLITE_LOCKED`. All
three are **handled exactly like a fingerprint mismatch**: `await session.rollback()`, no call to
`accept_collection`, and the same fail-closed `303` to `/collection/{id}/review?stale=1`. Left
uncaught they surface as an HTTP 500, which is the promise of a refusal broken in the one situation
the guard exists for — and a 500 on a POST is exactly what invites the operator to retry the
destructive action blind. The catch is **narrow by classification, not by breadth**: only
`OperationalError` whose SQLite code is one of those three (matched on the driver exception's
`sqlite_errorname` where present, falling back to the message text) is converted; every other
`OperationalError` or `DatabaseError` propagates untouched, because a corrupt datastore or a schema
error must not be reported to the operator as "the collection changed since the page loaded". The
guard's own `SELECT`s run after the lock is held, so this is the only statement in the sequence that
needs the treatment.

*The refusal.* No mutation of any kind, and a **303 to `/collection/{id}/review?stale=1`** — the
page that lists exactly the issues that caused the refusal, with the clearing controls in reach. The
panel has no flash-message mechanism, so the marker is the message: `collection_review` accepts
`stale` as a whitelisted query parameter (only `1` is recognized; anything else is ignored, exactly
as `view` / `filter` are handled in D11) and renders a dismissable banner — *"This collection
changed since the page loaded — the list below is current."* A bare redirect would land the operator
on an ordinary review page with no account of why their click did nothing, which reads as a broken
button and invites them to click it again.

*Coverage, stated plainly.* `accept_collection` mutates three populations, and the guard now binds
each of them: the **file rows it deletes and rewrites**, by identity *and* generation
(`id + relpath + status + sha256 + first_seen`); the **event population it acknowledges**, by
identity and generation (`open_events=` in the header, `id + kind + detected_at` per event); and the
**`new` set it promotes**, which is the one deliberate,
disclosed exception below. There is no fourth thing the verb touches.

*Accepted limitation.* The review form's fingerprint does not cover the `new` set — so new files that
appear between render and submit are still adopted by a review-page accept. That is today's unscoped
verb, unchanged. The fingerprint's job here is the **destructive** half: the `missing` rows accept
deletes and the alerts it clears. Covering the `new` set as well would refuse an accept on any
actively-growing collection (Photos scans every 15 minutes) for a promotion that deletes nothing —
the wrong trade. Scoping the verb properly is #16. The spec delta is narrowed to match: what the
review form's fingerprint SHALL cover is the collection's whole **protected** population — its
untruncated `missing` + `modified` set plus its open-event count — and the `new` set's exclusion is
written there as an explicit accepted limitation rather than left as a contradiction of a broader
"whole population" SHALL. (The detail page's baseline form is unaffected: its population *is* the
`new` set, and it covers all of it plus the zero-issue and zero-open-event assertions.)

The `open_events` term does **not** quietly re-close that exception: `streamline-event-acknowledgement`
has the scanner write `added` (and `restored` / `moved`) events **already acknowledged**, so a file
first seen between render and submit leaves the open-event set untouched. The set moves only for
the kinds that actually alarm — a new `missing`, or a WORM `modified` — which is exactly the
population the guard is meant to bind.
