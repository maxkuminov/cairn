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
Files owned: `src/services/ots.py`, `routes.py::verify_run`, `templates/_macros.html`,
`templates/partials/verify_results.html`, `templates/partials/verify_result.html`,
`tests/test_ots.py`, new `tests/test_ux_verify.py`.

- [ ] 2.1 `src/services/ots.py`: add `digest_mismatch: bool = False` to `VerifyResult`.
- [ ] 2.2 Set `digest_mismatch=True` at **both** mismatch sites in `_verify_via_explorer`: the
  `want != detached.file_digest` return, and the `if mismatch:` (merkle-root) return. Leave the
  `state`/`message` values as they are — only the new flag is added.
- [ ] 2.3 Leave `_verify_via_cli` alone: `ots verify -d` cannot distinguish a digest mismatch from
  an unanchored proof, and a *guessed* mismatch is a false alarm on the core signal (design D1).
  Add a short comment saying so, so the asymmetry is not read as an oversight.
- [ ] 2.4 `routes.py::verify_run`: test `result.digest_mismatch` **before** the
  `result.state in ("incomplete", "pending")` branch, and after the `live_unavailable` branch.
  Render `verdict="danger"` with a title that states the failure plainly (e.g.
  "File no longer matches its proof"). Pass `mismatch` into the template context.
- [ ] 2.5 `routes.py::verify_run`: the `except ots_svc.OtsError` fallback stops passing
  `state=fe.ots_state` — construct `VerifyResult(verified=False, state="none", message=str(exc))`
  and surface it as `verdict="danger"`, title **"Verification unavailable"** (design D2). An
  unreachable explorer must never read as a proof that is merely young.
- [ ] 2.6 `partials/verify_result.html`: add the two new sub-copy branches. The mismatch branch says
  the bytes changed since stamping and that the proof still attests the *earlier* bytes; the
  transport branch says Cairn could not reach the explorer/node and that this says nothing about the
  file. Do not weaken the existing `verdict == "ok"` copy.
- [ ] 2.7 `partials/verify_results.html:15`: `{{ m.ots_badge(f.state, "sm") }}` — `f.state` is
  already supplied by `_anchored_view`; confirm it renders for both `incomplete` and `complete` rows.
- [ ] 2.8 `_macros.html:141`: `"Incomplete"` → `"Pending confirmation"` in `ots_badge`. Leave the
  `pending` entry's `"Pending"` as-is (it is the pre-submission state and reads correctly beside it).
- [ ] 2.9 `partials/verify_results.html`: branch the empty state on whether `q` is set — a search
  with no hits keeps today's message; **no search and no results** gets "No files have been anchored
  yet" with a pointer to stamping (#32).
- [ ] 2.10 `partials/verify_result.html:57`: derive the closing sentence from `verified_via` instead
  of asserting "Verified by explorer lookup" (#34). Keep the portability sentence and the `/learn`
  link untouched.
- [ ] 2.11 `tests/test_ots.py`: a proof verified against a non-matching digest returns
  `digest_mismatch=True` (both explorer mismatch sites — file-digest and merkle-root, the latter
  with a stubbed explorer fetch). A matching, complete proof returns `digest_mismatch=False`.
- [ ] 2.12 `tests/test_ux_verify.py` (new): **the #13 regression test** — a stamped file whose bytes
  changed on disk must render neither the string "pending" nor a `warn` verdict; it must render the
  mismatch card. Plus: an `OtsError` from `ots_svc.verify` renders "Verification unavailable" and
  never "pending"; the anchored list shows a `Pending confirmation` badge for an `incomplete` row
  (not "Anchored"); `/verify` with nothing anchored renders the empty state.
- [ ] 2.13 Smoke the external-process boundary without a network: `ots.verify` is exercised with a
  stubbed explorer HTTP layer only (no live calendar/explorer/node calls in the suite).

## 3. Slice B — dashboard & collection surfaces (#14, #18, #20, #31, #32-tiles)
Files owned: `routes.py` (`_STATUS_META`, `_ots_counts`, `_collection_view`, `_base_context`,
`_event_feed`, `dashboard`, `ack_event`, `collection_detail`, new `_alert_badge_count`),
`templates/base.html`, `templates/dashboard.html`, `templates/collection_detail.html`,
`templates/partials/_collection_card.html`, `partials/events_feed.html`, `partials/event_ack.html`,
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
- [ ] 3.5 `routes.py::_ots_counts`: return an extra `none_active` key — `ots_state='none'` **AND
  `status != 'missing'`** (design D5). Counting missing files here would ship a permanent,
  un-clearable warning. Expose `stampable` and the ratio string from `_collection_view`.
- [ ] 3.6 Kill every unearned completeness claim, in `collection_detail.html:85-88`,
  `partials/_collection_card.html:45` and `:57`, and `dashboard.html`'s `anchored_sub`:
  - "all confirmed" only when `complete > 0` **and** `pending == incomplete == none_active == 0`;
  - otherwise a `complete / stampable` ratio, plus an amber `"{none_active:,} not stamped"` line
    next to the existing Stamp-all affordance;
  - `file_count == 0` → **"No files indexed yet"** everywhere (card legend and both detail tiles),
    replacing "All files verified" / "all confirmed" (#31).
- [ ] 3.7 `collection_detail.html:26-38`: when `c.issues > 0`, **remove the Accept form from the
  header** and make "Review issues" the `btn--primary` (design D7). The destructive path is reachable
  only from the page that explains it.
- [ ] 3.8 When `c.issues == 0 and c.counts.new > 0`, keep **"Baseline new files"** with an
  `onsubmit` light confirm naming the count. When both are zero, render neither.
- [ ] 3.9 Any Accept form that still exists on this page carries the review page's `onsubmit`
  confirm string.
- [ ] 3.10 `routes.py::_STATUS_META`: `alert` → `minusCircle`, `attention` keeps `alert` (design D6).
  No new SVG.
- [ ] 3.11 `dashboard.html`: add the fourth **"New — watched, not yet baselined"** tile
  (`tiles.new`), so `Total ≠ OK + issues` stops being unexplained; rename the collection-detail
  **"Verified OK" → "Matching baseline"**.
- [ ] 3.12 `routes.py::collection_detail`: accept optional `view` (`tree`|`list`, default `tree`)
  and `filter` (`all`|`issues`|`new`|`ok`, default `all`), both whitelist-validated. Thread
  `status_filter=filter` into the **initial list-view** `query_files` call; leave the tree query
  unfiltered; a non-`all` filter with no explicit `view` implies `view="list"` (design D11).
  Template `data-view` and the Tree/List `is-active` classes from `view`.
- [ ] 3.13 `tests/test_ux_dashboard.py` (new): tile is an `<a>` at issues > 0 and a `<div>` at zero;
  single-collection href is the review page and multi-collection is `/collections` (and neither is
  `/review`); badge carries an aria-label and counts missing + modified; a collection with
  `none_active > 0` never renders "all confirmed" and does render the ratio; a zero-file collection
  renders "No files indexed yet"; collection detail with `issues > 0` renders no header Accept form;
  `?view=list&filter=issues` returns a filtered list with the Issues radio checked and List active.

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
  `row.id` to a `restored_ids` list. After the missing-sweep / auto-baseline and **before** the
  scan's `await session.commit()`, issue one `update(Event)` per ≤500-id chunk setting
  `acknowledged_at=now, acknowledged_by=None` where
  `Event.file_id.in_(chunk) AND Event.kind == "missing" AND Event.acknowledged_at.is_(None)`
  (design D10).
- [ ] 4.7 **`kind == "missing"` is load-bearing.** A blanket `WHERE file_id = …` would also clear an
  open WORM `modified` event on the same file (#12's rejected fix 7). Leave a comment saying so.
- [ ] 4.8 `collection_review.html`: render the **Acknowledge half** of the resolve panel whenever
  `review_open > 0`, independently of `total_issues`. In the `total_issues == 0 and review_open > 0`
  state, head it *"{n} alerts from files that have since been restored"* and **do not render
  Accept** — from an otherwise-empty page it would baseline every pending `new` file (design D9).
- [ ] 4.9 `collection_review.html`: the truncation notice deep-links to
  `/collection/{id}?view=list&filter=issues` (Slice B adds the parameter support; land this line
  regardless — it degrades to today's behaviour if merged first).
- [ ] 4.10 Recovery step 3 copy → *"Run **Scan now** on the collection page"*.
- [ ] 4.11 The Copy-paths buttons get a `.catch()` on `navigator.clipboard.writeText` **and** a
  hidden-textarea + `document.execCommand('copy')` fallback for non-secure contexts, with the
  failure path visibly reporting that the copy did not happen.
- [ ] 4.12 `tests/test_scanner.py`: a file that goes missing (open `missing` event) and is then
  restored has that event acknowledged with `acknowledged_by IS NULL`; **an open WORM `modified`
  event on the same file is left untouched**; a `missing` event on a *different* file is untouched.
- [ ] 4.13 `tests/test_ux_review.py` (new): a collection with `total_issues == 0` and
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
- [ ] 5.3 `learn.html:105-110`: name **both** proof states as the badge now words them — *Pending
  confirmation* and *Anchored* — so `/learn`, the badge, the tiles and the verdict use one
  vocabulary (#23).
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
  the `.ots` are needed, mentions a Bitcoin node for the CLI path, and **does not contain
  `--no-bitcoin`**; `/settings?tab=verify` contains no `radio-card` class and does name
  `CAIRN_VERIFY_BACKEND`.

## 6. Integration (after all slices merge)
- [ ] 6.1 Merge the slices and resolve `routes.py` / `panel.css` by the §D12 ownership map — if a
  hunk falls outside its slice's declared region, that is a scope violation, not a merge conflict.
- [ ] 6.2 Grep for stragglers of the renamed vocabulary: no user-facing `"Acknowledge"` outside the
  review page's explanatory contrast card (which keeps the noun deliberately), and no user-facing
  `"Incomplete"` remaining.
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
  `digest_mismatch` branch ordering in `verify_run`; the restore-branch ack's scoping; and every
  place a completeness claim is now conditional.
- [ ] 6.9 Deploy, then a `user-representative` pass over the panel (self-hosted tool, technical
  operator, not a consumer app), including at 390px width for #33.
