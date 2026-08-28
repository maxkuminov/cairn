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
- [x] 1.1 Confirm the working tree is on the intended base: `src/services/ots.py` contains
  `_verify_via_explorer` and `src/control_panel/templates/collection_review.html` contains the
  `resolve__opt--accept` card. If either is missing, the branch is stale — stop.
- [x] 1.2 Add `tests/conftest.py` with the shared `cairn_env` fixture and a `seed_collection`
  helper, lifted verbatim from `tests/test_panel.py` (which keeps its own copies — do not edit that
  file). Every new test module in sections 2–5 uses these.
- [x] 1.3 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
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
- [ ] 2.3 `_verify_via_explorer`: **reorder the aggregation so a validated attestation wins**, then
  set `proof_mismatch=True` (**not** `digest_mismatch`) on the merkle-root failure. OTS verification
  is existential — one attestation confirmed against its real block *is* proof — so the
  `best is not None` verified return must come **before** the `if mismatch:` branch, and
  `proof_mismatch` is set only when **no** attestation validated and at least one mismatched. Today
  the loop tests `mismatch` first, so one bad sibling beside a good one renders a red "this proof
  does not check out" over a genuinely anchored proof: a false alarm on the core signal (design D2).
  A mismatched sibling on a verified result is kept as **diagnostic detail in `message`** only. On
  the `proof_mismatch` return the live digest matched, so the copy blames the proof or the explorer's
  block data, never the file (design D1); leave `state` as it is.
- [ ] 2.4 `_verify_via_explorer`: accumulate every `_fetch_block_merkleroot` failure and attach the
  joined reasons as `transport_error` to **every** terminal result returned after the fetch loop —
  the verified one, the `proof_mismatch` one and the `best is None` one alike. One shared
  `transport_error = "; ".join(errors) or None` computed once after the loop, passed to all three
  returns, so a later edit cannot reintroduce a return that drops it. Today the `best is None` return
  carries `state="complete"` and no reason (an unreachable explorer reads as a plain red "Could not
  verify"), and the mismatch return discards `errors` entirely — a proof reported bad on the strength
  of the one attestation that could be fetched, with no hint that the others were not. A recorded
  `transport_error` never downgrades a verified result and never outranks a mismatch (2.6's order);
  on those it is diagnostic detail.
- [ ] 2.5 `_verify_via_cli`: wrap the `_run_ots` call so an `OtsError` (missing binary, timeout)
  returns `VerifyResult(verified=False, state="none", transport_error=str(exc))` instead of
  propagating; and set `inconclusive=True` on the existing non-success-exit return. Do **not** parse
  stderr for a mismatch **or for a transport failure** — `ots verify -d` reports a mismatch, an
  unanchored proof and a dead node identically, and classifying by regexing its wording is a guess
  either way (design D1). The ambiguity is carried in the *copy* (2.9), not resolved by a pattern
  match. Comment the asymmetry with the explorer path so it is not read as an oversight.
- [ ] 2.6 `routes.py::verify_run`: replace the verdict chain with design D2's order —
  `live_unavailable` → `digest_mismatch` (`danger`, "File no longer matches its proof") →
  `result.verified` (`ok`) → `proof_mismatch` (`danger`, "This proof does not check out") →
  `transport_error` (`unavailable`, "Couldn't check right now") → `inconclusive` (`unavailable`,
  "Couldn't confirm — pending, changed, or unreachable") → `state == "incomplete"` (`warn`,
  "Pending confirmation") → `state == "pending"` (`warn`, "Queued to stamp") → else `danger`.
  **`verified` sits above `proof_mismatch`** as belt-and-braces on 2.3's source-level rule: one
  valid attestation is proof, and no caller may turn a bad sibling into a verdict. Mismatch stays
  **before** transport: a mismatch established before the network failed is knowledge.
  `incomplete` and `pending` are two branches, not one (design D13). Pass the reason flags into the
  template context — **`transport_error` included on every branch**, not only the branch it wins, so
  2.9 can disclose it under a verdict that outranks it.
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
  reach the explorer/node and this says nothing about the file, retry; inconclusive: **all three**
  possibilities named — not yet confirmed, *or* the file no longer matches, *or* the Bitcoin node
  could not be reached — and the Bitcoin-node backend cannot tell them apart. Naming only the first
  two invents a file-change scare for what is usually a down node. Add the two lifecycle branches
  too: `incomplete` reads "Pending confirmation" (submitted, waiting on Bitcoin) and `pending` reads
  "Queued to stamp" with **no awaiting-confirmation wording anywhere in its copy** (design D13). Do
  not weaken the existing `verdict == "ok"` copy.
  **Plus the diagnostic transport line (design D2):** whenever `transport_error` is present on a
  result whose verdict is `ok` (verified) or the proof mismatch, render it *under* the verdict —
  verified: *"Note: N attestation lookups failed; the verdict is based on the attestations
  reached."*; proof mismatch: the same note, qualifying the mismatch as established over the
  attestations that could be reached. It is a muted sub-line, never a second verdict style and never
  a downgrade of the headline. Without it, precedence turns into concealment: the operator reads a
  categorical "this proof does not check out" over a proof half of which was never fetched.
- [ ] 2.10 `src/cli.py::_cmd_verify`: branch in the same order as 2.6 — `digest_mismatch`, then
  `result.verified`, then `proof_mismatch`, `transport_error`, `inconclusive`, all **before** the
  lifecycle lines, each printing its own reason and returning non-zero. Today the CLI prints
  "pending (proof not yet anchored to Bitcoin)" for a changed file whose proof is incomplete — the
  same false negative #13 fixes in the panel, on the other consumer of the same contract. Split the
  lifecycle line the same way the panel does: `incomplete` prints the pending-confirmation wording,
  `pending` prints **"queued to stamp — not yet submitted to a calendar"** and never says it is
  awaiting confirmation (design D13). The inconclusive line names all three possibilities (2.9).
  **Same disclosure rule as the panel:** when `transport_error` rides along with a verified or
  `proof_mismatch` verdict, print it as an extra line after the verdict line ("N attestation lookups
  failed; the verdict is based on the attestations reached", qualifying the mismatch on the mismatch
  branch). That line never changes the exit status the verdict already sets.
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
  `digest_mismatch=False` for a matching digest whose **only** attestation's stubbed block reports a
  different merkle root; a matching, complete proof returns all four flags clear. **Mixed
  attestations:** a proof with two Bitcoin attestations, one matching its stubbed block and one not,
  is `verified=True` with `proof_mismatch=False` — one valid attestation is proof, and the bad
  sibling appears only in `message`. With every stubbed fetch failing, the result carries
  `transport_error` and is **not** verified; with one fetch failing and another matching, the result
  is verified **and** carries `transport_error`; **with one fetch failing and the only fetched
  attestation mismatching, the result carries `proof_mismatch=True` *and* `transport_error`** — the
  swallowed fetch error survives the mismatch return. The node backend returns `inconclusive=True` on
  a stubbed non-success exit and `transport_error` when the binary cannot be run.
- [ ] 2.16 `tests/test_ux_verify.py` (new): **the #13 regression tests** — a stamped file whose bytes
  changed renders neither the string "pending" nor a `warn` verdict, but the mismatch card; a
  merkle-root mismatch renders a card that does **not** claim the file changed; a returned
  `transport_error` and a raised `OtsError` both render "Couldn't check right now" as
  `verdict--unavailable` (never `danger`, never "pending"); **a verified result that also carries
  `proof_mismatch` renders the ok verdict, not the mismatch card**; **a verified result carrying
  `transport_error` renders the ok verdict *and* the failed-lookup note, and a `proof_mismatch`
  result carrying `transport_error` renders the mismatch card *and* that note qualifying it** —
  neither may render the transport verdict style, and neither may omit the note; a node-backend `inconclusive`
  result renders copy naming **all three** possibilities (unanchored, changed, node unreachable) and
  never "pending confirmation"; **a `state='pending'` result with no other signal renders "Queued to
  stamp" and no awaiting-confirmation wording, while `state='incomplete'` renders "Pending
  confirmation"**; the anchored list shows a
  `Pending confirmation` badge for an `incomplete` row and `Queued to stamp` for a `pending` one
  (neither says "Anchored"); `/verify` with nothing anchored renders the empty state. Plus the CLI
  regression: `_cmd_verify` on a `digest_mismatch` result prints the mismatch and not "pending", on
  a `transport_error`/`inconclusive` result prints neither "pending" nor a verified line, and on a
  plain `state='pending'` result prints the queued wording without any awaiting-confirmation
  language; and a verified-plus-`transport_error` result **and** a
  `proof_mismatch`-plus-`transport_error` result each print the winning verdict *and* the
  failed-lookup line, with the exit status the verdict alone would give.
- [ ] 2.17 Smoke the external-process boundary without a network: `ots.verify` is exercised with a
  stubbed explorer HTTP layer and a stubbed `_run_ots` only (no live calendar/explorer/node calls in
  the suite).

## 3. Slice B — dashboard & collection surfaces (#14, #18, #20, #31, #32-tiles)
*Owns the route half of the D14 accept guard for **both** accept-family routes; Slice C renders
the two lines it needs in `collection_review.html` (design D12).*
Files owned: `routes.py` (`_STATUS_META`, `_collection_status`, `_ots_counts`, `_op_status_c`,
`_collection_view`, `_base_context`, `_event_feed`, `dashboard`, `ack_event`, `collection_detail`,
`collection_accept`, `collection_review`, `collection_review_accept`, new `_alert_badge_count`, new
`_population_fingerprint`), `templates/base.html`, `templates/dashboard.html`,
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
  "folder", "muted")` (existing icon, no new SVG, and not the `minusCircle` 3.14 gives `alert`). That one map feeds `_op_status_c`, `_collection_view` and
  `_base_context`, so the dashboard card pill, the detail header pill, `partials/op_status.html`'s
  resting pill and the sidebar dot all change together — the tiles alone are not enough, because
  `op_status.html` is what renders the green "All clear" pill on both pages. Also replace "All files
  verified" / "all confirmed" with **"No files indexed yet"** on the card legend and both detail
  tiles.
- [ ] 3.9 `collection_detail.html:26-38`: when `c.issues > 0`, **remove the Accept form from the
  header** and make "Review issues" the `btn--primary` (design D7). The destructive path is reachable
  only from the page that explains it.
- [ ] 3.10 When `c.issues == 0` **and the collection's open-event count is 0** and `c.counts.new >
  0`, keep **"Baseline new files"** with an `onsubmit` light confirm naming the count. With
  `issues == 0` but open events outstanding, render **"Review issues"** as the primary action and no
  baseline form (design D7): `accept_collection` also acknowledges every open event, so a restored
  file's unread alert would otherwise be cleared under a confirm that promises only a new-file
  promotion. When issues, open events and `new` are all zero, render neither. The count is the same
  `acknowledged_at IS NULL` count the D14 fingerprint hashes — compute it once in
  `_collection_view` and reuse it, so the render gate and the guard can never disagree.
- [ ] 3.11 `routes.py`: add the shared **population fingerprint** guard (design D14) and apply it to
  `collection_accept`. New `async def _population_fingerprint(session, collection, scope) -> str`
  (it takes the `Collection` row `_get_owned_collection` already loaded, because the collection's
  `created_at` is part of the preimage) returning the hex SHA-256 of design D14's **canonical
  encoding**, over the population that scope names: header
  `f"{scope}\x1f{collection.id}\x1f{collection.created_at.isoformat()}\x1fopen_events={k}"` — where
  `k` is `select(func.count()).select_from(Event).where(Event.collection_id == collection.id,
  Event.acknowledged_at.is_(None))`, read in the **same** transaction as the file rows — plus
  `f"\x1fissues={n}"` for `baseline-new` only (that scope's two zero assertions, `issues == 0` and
  `open_events == 0`, are hashed *inside* the fingerprint), then one record per file
  `f"{id}\x1f{len(relpath.encode())}\x1f{relpath}\x1f{status}\x1f{sha256 or ''}\x1f{first_seen.isoformat() if first_seen else ''}"`,
  records sorted by `relpath` and joined with `\x1e`, the whole preimage UTF-8 encoded.
  `baseline-new` hashes the collection's `new` files; `review-accept` (3.12) its `missing` +
  `modified` files. **`relpath` and `sha256` are load-bearing, not decoration:** `files.id` /
  `collections.id` are `INTEGER PRIMARY KEY` **without** `AUTOINCREMENT`, so SQLite may hand a
  deleted row's id to a later insert — an id-and-status-only preimage is byte-identical for two
  populations sharing no file, and the old form would then delete a record the operator never saw.
  `relpath` pins the logical file, `sha256` (empty string when NULL) pins the content generation,
  **`first_seen` pins the row generation** — it is `NOT NULL`, written at row insertion and never
  rewritten in place, so a row deleted by an accept and re-created at the *same path with the same
  digest* on the *same reused id* still encodes differently and cannot validate the stale form —
  and the collection's `created_at` does the same job for a recreated collection reusing its id.
  **`open_events` is the third population:** `accept_collection` acknowledges every open event, and
  that set is not derivable from the file rows (a modified → missing → restored file returns the
  protected set to its rendered value while its alert stays open), so a change in the count between
  render and submit is a refusal. `added`/`restored`/`moved` events are born acknowledged, so the
  term does not defeat the documented `new`-set exception. The
  byte-length prefix on `relpath` keeps the encoding unambiguous for paths containing `\x1f`/`\x1e`.
  Sort by `relpath` (unique per collection), never by `id`. One `select(FileEntry.id,
  FileEntry.relpath, FileEntry.status, FileEntry.sha256, FileEntry.first_seen)` plus the one
  `count()` over `Event` — every column is already on the entity `query_files` /
  `collection_review` select, so this adds no join.
  `collection_detail.html` renders it as a hidden `population_fp` input in
  the baseline form. The POST then, **in one write transaction**: (1) takes the write lock first with
  a no-op write on the collection's own row (`UPDATE collections SET name = name WHERE id = :id`) —
  SQLite holds that lock until commit, so nothing can interleave; (2) recomputes the fingerprint —
  file rows *and* the open-event count read inside this transaction — and re-asserts `issues == 0`
  and `open_events == 0`; (3) compares — an absent or empty field is a **mismatch** (fail closed);
  (4) only then calls `accept_collection`, whose own `commit()` closes the same transaction. Keep the
  `collections_svc.active_run()` check as belt-and-braces for the long window. On any refusal:
  **no mutation**, 303 to `/collection/{id}/review?stale=1`. A recount that is not inside the
  accept's own transaction is not a guard — the same scan can claim, run and commit between the
  recount statement and the accept's first `DELETE`.
- [ ] 3.12 `routes.py`: the **review page's accept is guarded by the same mechanism** (design D14).
  `collection_review` publishes `population_fp` (scope `review-accept`, hashed over the collection's
  **entire** `missing + modified` set in SQL plus the header's `open_events` count — *not* the
  `REVIEW_ROW_LIMIT`-capped rendered rows, and
  deliberately **not** the `new` set the same accept promotes: that exclusion is the documented
  accepted limitation (design D14, stated normatively in the delta), so do not "complete" the
  fingerprint by folding `new` in — it would refuse every accept on a collection that scans every
  few minutes) and
  a `stale` context flag from a whitelisted `?stale=1` query parameter (only `1` recognized,
  anything else ignored, exactly as `view`/`filter` are handled). `collection_review_accept` runs the
  identical write-lock → recompute → compare → act sequence and refuses to
  `/collection/{id}/review?stale=1`. Slice C renders both keys in `collection_review.html` (D12);
  the route must fail closed if the field is absent. "It lists exactly what it will adopt" is a
  statement about the *render*: a scan that records another missing file after it makes the claim
  false, and the operator deletes a record they never saw from the page whose whole purpose is that
  they saw it.
- [ ] 3.13 Any Accept form that still exists on this page carries the review page's `onsubmit`
  confirm string.
- [ ] 3.14 `routes.py::_STATUS_META`: `alert` → `minusCircle`, `attention` keeps `alert` (design D6).
  No new SVG.
- [ ] 3.15 `dashboard.html`: add the fourth **"New — watched, not yet baselined"** tile
  (`tiles.new`), so `Total ≠ OK + issues` stops being unexplained; rename the collection-detail
  **"Verified OK" → "Matching baseline"**.
- [ ] 3.16 `routes.py::collection_detail`: accept optional `view` (`tree`|`list`, default `tree`)
  and `filter` (`all`|`issues`|`new`|`ok`, default `all`), both whitelist-validated. Thread
  `status_filter=filter` into the **initial list-view** `query_files` call; leave the tree query
  unfiltered; a non-`all` filter with no explicit `view` implies `view="list"` (design D11).
  Template `data-view` and the Tree/List `is-active` classes from `view`.
- [ ] 3.17 `tests/test_ux_dashboard.py` (new): tile is an `<a>` at issues > 0 and a `<div>` at zero;
  single-collection href is the review page and multi-collection is `/collections` (and neither is
  `/review`); badge carries an aria-label and counts missing + modified; a collection with
  `none_active > 0` never renders "all confirmed" and does render the ratio; **a collection whose
  only complete proof is on a `missing` file reports `0 / 1`, not "all confirmed"**; a collection
  with both `pending_active` and `incomplete_active` renders "queued" and "pending confirmation"
  separately and no summed total; **a dashboard with one tripwire and one perfile collection counts
  only the perfile one in the proof tile, with no "not stamped" count from the tripwire files**; a
  zero-file collection renders "No files indexed yet" and the string "All clear" appears **nowhere**
  in its card, its detail page or its `op-status` fragment; collection detail with `issues > 0`
  renders no header Accept form; `?view=list&filter=issues` returns a filtered list with the Issues
  radio checked and List active.
- [ ] 3.18 `tests/test_ux_dashboard.py` — **the D14 guard, both routes**, each driven as a real
  interleaving (render the page, mutate the DB the way a scan would, then POST the
  already-rendered `population_fp`): a file marked `missing` between render and POST makes
  `/collection/{id}/accept` refuse — the missing row is still there, its event still unacknowledged,
  no `new` file promoted — and the response is a 303 to the review view carrying `stale=1`; the same
  interleaving on `/collection/{id}/review/accept` refuses identically; a POST with a **missing or
  empty** `population_fp` refuses (fails closed); an unchanged population accepts normally, so the
  guard is not simply refusing everything; a fingerprint minted for one collection is refused on
  another, and a `baseline-new` fingerprint is refused by the review route; a POST while
  `active_run()` reports an operation in flight refuses. **Id reuse:** render the review form,
  delete the missing row, insert a *different* `relpath` that reuses the freed id (set it explicitly)
  and mark it `missing` — the already-rendered `population_fp` is refused and the replacement row
  survives; do the same for a collection deleted and recreated on the same `collection_id` with a
  new `created_at`. **Row generation:** the harder variant — delete the missing row and reinsert
  **the same `relpath` with the same `sha256` on the same reused `id`**, marked `missing`, differing
  only in `first_seen`; the stale form is refused and the replacement row survives (this is the case
  `id + relpath + status + sha256` alone cannot see). **ABA on the event population:** with the
  protected file set returned to exactly its rendered value (take a *second* file `modified`, then
  `missing`, then restore it to `ok`, leaving its `modified` event open), the already-rendered
  `population_fp` is refused on both routes and that event is **still unacknowledged** afterwards;
  and the detail page renders **no** baseline form for a collection with zero issues, a non-zero
  `new` count and one open event (design D7). **The `new`-set exception (design D14 / the delta's accepted limitation):** add a
  not-yet-baselined file between render and POST on `/collection/{id}/review/accept` and assert the
  accept **succeeds** and promotes it — the guard must not refuse on a growing collection. Assert the
  *absence* of mutation on every refusal, not just the status code — a guard that redirects and still
  deletes is the bug.

- [ ] 3.19 `routes.py`: the guard helper handles **lock contention as a refusal** (design D14).
  Wrap the step-1 no-op `UPDATE collections SET name = name WHERE id = :id` — the statement that
  acquires/upgrades the writer transaction — in `except sqlalchemy.exc.OperationalError`, and
  convert it **only** when SQLite reports `SQLITE_BUSY`, `SQLITE_BUSY_SNAPSHOT` or `SQLITE_LOCKED`
  (test the driver exception's `sqlite_errorname` where available, falling back to the message
  text); then `await session.rollback()`, **do not** call `accept_collection`, and return the same
  fail-closed `303` to `/collection/{id}/review?stale=1` a fingerprint mismatch returns. Re-raise
  every other `OperationalError`/`DatabaseError` untouched — a corrupt or misconfigured datastore
  must not be reported as "the collection changed since the page loaded". Uncaught, these become an
  HTTP 500 on a destructive POST: the refusal promise broken exactly where the guard exists, and an
  invitation to retry blind. Both accept-family routes go through the one helper.
- [ ] 3.20 `tests/test_ux_dashboard.py` — **contention**: with a second session holding the SQLite
  writer lock on the same database (begin a write transaction there and leave it open) fire the
  accept POST on both routes and assert each returns the `303` to `…/review?stale=1`, **not** a 500,
  and that nothing was mutated once the holding transaction is rolled back; and that an
  `OperationalError` which is *not* one of the three lock codes propagates rather than being
  reported as staleness (inject it on the no-op `UPDATE`).

## 4. Slice C — review, acknowledgement vocabulary, and the restore ack (#17, #22, #32-review)
Files owned: `src/services/scanner.py`, `templates/collection_review.html`,
`templates/partials/review_row.html`, `partials/_event_row.html`,
`partials/_events_controls.html`, `tests/test_scanner.py`, new `tests/test_ux_review.py`.
**No `routes.py` change is needed** — `review_open` and `total_issues` are already in the review
context.

- [x] 4.1 Rename the per-file action to **"Mark reviewed"** in `partials/review_row.html` and
  `partials/_event_row.html`; the acknowledged state becomes a muted **"Reviewed"** pill.
- [x] 4.2 Add the hint copy **verbatim** from #17 next to the per-file control: *"Notes that you've
  seen this. The file stays on record as missing or changed, keeps any existence proof, and the
  collection keeps its Alert status until you restore or retire it."*
- [x] 4.3 `collection_review.html`: the collection-scoped bulk action becomes **"Mark all N
  reviewed"** with the hint *"Clears N alerts in this collection. Nothing about the files changes."*
  (`N` = `review_open`).
- [x] 4.4 `partials/_events_controls.html`: the dashboard bulk action becomes **"Mark all N reviewed
  (all collections)"** with an `hx-confirm` and the hint *"Marks N alerts across all your collections
  as seen. The files stay missing or changed and the red counts stay — this only clears the
  notification."* It reaches events outside the 20-row feed, which is why it needs both the count and
  the confirm.
- [x] 4.5 Un-invert the styles (design D8): per-file Mark reviewed → `btn--subtle` unconditionally
  (drop the `btn--danger if missing` conditional, in both row templates); the review panel's **Accept
  all changes** → `btn--danger`.
- [x] 4.6 `collection_review.html`: render the two keys Slice B publishes for the D14 accept guard
  (design D12 — this file stays single-owner, so the lines land here rather than in a B-owned
  partial). (a) A hidden `<input type="hidden" name="population_fp" value="{{ population_fp }}">`
  inside the **Accept all changes** form — the same hunk 4.5 already edits to give that button
  `btn--danger`. (b) When `stale` is set, a **dismissable banner** above the list reading *"This
  collection changed since the page loaded — the list below is current."* — this is where an accept
  refused by the guard lands (`?stale=1`), and without it the operator sees an ordinary review page
  with no account of why their click did nothing. Neither line invents state: both come from the
  route context.
- [x] 4.7 `src/services/scanner.py`: in the restore branch (`elif row.status == "missing":`) append
  `row.id` to a `restored_ids` buffer.
- [x] 4.8 `src/services/scanner.py::_drain`: drain `restored_ids` **inside `_drain`, before its own
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
- [x] 4.9 **`kind == "missing"` is load-bearing.** A blanket `WHERE file_id = …` would also clear an
  open WORM `modified` event on the same file (#12's rejected fix 7). Leave a comment saying so.
- [x] 4.10 `collection_review.html`: render the **Acknowledge half** of the resolve panel whenever
  `review_open > 0`, independently of `total_issues`. In the `total_issues == 0 and review_open > 0`
  state, head it *"{n} alerts from files that have since been restored"* and **do not render
  Accept** — from an otherwise-empty page it would baseline every pending `new` file (design D9).
- [x] 4.11 `collection_review.html`: the truncation notice deep-links to
  `/collection/{id}?view=list&filter=issues` (Slice B adds the parameter support; land this line
  regardless — it degrades to today's behaviour if merged first).
- [x] 4.12 Recovery step 3 copy → *"Run **Scan now** on the collection page"*.
- [x] 4.13 The Copy-paths buttons get a `.catch()` on `navigator.clipboard.writeText` **and** a
  hidden-textarea + `document.execCommand('copy')` fallback for non-secure contexts, with the
  failure path visibly reporting that the copy did not happen.
- [x] 4.14 `tests/test_scanner.py`: a file that goes missing (open `missing` event) and is then
  restored has that event acknowledged with `acknowledged_by IS NULL`; **an open WORM `modified`
  event on the same file is left untouched**; a `missing` event on a *different* file is untouched;
  more restored files than one chunk holds are all acknowledged (drive the chunk size, don't create
  500 files).
- [x] 4.15 `tests/test_scanner.py` — **failure injection**: make the acknowledgement UPDATE raise
  and assert the batch's restore did **not** commit either (the file is still `missing`, no
  `restored` event, its `missing` event still open) and the run finalized `error`. This is the
  scenario D10's placement exists for; without it a regression that moves the ack back after the
  walk passes every other test.
- [x] 4.16 `tests/test_ux_review.py` (new): a collection with `total_issues == 0` and
  `review_open > 0` renders the Mark-all-reviewed control and **no Accept form**; the per-file
  control reads "Mark reviewed" and carries `btn--subtle`; the dashboard bulk control carries
  `hx-confirm` and its count; the review page's Accept form carries a non-empty `population_fp`
  hidden field; `GET …/review?stale=1` renders the "changed since the page loaded" banner and a
  plain `GET …/review` does not (nor does an unrecognized `stale` value).

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
  Then check the one **cross-slice contract**: every accept-family form renders a non-empty
  `population_fp` (grep `collection_detail.html` and `collection_review.html`) and both routes
  recompute it. B's route half and C's template half must land in the same merge — the routes fail
  closed, so a half-merge refuses every accept rather than accepting one unguarded.
- [ ] 6.2 Grep for stragglers of the renamed vocabulary: no user-facing `"Acknowledge"` outside the
  review page's explanatory contrast card (which keeps the noun deliberately); no user-facing
  `"Incomplete"` remaining; and **no surface applying "pending confirmation" to `ots_state='pending'`
  or summing it with `incomplete`** (design D13) — including the verify verdict, whose `pending`
  branch must read "Queued to stamp" in both the panel and the CLI.
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
  the restore ack's placement inside `_drain`'s transaction and its `kind` scoping; **the D14
  fingerprint guard on both accept-family routes — whether the recount and the accept really share
  one write transaction, whether any interleaving still deletes an unseen `missing` row or clears an
  unseen alert, whether the fingerprint can be replayed across forms, collections or **record
  generations** (same path, same digest, reused id), and whether every population
  `accept_collection` mutates is bound by it**; and every place a completeness claim
  is now conditional (including the perfile-only fleet-wide population). Tell it explicitly that
  OpenTimestamps verification is existential — one valid attestation is proof — so a *false mismatch*
  is as expensive here as a false clean bill.
- [ ] 6.9 Deploy, then a `user-representative` pass over the panel (self-hosted tool, technical
  operator, not a consumer app), including at 390px width for #33.
