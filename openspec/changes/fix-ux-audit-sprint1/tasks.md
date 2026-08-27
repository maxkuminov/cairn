# Tasks — UX audit sprint 1

Four disjoint slices (A–D), grouped by file scope so they can run as parallel implementation
subagents. **Read `design.md` §D12 (file ownership) before touching `routes.py`, `panel.css`, or any
template that another slice also names** — three files are shared and ownership is by function or by
appended region, not by file.

Standing guardrails for every slice:

- **Zero schema changes, zero Alembic revisions.** If a task seems to need a column, stop and
  escalate — it belongs to a different change.
- **Do not bundle findings from other issues.** The audit's other ~15 issues are sequenced into
  sprints 2 and 3 on purpose.
- Honour the "Do NOT implement these" and "correct as built" lists in `proposal.md`. In particular:
  do not touch `verify_run`'s refusal to fall back to the stored digest; do not use a blanket
  `WHERE file_id = …` ack; do not link anything to `/review`; do not suggest
  `ots --no-bitcoin verify`.

## 1. Shared prep (once, before fan-out)
- [ ] 1.1 Confirm the working tree is on the intended base: `src/services/ots.py` contains
  `_verify_via_explorer` and `src/control_panel/templates/collection_review.html` contains the
  `resolve__opt--accept` card. If either is missing, the branch is stale — stop.
- [ ] 1.2 Add `tests/conftest.py` with the shared `cairn_env` fixture and a `seed_collection`
  helper, lifted verbatim from `tests/test_panel.py` (which keeps its own copies — do not edit that
  file). Every new test module in sections 2–5 uses these.
- [ ] 1.3 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` both green before fan-out.

## 2. Slice A — the verify path (#13, #19, #23-badge, #32-verify, #34-verify_result)
Files owned: `src/services/ots.py`, `src/cli.py` (`_cmd_verify` only), `routes.py::verify_run`,
`templates/_macros.html`, `templates/partials/verify_results.html`,
`templates/partials/verify_result.html`, an **appended** `/* --- ux-audit sprint 1: verdict --- */`
`panel.css` section, `tests/test_ots.py`, new `tests/test_ux_verify.py`.

- [ ] 2.1 `src/services/ots.py`: add four optional fields to `VerifyResult` (design D2) —
  `digest_mismatch: bool = False`, `proof_mismatch: bool = False`,
  `transport_error: str | None = None`, `inconclusive: bool = False`. Defaults keep every existing
  construction site valid.
- [ ] 2.2 `_verify_via_explorer`: set `digest_mismatch=True` on the `want != detached.file_digest`
  return only. That site — and only that site — establishes that the file's bytes are not the ones
  the proof commits to.
- [ ] 2.3 `_verify_via_explorer`: set `proof_mismatch=True` (**not** `digest_mismatch`) on the
  `if mismatch:` merkle-root return. The live digest matched there; what failed is the proof or the
  explorer's block data (design D1). Leave the `state`/`message` values as they are.
- [ ] 2.4 `_verify_via_explorer`: the `if best is None:` return — every `_fetch_block_merkleroot`
  raised — sets `transport_error` to the joined fetch errors. It currently returns
  `state="complete"`, so today an unreachable explorer reads as a plain red "Could not verify".
  Also set `transport_error` on the *partial* path where some fetches raised but an attestation
  matched: that result stays `verified=True` (one attestation confirmed against a real block is
  proof) and nothing downgrades it, but the failure is recorded rather than swallowed.
- [ ] 2.5 `_verify_via_cli`: wrap the `_run_ots` call so an `OtsError` (missing binary, timeout)
  returns `VerifyResult(verified=False, state="none", transport_error=str(exc))` instead of
  propagating; and set `inconclusive=True` on the existing non-success-exit return. Do **not** parse
  stderr for a mismatch — `ots verify -d` reports a mismatch, an unanchored proof and a dead node
  identically, and a guessed mismatch is a false alarm on the core signal (design D1). Comment the
  asymmetry with the explorer path so it is not read as an oversight.
- [ ] 2.6 `routes.py::verify_run`: replace the verdict chain with design D2's order —
  `live_unavailable` → `digest_mismatch` (`danger`, "File no longer matches its proof") →
  `proof_mismatch` (`danger`, "This proof does not check out") → `result.verified` (`ok`) →
  `transport_error` (`unavailable`, "Couldn't check right now") → `inconclusive` (`unavailable`,
  "Couldn't confirm — pending or changed") → `state in ("incomplete", "pending")` (`warn`) →
  else `danger`. Mismatch **before** transport: a mismatch established before the network failed is
  knowledge. Pass the reason flags into the template context.
- [ ] 2.7 `routes.py::verify_run`: the `except ots_svc.OtsError` fallback stops passing
  `state=fe.ots_state` — construct
  `VerifyResult(verified=False, state="none", transport_error=str(exc))` so it lands on the same
  neutral branch as a returned transport failure. It is now a net, not the primary path: 2.4/2.5
  moved the common cases onto the result object, because `verify` mostly *returns* an unreachable
  backend rather than raising (design D2's swallow-point table).
- [ ] 2.8 `partials/verify_result.html` + `panel.css`: add the fourth verdict style
  `verdict--unavailable` (four rules mirroring `--warn` but in the muted `--text-3` palette, not
  red — design D2's supervisor override) and its icon branch. **No transport or inconclusive
  outcome may render `danger`.**
- [ ] 2.9 `partials/verify_result.html`: add the sub-copy branches, one per reason, none of them
  reusing another's wording — digest mismatch: the bytes changed since stamping and the proof still
  attests the *earlier* bytes; **proof mismatch: the proof's chain attestation does not check out,
  the proof may be corrupt, and this is not evidence the file changed**; transport: Cairn could not
  reach the explorer/node and this says nothing about the file, retry; inconclusive: not yet
  confirmed *or* no longer matching, and the Bitcoin-node backend cannot tell these apart. Do not
  weaken the existing `verdict == "ok"` copy.
- [ ] 2.10 `src/cli.py::_cmd_verify`: branch in the same order as 2.6 — `digest_mismatch`,
  `proof_mismatch`, `transport_error`, `inconclusive` all **before** the
  `result.state == "incomplete"` pending line, each printing its own reason and returning non-zero.
  Today the CLI prints "pending (proof not yet anchored to Bitcoin)" for a changed file whose proof
  is incomplete — the same false negative #13 fixes in the panel, on the other consumer of the same
  contract.
- [ ] 2.11 `partials/verify_results.html:15`: `{{ m.ots_badge(f.state, "sm") }}` — `f.state` is
  already supplied by `_anchored_view`; confirm it renders for both `incomplete` and `complete` rows.
- [ ] 2.12 `_macros.html:141`: in `ots_badge`, `incomplete` → **"Pending confirmation"** and
  `pending` → **"Queued to stamp"** (design D13). `pending` is queued-but-not-submitted and
  `incomplete` is submitted-awaiting-Bitcoin; naming both "pending" is what let the tiles sum them.
- [ ] 2.13 `partials/verify_results.html`: branch the empty state on whether `q` is set — a search
  with no hits keeps today's message; **no search and no results** gets "No files have been anchored
  yet" with a pointer to stamping (#32).
- [ ] 2.14 `partials/verify_result.html:57`: derive the closing sentence from `verified_via` instead
  of asserting "Verified by explorer lookup" (#34). Keep the portability sentence and the `/learn`
  link untouched.
- [ ] 2.15 `tests/test_ots.py`: the explorer backend returns `digest_mismatch=True` /
  `proof_mismatch=False` for a non-matching digest, and `proof_mismatch=True` /
  `digest_mismatch=False` for a matching digest whose stubbed explorer block reports a different
  merkle root; a matching, complete proof returns all four flags clear. With every stubbed fetch
  failing, the result carries `transport_error` and is **not** verified; with one fetch failing and
  another matching, the result is verified **and** carries `transport_error`. The node backend
  returns `inconclusive=True` on a stubbed non-success exit and `transport_error` when the binary
  cannot be run.
- [ ] 2.16 `tests/test_ux_verify.py` (new): **the #13 regression tests** — a stamped file whose bytes
  changed renders neither the string "pending" nor a `warn` verdict, but the mismatch card; a
  merkle-root mismatch renders a card that does **not** claim the file changed; a returned
  `transport_error` and a raised `OtsError` both render "Couldn't check right now" as
  `verdict--unavailable` (never `danger`, never "pending"); a node-backend `inconclusive` result
  renders copy naming both possibilities and never "pending confirmation"; the anchored list shows a
  `Pending confirmation` badge for an `incomplete` row and `Queued to stamp` for a `pending` one
  (neither says "Anchored"); `/verify` with nothing anchored renders the empty state. Plus the CLI
  regression: `_cmd_verify` on a `digest_mismatch` result prints the mismatch and not "pending", and
  on a `transport_error`/`inconclusive` result prints neither "pending" nor a verified line.
- [ ] 2.17 Smoke the external-process boundary without a network: `ots.verify` is exercised with a
  stubbed explorer HTTP layer and a stubbed `_run_ots` only (no live calendar/explorer/node calls in
  the suite).

## 3. Slice B — dashboard & collection surfaces (#14, #18, #20, #31, #32-tiles)
Files owned: `routes.py` (`_STATUS_META`, `_collection_status`, `_ots_counts`, `_op_status_c`,
`_collection_view`, `_base_context`, `_event_feed`, `dashboard`, `ack_event`, `collection_detail`,
`collection_accept`, new `_alert_badge_count`), `templates/base.html`, `templates/dashboard.html`,
`templates/collection_detail.html`, `templates/partials/_collection_card.html`,
`partials/op_status.html`, `partials/events_feed.html`, `partials/event_ack.html`,
`partials/review_ack_row.html`, an **appended** `panel.css` section, new
`tests/test_ux_dashboard.py`.

- [ ] 3.1 `routes.py`: add `async def _alert_badge_count(session, collection_ids) -> int` counting
  `FileEntry.status IN ('missing','modified')` for those collections. Call it from `_base_context`,
  `_event_feed`, and `ack_event` — replacing all three inline `status == "missing"` counts (design
  D3). One definition, four render sites.
- [ ] 3.2 `base.html:40-42`: give the badge `title` and `aria-label` —
  `"{n} files missing or changed — open dashboard"` (singular form at 1). Apply the identical markup
  in `partials/events_feed.html`, `partials/event_ack.html` and `partials/review_ack_row.html` so an
  OOB swap cannot drop the label.
- [ ] 3.3 `routes.py::dashboard`: compute `issues_href` — exactly one collection with
  `issues > 0` → `/collection/{id}/review`; two or more → `/collections`. **Never `/review`**
  (404 until #27). Absent/None when the total is zero.
- [ ] 3.4 `dashboard.html:27-31`: render the Open-issues tile as
  `<a class="card tile tile--link" href="{{ tiles.issues_href }}">` with a `· Review ›` CTA when
  `tiles.issues > 0`; keep the plain `<div class="card tile">` at zero. Append `.tile--link` /
  `.tile__cta` to `panel.css` reusing `.mini-stat--link`'s hover treatment.
- [ ] 3.5 `routes.py::_ots_counts`: add **one** grouped query carrying `status != 'missing'` and
  return `complete_active`, `incomplete_active`, `pending_active`, `none_active` beside the existing
  raw totals (design D5). Every ratio component must be counted over the same population — that is
  what `mark_unstamped_pending` queues — so `complete_active + incomplete_active + pending_active +
  none_active == stampable` holds by construction. Expose `stampable = file_count - counts.missing`
  and the ratio string from `_collection_view`; leave the unqualified keys for raw display only.
- [ ] 3.6 Kill every unearned completeness claim, in `collection_detail.html:85-88`,
  `partials/_collection_card.html:45` and `:57`, and `dashboard.html`'s `anchored_sub`:
  - "all confirmed" only when **`complete_active == stampable > 0`** — one comparison, not four, and
    never over a population that includes missing files (a missing file with a complete proof must
    not fill the ratio);
  - otherwise a `complete_active / stampable` ratio, plus an amber `"{none_active:,} not stamped"`
    line next to the existing Stamp-all affordance;
  - the not-yet-confirmed line names the two states separately and **never sums them** (design D13):
    `"{pending_active:,} queued · {incomplete_active:,} pending confirmation"`, dropping whichever
    half is zero. Today the card footer, the detail tile and `anchored_sub` all add them and call
    the total "pending confirmation".
- [ ] 3.7 `routes.py::dashboard`: compute the fleet-wide proof figures **only over views whose
  `ots` mode is `perfile`** — numerator *and* denominator — and label the tile's sub-line with the
  population it covers ("across N notarized collections"). Tripwire collections stamp nothing and
  their stamp route refuses them, so folding their files in ships an un-clearable "not stamped"
  count; dropping them from one half only restores a false "all confirmed" (design D5).
- [ ] 3.8 Zero-file collections must not read healthy on **any** surface (#31): `_collection_status`
  gains an `"empty"` return (its `counts` dict sums to zero exactly when the collection has no
  files, so no new argument), `_STATUS_META["empty"] = ("No files indexed", "var(--text-3)",
  "folder", "muted")` (existing icon, no new SVG, and not the `minusCircle` 3.13 gives `alert`). That one map feeds `_op_status_c`, `_collection_view` and
  `_base_context`, so the dashboard card pill, the detail header pill, `partials/op_status.html`'s
  resting pill and the sidebar dot all change together — the tiles alone are not enough, because
  `op_status.html` is what renders the green "All clear" pill on both pages. Also replace "All files
  verified" / "all confirmed" with **"No files indexed yet"** on the card legend and both detail
  tiles.
- [ ] 3.9 `collection_detail.html:26-38`: when `c.issues > 0`, **remove the Accept form from the
  header** and make "Review issues" the `btn--primary` (design D7). The destructive path is reachable
  only from the page that explains it.
- [ ] 3.10 When `c.issues == 0 and c.counts.new > 0`, keep **"Baseline new files"** with an
  `onsubmit` light confirm naming the count. When both are zero, render neither.
- [ ] 3.11 `routes.py::collection_accept`: add the **submit-time precondition** (design D7).
  Re-count the collection's `modified + missing` inside the POST and check
  `collections_svc.active_run()`; if either is non-zero / in flight, **do not call
  `accept_collection`** — return the refusal *"This collection changed since the page loaded —
  review the issues instead"* with a link to `/collection/{id}/review`. Render-time visibility is
  not a guard: a scheduled scan between GET and POST turns "Baseline 40 new files" into a deletion
  of `missing` rows the operator never saw, under a confirmation that described something else. The
  review page's own accept route is untouched — it lists what it is adopting on the same page.
- [ ] 3.12 Any Accept form that still exists on this page carries the review page's `onsubmit`
  confirm string.
- [ ] 3.13 `routes.py::_STATUS_META`: `alert` → `minusCircle`, `attention` keeps `alert` (design D6).
  No new SVG.
- [ ] 3.14 `dashboard.html`: add the fourth **"New — watched, not yet baselined"** tile
  (`tiles.new`), so `Total ≠ OK + issues` stops being unexplained; rename the collection-detail
  **"Verified OK" → "Matching baseline"**.
- [ ] 3.15 `routes.py::collection_detail`: accept optional `view` (`tree`|`list`, default `tree`)
  and `filter` (`all`|`issues`|`new`|`ok`, default `all`), both whitelist-validated. Thread
  `status_filter=filter` into the **initial list-view** `query_files` call; leave the tree query
  unfiltered; a non-`all` filter with no explicit `view` implies `view="list"` (design D11).
  Template `data-view` and the Tree/List `is-active` classes from `view`.
- [ ] 3.16 `tests/test_ux_dashboard.py` (new): tile is an `<a>` at issues > 0 and a `<div>` at zero;
  single-collection href is the review page and multi-collection is `/collections` (and neither is
  `/review`); badge carries an aria-label and counts missing + modified; a collection with
  `none_active > 0` never renders "all confirmed" and does render the ratio; **a collection whose
  only complete proof is on a `missing` file reports `0 / 1`, not "all confirmed"**; a collection
  with both `pending_active` and `incomplete_active` renders "queued" and "pending confirmation"
  separately and no summed total; **a dashboard with one tripwire and one perfile collection counts
  only the perfile one in the proof tile, with no "not stamped" count from the tripwire files**; a
  zero-file collection renders "No files indexed yet" and the string "All clear" appears **nowhere**
  in its card, its detail page or its `op-status` fragment; collection detail with `issues > 0`
  renders no header Accept form; **posting to `/collection/{id}/accept` after a file has been marked
  missing since render refuses, leaves the missing row in place and its event unacknowledged, and
  names the review view**; `?view=list&filter=issues` returns a filtered list with the Issues radio
  checked and List active.

## 4. Slice C — review, acknowledgement vocabulary, and the restore ack (#17, #22, #32-review)
Files owned: `src/services/scanner.py`, `templates/collection_review.html`,
`templates/partials/review_row.html`, `partials/_event_row.html`,
`partials/_events_controls.html`, `tests/test_scanner.py`, new `tests/test_ux_review.py`.
**No `routes.py` change is needed** — `review_open` and `total_issues` are already in the review
context.

- [ ] 4.1 Rename the per-file action to **"Mark reviewed"** in `partials/review_row.html` and
  `partials/_event_row.html`; the acknowledged state becomes a muted **"Reviewed"** pill.
- [ ] 4.2 Add the hint copy **verbatim** from #17 next to the per-file control: *"Notes that you've
  seen this. The file stays on record as missing or changed, keeps any existence proof, and the
  collection keeps its Alert status until you restore or retire it."*
- [ ] 4.3 `collection_review.html`: the collection-scoped bulk action becomes **"Mark all N
  reviewed"** with the hint *"Clears N alerts in this collection. Nothing about the files changes."*
  (`N` = `review_open`).
- [ ] 4.4 `partials/_events_controls.html`: the dashboard bulk action becomes **"Mark all N reviewed
  (all collections)"** with an `hx-confirm` and the hint *"Marks N alerts across all your collections
  as seen. The files stay missing or changed and the red counts stay — this only clears the
  notification."* It reaches events outside the 20-row feed, which is why it needs both the count and
  the confirm.
- [ ] 4.5 Un-invert the styles (design D8): per-file Mark reviewed → `btn--subtle` unconditionally
  (drop the `btn--danger if missing` conditional, in both row templates); the review panel's **Accept
  all changes** → `btn--danger`.
- [ ] 4.6 `src/services/scanner.py`: in the restore branch (`elif row.status == "missing":`) append
  `row.id` to a `restored_ids` buffer.
- [ ] 4.7 `src/services/scanner.py::_drain`: drain `restored_ids` **inside `_drain`, before its own
  `await session.commit()`**, over the ids accumulated since the previous drain — one
  `update(Event)` per ≤500-id chunk setting `acknowledged_at=now, acknowledged_by=None` where
  `Event.file_id.in_(chunk) AND Event.kind == "missing" AND Event.acknowledged_at.is_(None)` — and
  clear the buffer **only after that commit returns**, exactly as `added_buffer` is cleared (design
  D10). Doing it after the walk instead would be a half-update by construction: `_drain` runs every
  `BATCH` files *and* unconditionally after the walk, so the restored rows' `status='ok'` and their
  `restored` events are already committed by then, and a failing ack would leave a healthy file
  wearing an open `missing` alert that nothing can clear. Inside `_drain` a failing ack takes its
  own batch down with it — the exception reaches the scan body's `except`, the session is rolled
  back and the run finalizes `error`.
- [ ] 4.8 **`kind == "missing"` is load-bearing.** A blanket `WHERE file_id = …` would also clear an
  open WORM `modified` event on the same file (#12's rejected fix 7). Leave a comment saying so.
- [ ] 4.9 `collection_review.html`: render the **Acknowledge half** of the resolve panel whenever
  `review_open > 0`, independently of `total_issues`. In the `total_issues == 0 and review_open > 0`
  state, head it *"{n} alerts from files that have since been restored"* and **do not render
  Accept** — from an otherwise-empty page it would baseline every pending `new` file (design D9).
- [ ] 4.10 `collection_review.html`: the truncation notice deep-links to
  `/collection/{id}?view=list&filter=issues` (Slice B adds the parameter support; land this line
  regardless — it degrades to today's behaviour if merged first).
- [ ] 4.11 Recovery step 3 copy → *"Run **Scan now** on the collection page"*.
- [ ] 4.12 The Copy-paths buttons get a `.catch()` on `navigator.clipboard.writeText` **and** a
  hidden-textarea + `document.execCommand('copy')` fallback for non-secure contexts, with the
  failure path visibly reporting that the copy did not happen.
- [ ] 4.13 `tests/test_scanner.py`: a file that goes missing (open `missing` event) and is then
  restored has that event acknowledged with `acknowledged_by IS NULL`; **an open WORM `modified`
  event on the same file is left untouched**; a `missing` event on a *different* file is untouched;
  more restored files than one chunk holds are all acknowledged (drive the chunk size, don't create
  500 files).
- [ ] 4.14 `tests/test_scanner.py` — **failure injection**: make the acknowledgement UPDATE raise
  and assert the batch's restore did **not** commit either (the file is still `missing`, no
  `restored` event, its `missing` event still open) and the run finalized `error`. This is the
  scenario D10's placement exists for; without it a regression that moves the ack back after the
  walk passes every other test.
- [ ] 4.15 `tests/test_ux_review.py` (new): a collection with `total_issues == 0` and
  `review_open > 0` renders the Mark-all-reviewed control and **no Accept form**; the per-file
  control reads "Mark reviewed" and carries `btn--subtle`; the dashboard bulk control carries
  `hx-confirm` and its count.

## 5. Slice D — docs, settings, mobile (#26, #23-learn, #34, #33)
Files owned: `templates/learn.html`, `templates/settings.html`, `routes.py::settings_page` context,
`src/control_panel/static/css/panel.css` (the `@media (max-width: 768px)` blocks + an appended
mobile section), new `tests/test_ux_docs.py`.

- [ ] 5.1 `learn.html:152-160`: lead the "Verify a proof yourself" list with the
  **opentimestamps.org drag-and-drop**, and state explicitly that an auditor needs **both the file
  and its `.ots`** — the panel's export route serves only the proof, so the file must be supplied
  separately.
- [ ] 5.2 Say plainly that the CLI path (`ots verify`) requires a reachable **Bitcoin Core node**,
  which is why Cairn itself defaults to an explorer lookup. **Do not** substitute
  `ots --no-bitcoin verify` — it exits 1 having verified nothing, which is worse than the current
  visible error (#12's rejected fix 4).
- [ ] 5.3 `learn.html:105-110`: name **all three** proof states as the badge now words them —
  *Queued to stamp* (queued locally, not yet submitted to a calendar), *Pending confirmation*
  (submitted, waiting on Bitcoin) and *Anchored* — and say what distinguishes the first two, so
  `/learn`, the badge, the tiles and the verdict use one vocabulary (#23, design D13). The queued
  state is never called "pending confirmation" anywhere.
- [ ] 5.4 `settings.html:256-277`: strip `.radio-card` from the Verification tab and render the two
  backends as **descriptive text**, naming `CAIRN_VERIFY_BACKEND` and `CAIRN_NODE_RPC_URL` and
  noting that a change requires a restart. Mark which one is active. Nothing on this tab may look
  clickable.
- [ ] 5.5 `routes.py::settings_page`: add `node_rpc_url` to the context so the node line can show the
  configured RPC URL (or say it is unset). **Do not** add DB-backed persistence for the verify
  backend — the panel and the CLI must never disagree about how an integrity claim was verified
  (#34).
- [ ] 5.6 `panel.css` `@media (max-width: 768px)`: hide `.op-bar` (345 − 92 = 253px, which fits a
  320px row), and stop the detail page's status meta-cell clipping — let `.meta-cell--wide` take a
  full row and drop the `nowrap`/ellipsis on its value under the breakpoint. Note there is **no
  `140px` literal** in the file (design: line-reference drift); the clip comes from
  `.meta-cell__value`'s ellipsis inside a `flex: 0 0 auto` cell.
- [ ] 5.7 `tests/test_ux_docs.py` (new): `/learn` mentions opentimestamps.org, says both the file and
  the `.ots` are needed, mentions a Bitcoin node for the CLI path, names the queued and
  pending-confirmation states distinctly, and **does not contain `--no-bitcoin`**; `/settings?tab=verify` contains no `radio-card` class and does name
  `CAIRN_VERIFY_BACKEND`.

## 6. Integration (after all slices merge)
- [ ] 6.1 Merge the slices and resolve `routes.py` / `panel.css` by the §D12 ownership map — if a
  hunk falls outside its slice's declared region, that is a scope violation, not a merge conflict.
- [ ] 6.2 Grep for stragglers of the renamed vocabulary: no user-facing `"Acknowledge"` outside the
  review page's explanatory contrast card (which keeps the noun deliberately); no user-facing
  `"Incomplete"` remaining; and **no surface applying "pending confirmation" to `ots_state='pending'`
  or summing it with `incomplete`** (design D13).
- [ ] 6.3 Confirm **no** file under `alembic/` changed and no model column was added — this change is
  schema-free by contract, so no `make migrate` after deploy.
- [ ] 6.4 `PYTHONPATH=. pytest -q` — full suite green, including the pre-existing
  `tests/test_panel.py` regression tests (the tile, badge, and review templates it already asserts
  on are edited here).
- [ ] 6.5 `ruff check .` clean.
- [ ] 6.6 `openspec validate fix-ux-audit-sprint1 --strict` passes.
- [ ] 6.7 `make audit` (pip-audit) — unchanged dependency set, so this must stay green.
- [ ] 6.8 Adversarial Codex pass. Mandatory trigger: this change touches the verify verdict path.
  Frame it as a defensive control review and say what "wrong" means here — the expensive failure is a
  **false negative** (a changed or deleted file that reads clean, a proof that reads verified when it
  should not, or an alert that silently stops being raised). Specifically ask it to attack: the new
  mismatch/transport/inconclusive branch ordering in `verify_run` *and* `cli._cmd_verify`; every
  backend path that could still return a normal-looking result for an unreachable explorer or node;
  the restore ack's placement inside `_drain`'s transaction and its `kind` scoping; the stale-form
  refusal in `collection_accept`; and every place a completeness claim is now conditional (including
  the perfile-only fleet-wide population).
- [ ] 6.9 Deploy, then a `user-representative` pass over the panel (self-hosted tool, technical
  operator, not a consumer app), including at 390px width for #33.
