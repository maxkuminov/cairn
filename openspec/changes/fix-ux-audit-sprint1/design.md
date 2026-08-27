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

## D1 — `digest_mismatch` is set by the explorer backend only; the node backend is documented as
unable to report it

`_verify_via_explorer` parses the `.ots` locally, so it *knows* the file digest differs from the one
the proof commits to — that is where the flag is set (both sites). `_verify_via_cli` shells out to
`ots verify -d`; a digest mismatch there produces a non-zero exit with no success line, exactly like
a proof that is not yet anchored. Inventing a mismatch from stderr text would be a regex guess on
the CLI's wording, and a *false* mismatch is a false alarm on the product's core signal.

**Decision:** set `digest_mismatch` only where it is actually known. The explorer backend is the
configured default and the one every homelab deploy runs (the node path needs a reachable
`bitcoind`), so the fix covers the live failure. For the node backend, an unverified-and-incomplete
result keeps today's "pending confirmation" reading — recorded as an **accepted limitation** in the
spec delta, not silently. Making the node backend distinguish the two is follow-up work that
belongs with #24-adjacent verify hardening, not sprint 1.

## D2 — a transport failure gets its own verdict, not "pending" and not "the file failed"

Issue #13 says only that the `OtsError` fallback must stop inheriting `fe.ots_state`. That leaves
the question of what it *should* read. Both obvious answers are wrong:

- reusing the `warn` / "Proof pending confirmation" card tells the operator to wait for a network
  error that will never settle (the bug being fixed);
- reusing "Could not verify" implies something about the *file*, when Cairn learned nothing about
  the file at all.

**Decision:** the `OtsError` fallback constructs `VerifyResult(verified=False, state="none", …)` —
dropping `fe.ots_state` entirely — and `verify_run` renders a **fourth, neutral verdict style**
(`verdict="unavailable"`, one small CSS addition) with the title **"Couldn't check right now"** and
sub-copy that says Cairn could not reach the block explorer / node, that this says nothing about the
file, and to retry. Not `warn`/"pending" (the bug being fixed), and **not `danger`**: issue #24 —
the fuller verify-failure-card split queued for a later change — is explicit that an unreachable
explorer must *never* render red, because red for a network blip either panics the operator or, by
crying wolf, teaches them to dismiss the red card that means a genuine mismatch. Adding the neutral
style now is a deliberate small pull-forward from #24 so the transport branch is not styled twice.
(Supervisor override of the drafter's original `danger` choice, for exactly that reason.)

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

## D5 — what "coverage" means, and where the counts come from

#20 and #31 both need a count of files that *could* be stamped but are not.

**Decision:** `_ots_counts` returns one extra key, `none_active` — files with `ots_state='none'`
**and `status != 'missing'`**, i.e. exactly the population `mark_unstamped_pending` queues and the
"Stamp all" button will actually act on. Counting missing files here would ship a warning the
operator can never clear (#20's stated caution). One extra grouped query in the same helper keeps
the definition in a single place.

Derived, in `_collection_view`:

- `stampable = file_count - counts.missing`
- `anchored_ratio = f"{complete:,} / {stampable:,}"`
- **"all confirmed" is emitted only when `complete > 0 and pending == 0 and incomplete == 0 and
  none_active == 0`.** Any other non-empty case shows the ratio; `pending + incomplete > 0` keeps
  today's "N pending confirmation" line, and `none_active > 0` adds an amber
  `"{none_active:,} not stamped"` line next to the existing Stamp-all affordance.
- **`file_count == 0` short-circuits everything to "No files indexed yet"** — on the card legend
  (`_collection_card.html`), the detail proof tile, and the detail "Matching baseline" tile. A
  zero-file collection is a configuration failure to surface, not a clean bill of health.

`none` collections (tripwire-only) are unaffected: they render the Last-scan tile, not the proof
tile.

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
| `issues == 0 and new > 0` | Edit · Scan now · Stamp all · **Baseline new files (`btn--primary`, `onsubmit` confirm)** |
| `issues == 0 and new == 0` | Edit · Scan now · Stamp all |

"Baseline new files" keeps `btn--primary` because in that state it is the only call to action and it
is harmless (`new → ok`, no deletion, no baseline rewrite). Its confirm is deliberately *light*
("Baseline N new files as the expected version?") — a heavy warning on a harmless action trains the
operator to click through the heavy warning on the destructive one.

**The destructive Accept survives only on the review page**, where the contrast card explains it and
its `onsubmit` confirm already exists. The side benefit #14 notes holds: a deletion landing between
page render and click can no longer be swept up by a stale header label.

*Overridable:* demoting "Baseline new files" to `btn--subtle` is a one-class change if Max wants
`btn--primary` reserved for navigation only.

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

## D10 — how the restore branch acknowledges, without a per-file UPDATE inside the walk

The walk buffers rows and commits in batches (`_drain`); issuing an `UPDATE` per restored file would
autoflush mid-walk once per restore and scale badly on a mass restore.

**Decision:** the restore branch appends `row.id` to a `restored_ids: list[int]`. After the
missing-sweep and the auto-baseline block, and **before the scan's `await session.commit()`**, one
statement per ≤500-id chunk (SQLite's bound-parameter ceiling is 999):

```python
update(Event)
  .where(Event.file_id.in_(chunk),
         Event.kind == "missing",
         Event.acknowledged_at.is_(None))
  .values(acknowledged_at=now, acknowledged_by=None)
```

`kind == "missing"` is load-bearing (#12's rejected fix 7): a blanket `file_id` predicate would also
clear an open WORM `modified` event on the same file. `acknowledged_by=None` marks it a *system*
ack, matching the born-acked convention already used for `added`/`restored`/`moved`. It sits inside
the scan's existing try/except, so a failure finalizes the run as `error` like any other scan-body
failure rather than half-committing.

Ordering note: a file cannot be classified `missing` and `restored` in the same pass, so this can
never acknowledge an event the same scan just raised.

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
| `_STATUS_META`, `_ots_counts`, `_collection_view`, `_base_context`, `_event_feed`, `dashboard`, `ack_event`, `collection_detail`, new `_alert_badge_count` | B |
| — (C needs no routes.py change; `review_open` and `total_issues` are already in the review context) | C |
| `settings_page` context only | D |

**`src/control_panel/static/css/panel.css`**

| Region | Owner |
|---|---|
| the two existing `@media (max-width: 768px)` blocks, and a new appended `/* --- ux-audit sprint 1: mobile --- */` section | D |
| a new appended `/* --- ux-audit sprint 1: tiles & links --- */` section at EOF **only** | B |

B appends after D's marker if both exist; neither slice edits the other's region.

**Templates** are single-owner throughout, including the two that look like they straddle:

- `partials/review_ack_row.html` → **B** (only its `sidebar-alert-badge` OOB span changes; the
  `review_row.html` it includes is C's).
- `partials/verify_result.html` → **A** (its #34 half — the `verified_via`-derived sentence — travels
  with the verify work, not with `settings.html`).
- `learn.html` → **D** (both #26's verification section and #23's pending-vs-anchored addition;
  A owns only the `_macros.html` badge label).
- `collection_detail.html` → **B** (both the #14 header and the #32 browser toolbar).

**Tests**: each slice owns one new module. A shared `tests/conftest.py` (the `cairn_env` fixture and
a `seed_collection` helper, lifted from `tests/test_panel.py`) is created **once in section 1,
before fan-out**; `tests/test_panel.py` keeps its own module-level copies untouched, so nothing
existing changes behaviour. Slices A and C additionally append to `tests/test_ots.py` and
`tests/test_scanner.py` respectively — one slice each, no sharing.
