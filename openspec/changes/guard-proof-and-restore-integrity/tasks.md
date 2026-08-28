# Tasks — guard proof and restore integrity

Two disjoint implementation slices (**A** = #15 + provenance + verify blame, **B** = #21), preceded
by one **shared prep** step that owns the schema, and followed by integration + the gates.

**Read `design.md` §D9 (file ownership) before touching `routes.py`.** It is the one file both
slices name, and ownership is **by function**: Slice A owns `verify_run`, Slice B owns
`_event_view`. Neither slice reformats it, and neither edits the other's function.

Standing guardrails for every slice:

- **Exactly one Alembic revision (`0011`) exists in this change, and it is written in shared prep,
  before fan-out.** No slice creates a migration. If a task seems to need another column, stop and
  escalate.
- **Honour `proposal.md`'s Non-goals and #12's "Do NOT implement these".** In particular: no
  `files.status = 'gone'` (rejected fix 3); no blanket `WHERE file_id = …` acknowledgement
  (rejected fix 7 — the restore ack keeps its `kind='missing'` scoping); no proof is ever deleted,
  including a superseded one, and no preserved proof is ever overwritten or discarded — not even for
  a duplicate digest (design D1, "Archive collision"); no per-file accept, no accept re-scoping, no
  `/review` fleet page.
- **Do not bundle findings from other audit issues.** Sprints 2 and 3 are sequenced on purpose.
- **Every proof-mutating entry point runs under the collection's operation claim** (design D10). Do
  not add a lock inside `_place_proof`, `stamp_pending` or `upgrade_incomplete` — the claim wraps
  them from the caller, and those functions are called from callers that already hold one.
- **`ots_digest` is never filled from an *uncorroborated* read of the proof** (design D2). Only
  placement, corroborated adoption, and the corroborated fill in the daily upgrade pass (design D3)
  write it — and the latter two only when the parsed proof digest equals the row's `sha256`. No
  read-side path (`/verify`, proof download, export, a scan) writes it at all.

## 1. Shared prep (once, on the base branch, before fan-out)

- [ ] 1.1 Confirm the working tree is on the intended base: `src/services/ots.py` contains
  `_place_proof` **and** `_proof_output_writable`; `src/control_panel/routes.py` contains
  `mismatch_blame`; `openspec/changes/archive/2026-08-28-fix-ux-audit-sprint1/` exists. If any is
  missing, the branch is stale — stop.
- [ ] 1.2 Confirm the Alembic head is `0010_auto_baseline_new` (`ls alembic/versions/`). This
  change's revision is `0011`; if the head has moved, renumber before writing it.
- [ ] 1.3 Write `alembic/versions/0011_proof_provenance_and_restored_changed.py`:
  - `files.ots_digest` — plain `op.add_column("files", sa.Column("ots_digest", sa.String(64),
    nullable=True))`. **No in-migration backfill** (design D3 — the backfill is the upgrade pass's,
    task 2.11a); no table rebuild.
  - `events.kind` CHECK widened to include `restored_changed`, via `op.batch_alter_table("events")`
    — mirror `0005_rename_detection.py` exactly (it did the same rebuild on the same table).
  - A `downgrade()` that reverses both — **and refuses rather than losing evidence**: it SHALL first
    `SELECT count(*) FROM events WHERE kind='restored_changed'` and, if non-zero, raise with a
    message naming the kind and the count and telling the operator to export or deliberately
    re-classify those events before downgrading. It must NOT delete them and must NOT rewrite them to
    `modified` (design D4a). With no such rows it narrows the CHECK and drops `ots_digest` normally.
- [ ] 1.4 `src/models/db.py`: add `FileEntry.ots_digest: Mapped[str | None] =
  mapped_column(String(64))` with a docstring/comment stating it records **the digest the proof
  Cairn placed at `ots_path` commits to**, written with `ots_path`/`ots_state` and cleared with
  them; and extend `ck_events_kind` to `('added','modified','missing','restored','moved','restored_changed')`.
- [ ] 1.4a Test the migration against a **populated** database: apply `0011`, insert a
  `restored_changed` event, run `alembic downgrade` and assert it **fails** with the count-naming
  error and that every table is untouched (no event deleted or re-kinded); then delete that event and
  assert the downgrade succeeds, restoring the old CHECK and dropping `files.ots_digest` while
  preserving every other row. Lives with shared prep because shared prep owns the migration.
- [ ] 1.5 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green, and `alembic upgrade head` clean against a scratch DB.
- [ ] 1.6 Commit shared prep on the base branch **before** creating any worktree — agent worktrees
  branch from the committed base, so an uncommitted migration is invisible to both slices.

## 2. Slice A — a stamp never destroys a proof, and each proof records its digest (#15, D1 IOU)

Files owned: `src/services/ots.py`, `src/services/proofs.py`, `routes.py::verify_run`, `src/cli.py`
(`_cmd_verify`, `_cmd_stamp`, `_cmd_upgrade`, and `_cmd_scan`'s refusal line only),
`src/services/scheduler.py` (claim audit only), `templates/partials/verify_result.html`,
`tests/test_ots.py`, new `tests/test_proof_preservation.py`, new `tests/test_proof_serialization.py`.

### Proof reading

- [ ] 2.1 `ots.py`: add an offline helper that parses a stored `.ots` **with the OpenTimestamps
  library** (lazy import inside the function, as `_verify_via_explorer` already does) and returns
  its committed digest **and** whether it carries a `BitcoinBlockHeaderAttestation` — one parse,
  both facts. It must never raise for an unreadable/absent file: return "unknown". No subprocess
  (`ots info` costs a process spawn per occupied path).

### Preservation

- [ ] 2.2 `ots.py`: add the superseded-archive path builder —
  `<proof_store>/.superseded/<collection_id>/<digest[:2]>/<digest>.ots`, and
  `…/<collection_id>/unknown/<uuid>.ots` for an unreadable proof. Names are **fixed-length**: no
  watched filename may influence archive path length (design D1), so the archive can never trip
  `ENAMETOOLONG` and `_proof_output_writable` keeps applying only to the canonical path. On a taken
  name the builder yields `<digest>.1.ots`, `<digest>.2.ots`, … — the first free index (design D1,
  "Archive collision").
- [ ] 2.3 `_place_proof`: when the canonical output path is **unoccupied**, behave exactly as today
  (`mkdir` + `os.replace`, same ENAMETOOLONG-permanent / everything-else-transient classification).
  This is the only path an ordinary new-file stamp takes; do not add cost to it beyond one `stat`.
- [ ] 2.4 `_place_proof`: when the canonical path **is** occupied, apply the placement rule (design
  D1 table): same digest + complete + **anchor confirmed by the caller** → **keep existing, discard
  staged**; same digest + complete + **anchor disproven by the caller** → archive then place; same
  digest + complete + **no verdict** (backend unreachable, or no lookup made) → **defer**; same digest
  + incomplete → archive then place; different digest → archive under its digest then place;
  unreadable → archive under `unknown/` then place. `_place_proof` stays **offline** — it takes the
  caller's verdict as an argument (`confirmed` / `disproven` / absent) rather than reaching the
  network, so a forged syntactic attestation cannot hold the canonical path (design D1a).
- [ ] 2.4a `_place_proof`, the **deferred** branch: change nothing on disk except to preserve the
  **staged** proof into its digest's archive family (task 2.6's exclusive create), leave the existing
  proof byte-identical at the canonical path, return the `deferred` outcome, and **raise nothing** —
  the caller writes no `ots_path`/`ots_state`/`ots_digest`/`ots_stamped_at` and the row stays
  `pending` for a later pass. A syntactic attestation nobody confirmed must never be recorded
  `complete`, and an outage must never demote or discard either proof (design D1a).
- [ ] 2.5 `_place_proof`: **archive first, place second** — never the reverse. A failure to archive
  **refuses the placement** and raises a **transient** `OtsError` (the member stays `pending`; it is
  never dropped to `none`, because archiving is not the final output path — see `ots.py`'s module
  docstring rule, which must not be contradicted).
- [ ] 2.6 `_place_proof`: the archive **never discards and never overwrites**. If the archive target
  exists, preserve under the next free monotonic suffix so **both** proofs survive (design D1 —
  strength comparison is a judgement the archive must not make; the earlier-archived proof is not
  reliably the stronger one). Write it as an **exclusive create that does not require hard-link
  support**: `os.open(candidate, O_CREAT | O_EXCL | O_WRONLY)` — `EEXIST` ⇒ try the next index — then
  copy the source's bytes in, `flush` + `os.fsync`, `close`, and **only then** `os.unlink` the source.
  Do **not** use `os.replace` (silently overwrites) and do **not** use `os.link` (needs hard links,
  which the proof store's writable-filesystem contract does not promise — on a CIFS/FAT/FUSE store it
  would turn every occupied-path placement into a permanent retry loop reported as transient). No
  preflight probe. Index selection is safe because the whole sequence runs under the collection claim
  (task 2.8a) and the archive path is already scoped by `<collection_id>`.
- [ ] 2.7 `_place_proof`: log at `WARNING` naming **both** paths whenever a proof is superseded or a
  staged proof is discarded — the archive has no panel surface, so the log is its discoverability.
- [ ] 2.8 `_place_proof` returns an outcome (`kind: 'placed' | 'kept' | 'deferred'`, `digest`,
  `state`) instead of `None` — `deferred` is an outcome, not a failure, and must not raise;
  `stamp_via_symlink` returns it, and `stamp_batch_via_symlink` returns `list[StampOutcome | None]`
  (with `None` still meaning "this member failed, fall back to a single-file stamp"). Update the two
  call sites in `proofs.py` and the existing `tests/test_ots.py` expectations.

### Single-writer serialization (design D10)

- [ ] 2.8a `src/cli.py::_cmd_stamp`: wrap the whole stamp in the collection's operation claim.
  Replace the direct `mark_unstamped_pending` + `stamp_pending` calls with the existing
  `proofs.run_stamp_backfill` for `--all`, and for the plain (already-`pending`) case open and
  `collections.claim_run` a `kind='stamp'` run the same way before calling `stamp_pending`. A lost
  claim **refuses**: print that an operation is already in progress for that collection, stamp
  nothing, do not wait, and exit non-zero.
- [ ] 2.8b `src/cli.py::_cmd_upgrade`: stop calling `upgrade_incomplete(session)` fleet-wide with no
  run. Iterate collections, and for each open + `claim_run` a `kind='upgrade'` run exactly as
  `scheduler.run_daily_upgrade` does, calling `upgrade_incomplete(session, collection)`. A collection
  whose slot is held is skipped **and named**; if every collection was skipped, exit non-zero.
  Prefer factoring the scheduler's existing per-collection upgrade body into a shared helper over
  duplicating it.
- [ ] 2.8c `src/cli.py::_cmd_scan`: a collection whose scan returned `result='skipped'` (the claim
  was lost) must print that it was skipped because an operation is in progress — not the ordinary
  all-zeroes result line, which reads as a clean scan. **Exit non-zero when *every* requested
  collection was skipped** and zero when at least one was actually scanned, the same rule 2.8a/2.8b
  apply, so a cron `cairn scan` that examined nothing cannot record a clean integrity pass. Do not
  change `scanner.py` (Slice B owns it); `summary.result == "skipped"` already carries the fact.
- [ ] 2.8d `src/services/scheduler.py`: audit only — confirm the scan pass and the daily upgrade both
  claim (they do: `scan_collection`'s `claim_run`, and `run_daily_upgrade`'s own). Expected diff is
  zero or a comment pointing at design D10. Do **not** add a claim inside `stamp_pending` or
  `upgrade_incomplete` — they are called from inside callers that already hold one (`scanner.py:540`).

### Adoption + provenance writes

- [ ] 2.9 `proofs.stamp_pending`: before building the batch, adopt — and drop from `work` — any
  pending file whose canonical proof (a) parses, (b) commits to the row's own `sha256`, **and** (c)
  carries a Bitcoin attestation that **verifies via the configured backend at that moment**
  (`ots.verify` with the configured backend/explorer/node, off the event loop like every other
  blocking OTS call). Record `ots_path`, `ots_state`, `ots_digest`, leave `ots_stamped_at`
  **unchanged**, and count it as stamped without a staging symlink or a calendar round-trip. The
  row's already-recorded `ots_digest` is **not** an alternative to (c) and must not short-circuit the
  lookup: it names the digest a placed proof committed to, not which artifact is on disk, so any
  fabricated same-digest `.ots` satisfies it (design D1a). **Never adopt** an `incomplete` proof, an
  unverifiable one, or one whose anchor could not be checked because the backend was unreachable.
  Log each adoption with the file, digest and confirming block.
- [ ] 2.9a `proofs.stamp_pending`, verdict propagation: where the backend **answered** that the
  existing proof's anchor does not confirm, pass the `disproven` verdict into `_place_proof` (task
  2.4) so its keep-existing branch cannot resurrect the proof adoption just rejected; where it
  answered that the anchor confirms, pass `confirmed`; where the backend was **unreachable**, pass
  **no** verdict, so placement takes its deferred branch (task 2.4a) and the pass records nothing for
  that file rather than promoting an unverified artifact to `complete`.
- [ ] 2.10 `proofs.stamp_pending`: write `ots_digest` on every successful placement, in the same
  transaction as `ots_path`/`ots_state`/`ots_stamped_at`. On the **kept-existing** outcome set
  `ots_state='complete'` and `ots_digest`, and **leave `ots_stamped_at` unchanged** (design D1 —
  do not stamp a three-year-old anchor with today's date). On the **deferred** outcome write
  **nothing** — no `ots_path`, `ots_state`, `ots_digest` or `ots_stamped_at` — and leave the row
  `pending` and in the queue; count it as neither stamped nor failed.
- [ ] 2.11 `proofs.stamp_pending`: in the `OtsPathError` permanent-skip branch, clear `ots_digest`
  alongside the existing `ots_path = None` / `ots_stamped_at = None` — no provenance for a proof
  that does not exist.
- [ ] 2.11a `proofs.upgrade_incomplete`: **corroborated backfill** (design D3). For each row it
  already visits whose `ots_digest` is NULL and whose `.ots` parses (reuse task 2.1's offline
  helper — no subprocess, no extra calendar traffic), set `ots_digest` to the proof's committed
  digest **if and only if** it equals `entry.sha256`. A non-matching parse leaves `ots_digest` NULL
  and logs at `WARNING` naming the relpath, `entry.sha256`, the parsed proof digest, and the action
  ("run `cairn verify` / the panel's Verify on this file") — it is the corrupted/swapped/misfiled
  case the column exists to catch. A row that **already** has `ots_digest` is never rewritten.
  Neither branch changes whether the proof is upgraded, and neither may raise out of the pass.

### Verify blame (sprint-1 design D1's IOU)

- [ ] 2.12 `ots.py`: add `proof_digest: str | None = None` to `VerifyResult`; set it in
  `_verify_via_explorer` on the returns where the proof was parsed. The node backend sets nothing
  (it establishes no digest disagreement — sprint-1 D1).
- [ ] 2.13 `routes.py::verify_run`: extend the `mismatch_blame` ladder per design D7, **in that
  order**. The "`proof_digest` known and ≠ `ots_digest`" branch is evaluated **first** among the
  provenance branches; `proof-stale` is reachable **only** when `proof_digest == ots_digest` and
  `ots_digest != live`. Where `ots_digest` is recorded but `proof_digest` is unavailable, fall
  through to the NULL-provenance wording — never to `proof-stale`. Where `fe.ots_digest` is NULL, the
  sprint-1 heuristic and its wording are **unchanged**.
- [ ] 2.14 `src/cli.py::_cmd_verify`: the same ladder in the same order and the same distinctions,
  so the panel and the command line never disagree about which artifact is blamed.
- [ ] 2.15 `templates/partials/verify_result.html`: split the `proof` verdict's copy — with
  provenance it is a positive finding about the **proof** ("this is not the proof Cairn placed for
  these bytes"), without it, sprint 1's "Cairn cannot tell which" survives verbatim. Neither
  version may claim anything about the file's bytes, which match their baseline. Make
  `proof-stale`'s "a re-stamp is pending" clause conditional on one actually being owed.

### Tests (Slice A)

- [ ] 2.16 A complete proof at the canonical path whose anchor the caller **confirmed** survives a
  same-digest re-stamp: the `.ots` bytes are byte-identical afterwards, `ots_state` is `complete`, and
  `ots_stamped_at` was not moved forward.
- [ ] 2.17 A different-digest stamp keeps **both**: the canonical path holds the new digest's proof
  and the old proof is byte-identical under `.superseded/…/<old digest>.ots`.
- [ ] 2.18 The **full #15 narrative**, end to end: stamp → `accept_collection` (row deleted) →
  file restored on disk → rescan → stamp. Assert the original proof's bytes still exist and are
  reachable, and that `/verify` for the file does not report a proof made today.
- [ ] 2.19 An unreadable existing proof is archived, not deleted, and the new proof is placed.
- [ ] 2.20 An archive failure leaves the file `pending` and the **existing proof intact** at the
  canonical path — nothing placed, nothing dropped to `none`.
- [ ] 2.21 A batch where one member's old proof cannot be archived still places every other member's
  proof (batch isolation is preserved).
- [ ] 2.22 The unwritable-proof-path skip clears `ots_digest` along with `ots_path`.
- [ ] 2.23 Blame: with `ots_digest == live` and a disagreeing proof → `proof`, established wording;
  with `ots_digest != live` **and `proof_digest == ots_digest`** → `proof-stale`; with `ots_digest`
  NULL → sprint 1's exact wording. Assert the same three for `cairn verify`.
- [ ] 2.23a Blame, the **A/B/C** case: recorded provenance `A`, live == baseline `B`, stored proof
  committing to a third digest `C`. Assert **`proof`** with the established "not the proof Cairn
  recorded placing" wording, and assert the proof-predates-this-version wording is **absent** — a
  staleness-first ladder passes every other blame test and fails this one. Panel **and**
  `cairn verify`.
- [ ] 2.23b Blame, provenance recorded but no parsed `proof_digest` on the result: falls back to the
  NULL-provenance wording, **not** to `proof-stale`. Panel and `cairn verify`.
- [ ] 2.24 A pending file whose canonical proof commits to a **different** digest is **not** adopted:
  it is stamped normally and the existing proof is preserved intact in the archive. (The positive
  adoption case is 2.31; the refusal cases are 2.32/2.33/2.34/2.35.)
- [ ] 2.25 Upgrade backfill, matching case: a row with `ots_digest` NULL whose stored proof commits
  to its `sha256` comes out of `upgrade_incomplete` with `ots_digest` set to that digest, and the
  upgrade outcome (`upgraded` / `still_incomplete`) is unchanged by the fill.
- [ ] 2.26 Upgrade backfill, **non**-matching case: a row with `ots_digest` NULL whose stored proof
  commits to some other digest comes out **still NULL**, a `WARNING` is logged naming both digests,
  and the upgrade still runs. This is the anti-laundering test — assert the NULL, not just the log.
- [ ] 2.27 Upgrade backfill leaves an already-recorded `ots_digest` untouched (including when the
  proof on disk now parses to something else — that disagreement is verify's finding to report, not
  the upgrade pass's to overwrite), and an unreadable/absent `.ots` leaves it NULL without raising.
- [ ] 2.28 **Archive collision keeps both**: archive digest `D`, then archive a *different* proof for
  the same `D`. Assert both files exist, byte-identical to what went in, under `<D>.ots` and
  `<D>.1.ots`, and that neither was overwritten.
- [ ] 2.28a **Preservation without hard links**: with `os.link` patched to raise
  `OSError(EPERM)` (a writable store that rejects hard links), a superseded proof is still archived
  byte-identically and the new proof is placed — no transient `OtsError`, nothing left retrying.
  Assert `os.link` was never called at all, so the implementation cannot be link-based.
- [ ] 2.29 **Interrupted between archive and place**: simulate a failure after the archive move and
  before the placement. Assert the old proof is intact in the archive, the canonical path is absent,
  the row is still `pending` with `ots_state`/`ots_digest` unwritten, and a re-run completes the
  placement.
- [ ] 2.30 **Interrupted after placement, before the DB commit**: assert the canonical path holds the
  new proof, the old proof is in the archive, and the row's un-committed state leaves the file
  `pending` — the next pass re-enters placement, finds its own same-digest proof, and (per 2.9)
  adopts it, archives-and-replaces it, or defers, in no case losing a proof.
- [ ] 2.31 **Adoption, anchored**: a pending file whose canonical proof commits to its `sha256` and
  whose anchor verifies is adopted — `ots_state='complete'`, `ots_digest` set, `ots_stamped_at`
  **not** moved forward — with the stamp helper asserted un-called.
- [ ] 2.32 **Recorded provenance does not qualify for adoption**: a pending row whose `ots_digest`
  already equals both its `sha256` and the canonical proof's committed digest, where that proof's
  anchor does **not** confirm, is **not** adopted and is **not** recorded `complete` — it is stamped
  normally and the existing proof is preserved. Assert the backend lookup **was** made (a
  provenance short-circuit would skip it) and that the row's state came from the newly placed proof.
- [ ] 2.33 **Incomplete is never adopted**: a same-digest `incomplete` canonical proof is archived and
  a fresh proof placed; the row ends `incomplete` with `ots_stamped_at = now` (so it stays visible to
  `stale_incomplete`), and the old proof is intact in the archive.
- [ ] 2.34 **Forged / unverifiable is never adopted _and_ never kept**: a same-digest proof carrying
  an attestation the backend does not confirm is archived and the fresh proof takes the canonical
  path; `ots_digest` is recorded from the **newly placed** proof, never from the rejected one. Assert
  the canonical path's bytes **changed** — a `_place_proof` that ignored the caller's verdict would
  keep the forgery and pass every other test here.
- [ ] 2.35 **Unreachable backend defers: no adoption, no demotion, no recorded claim**: with the
  verification backend unreachable, a same-digest anchored proof is *not* adopted and the row is
  **not** recorded `complete` — it stays `pending` with `ots_state`/`ots_digest`/`ots_stamped_at`
  unwritten. Assert the existing proof is byte-identical at the canonical path **and** the proof
  produced in that pass exists in the archive family for its digest (nothing discarded). Then re-run
  with the backend answering and assert the file reaches a conclusive outcome. Run the same case with
  the row's `ots_digest` pre-populated and assert the outcome is identical — an outage plus recorded
  provenance must not add up to a `complete`.
- [ ] 2.36 **Concurrent stampers**: two stamps of one collection from separate sessions/processes —
  the second is refused (message names the collection), places/adopts nothing, does not block, and
  every proof the first placed is intact. Cover `cairn stamp` and `cairn upgrade`; assert the
  refused-everywhere case exits non-zero.
- [ ] 2.36a **A fleet run that did some work exits zero**: `cairn scan`, `cairn stamp` and
  `cairn upgrade` over two collections where one claim is held — the skipped collection is named, the
  other is processed, and the exit status is **0**. One busy collection must not fail a healthy run.
- [ ] 2.37 **`cairn scan` refusal reads as a refusal, and exits non-zero when nothing ran**: a scan
  whose claim is lost prints the in-progress message and not an all-zeroes result line, and a scan in
  which **every** requested collection was skipped exits non-zero.

## 3. Slice B — a file that comes back different is not "restored" (#21)

Files owned: `src/services/scanner.py`, `routes.py::_event_view`,
`templates/collection_review.html`, `templates/partials/_event_row.html`, `tests/test_scanner.py`,
new `tests/test_restored_changed.py`.

- [ ] 3.1 `scanner.py`, restore branch: capture `prior = row.sha256` **before** any assignment, hash
  the file, then branch on the comparison. `row.sha256` is still updated to the observed digest in
  every outcome — the fix is *compare before overwrite*, not *stop overwriting* (design D6).
- [ ] 3.2 Identical bytes → today's behaviour **unchanged**: `ok`, `restored` born acknowledged,
  `summary.restored += 1`, `ots_state` untouched (no re-stamp).
- [ ] 3.3 No recorded digest (`prior is None`) → today's `restored`, with `events.detail` recording
  that no digest was available to compare. Nothing established ⇒ nothing alarmed.
- [ ] 3.4 Different bytes → `status='modified'` in **both** modes; one `restored_changed` event with
  `acknowledged_at=None`; `events.detail` carrying **both digests in full**; `summary.modified += 1`;
  `_record_alarm("restored_changed", relpath)`; and `ots_state='pending'` for `perfile` collections.
  Emit **only** `restored_changed` — not a `restored` event as well.
- [ ] 3.5 The reappeared row's id joins the batched `missing`-ack list in **every** outcome
  (design D5). Keep the `kind='missing'` scoping and the same-transaction ordering exactly as
  sprint 1 left them; do not widen the `UPDATE`.
- [ ] 3.6 Add a `restored_changed` counter to `RunSummary` for the CLI scan line / logging. **Do not
  add a `runs` column** — the file is already counted in `runs.modified` (design D4).
- [ ] 3.7 `routes.py::_event_view` + `partials/_event_row.html`: render the new kind with its own
  label, colour and icon, and show `events.detail` for it the way `moved` already does. It must read
  as alarming, not informational.
- [ ] 3.8 `templates/collection_review.html`: fix the three places that describe a check the scan did
  not perform (design "Grounding", lines ~96-97, ~131-134, ~144) so they describe the comparison that
  now happens **and both of its outcomes** — a match returns the file to OK; a mismatch raises a new
  alert rather than clearing the old one. Do not touch the Acknowledge-vs-Accept contrast card or the
  recovery panel (#12: correct as built).
- [ ] 3.9 Tests: a restore with identical bytes is `ok` + `restored` and its `missing` alert is
  closed (the sprint-1 behaviour must not regress).
- [ ] 3.10 Tests: a restore with different bytes is `modified` + an **unacknowledged**
  `restored_changed`, both digests appear in `detail`, `ots_state` is `pending` on a `perfile`
  collection, and the `missing` alert is still closed.
- [ ] 3.11 Tests: **the churn case** — a wrong restore into a `churn` collection alarms and appears
  in `summary.alarming` (this is the reason the new kind exists; a reused `modified` would be silent
  here).
- [ ] 3.12 Tests: an open WORM `modified` event on the same file survives the restore ack
  (#12 rejected fix 7).
- [ ] 3.13 Tests: a wrong restore is **not** swallowed by `_reconcile_moves` when a same-content
  rename happens in the same scan, and is **not** promoted by the deep pass's auto-baseline (it is
  `modified`, not `new`) — auto-baseline is enabled in production.

## 4. Integration (on the merged result, after both slices land)

- [ ] 4.1 Merge both slices onto the base and run the authoritative gate **once on the merged tree**:
  `PYTHONPATH=. pytest -q`, `ruff check .`, `alembic upgrade head`.
- [ ] 4.2 **The interlock test** (belongs to neither slice, so it is written here): a `perfile` file
  goes missing → is restored with **different** bytes → the scan classifies `restored_changed` and
  queues a re-stamp → the stamp pass runs. Assert the **original bytes' proof still exists** in the
  archive and the canonical path now holds the new bytes' proof. Without #15 this scenario destroys
  the old proof; without #21 it never happens at all.
- [ ] 4.2a **The accepted #39 limitation, pinned by a test** (it crosses both slices): move a file so
  `_reconcile_moves` repoints its `relpath` while `ots_path` still names the old path, then add and
  stamp a new file at that old path. Assert the moved row's original proof is **preserved** (not
  destroyed), that `/verify` on the moved row now reports the established `proof` blame rather than
  passing green, and that verification / download / export / upgrade all still resolve the moved
  row's recorded `ots_path` — i.e. the other file's proof. This is the qualification the
  canonical-consumer scenario carries; the test exists so the limitation is asserted, not assumed.
- [ ] 4.3 Grep for production callers of every new export (`_place_proof`'s outcome type, the archive
  path builder, the proof parser, `ots_digest`) — fan-out ships green-but-unwired code.
- [ ] 4.4 `openspec validate guard-proof-and-restore-integrity --strict` green.
- [ ] 4.5 `make audit` (pip-audit) green.

## 5. Gates

- [ ] 5.1 **`openspec-verifier` subagent** audits the implementation against the spec deltas.
  Iterate to zero blocking gaps. It must not be an agent that wrote any of the code.
- [ ] 5.2 **Adversarial Codex pass — mandatory.** This change touches the scan→diff→classify path
  **and** OTS stamp/proof placement: both are CLAUDE.md's named mandatory triggers. Frame it as a
  defensive PASS/FAIL control review and tell it what "wrong" means here — **the expensive failure
  is a false negative**: a wrong restore that scans clean, an alarm that is acknowledged away, a
  proof that is destroyed or replaced by a weaker one, a `verify` that blames the wrong artifact,
  or an `ots_digest` recorded from a proof nothing corroborated. Commit first (Codex reads the
  committed tree), run it in the background with both redirects, and ask for the machine-readable
  verdict block.
- [ ] 5.3 Fix every BLOCKER/MAJOR and re-run until it converges, saying which findings were
  addressed. If successive rounds keep producing **new classes** of finding, escalate rather than
  tuning — that pattern means the design is wrong, not that the reviewer is thorough.
- [ ] 5.4 Deploy: commit → push → `make deploy` → **`make migrate`** (this change adds revision
  `0011`, so the migrate step is required, not optional). Verify with `make status` / `/healthz`.
- [ ] 5.5 **`user-representative` pass** on the live panel: the verify card's new proof-blame
  wording, the review page's corrected restore copy, and a `restored_changed` row in the event feed.
  Brief it that this is a self-hosted tool for a technical operator, not a consumer app.
- [x] 5.6 File the follow-up issue for design **D8** (a moved file's `ots_path` still points at the
  old relpath's canonical proof — now detectable, still wrong) — filed as **#39**.
- [ ] 5.7 Update `CLAUDE.md`'s working notes with this change's summary, then
  `/openspec-archive-change` and push, closing **#15** and **#21**.
