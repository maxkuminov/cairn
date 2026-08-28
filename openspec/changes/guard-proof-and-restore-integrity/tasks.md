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
  including a superseded one; no per-file accept, no accept re-scoping, no `/review` fleet page.
- **Do not bundle findings from other audit issues.** Sprints 2 and 3 are sequenced on purpose.
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
  - A `downgrade()` that reverses both.
- [ ] 1.4 `src/models/db.py`: add `FileEntry.ots_digest: Mapped[str | None] =
  mapped_column(String(64))` with a docstring/comment stating it records **the digest the proof
  Cairn placed at `ots_path` commits to**, written with `ots_path`/`ots_state` and cleared with
  them; and extend `ck_events_kind` to `('added','modified','missing','restored','moved','restored_changed')`.
- [ ] 1.5 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green, and `alembic upgrade head` clean against a scratch DB.
- [ ] 1.6 Commit shared prep on the base branch **before** creating any worktree — agent worktrees
  branch from the committed base, so an uncommitted migration is invisible to both slices.

## 2. Slice A — a stamp never destroys a proof, and each proof records its digest (#15, D1 IOU)

Files owned: `src/services/ots.py`, `src/services/proofs.py`, `routes.py::verify_run`,
`src/cli.py::_cmd_verify`, `templates/partials/verify_result.html`, `tests/test_ots.py`, new
`tests/test_proof_preservation.py`.

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
  `ENAMETOOLONG` and `_proof_output_writable` keeps applying only to the canonical path.
- [ ] 2.3 `_place_proof`: when the canonical output path is **unoccupied**, behave exactly as today
  (`mkdir` + `os.replace`, same ENAMETOOLONG-permanent / everything-else-transient classification).
  This is the only path an ordinary new-file stamp takes; do not add cost to it beyond one `stat`.
- [ ] 2.4 `_place_proof`: when the canonical path **is** occupied, apply the four-way rule (design
  D1 table): same digest + complete → **keep existing, discard staged**; same digest + incomplete →
  archive then place; different digest → archive under its digest then place; unreadable → archive
  under `unknown/` then place.
- [ ] 2.5 `_place_proof`: **archive first, place second** — never the reverse. A failure to archive
  **refuses the placement** and raises a **transient** `OtsError` (the member stays `pending`; it is
  never dropped to `none`, because archiving is not the final output path — see `ots.py`'s module
  docstring rule, which must not be contradicted).
- [ ] 2.6 `_place_proof`: if the archive target already exists, **discard the incoming duplicate,
  do not overwrite** (design D1: the earlier-archived proof for a digest is the stronger claim).
- [ ] 2.7 `_place_proof`: log at `WARNING` naming **both** paths whenever a proof is superseded or a
  staged proof is discarded — the archive has no panel surface, so the log is its discoverability.
- [ ] 2.8 `_place_proof` returns an outcome (`placed: bool`, `digest`, `state`) instead of `None`;
  `stamp_via_symlink` returns it, and `stamp_batch_via_symlink` returns `list[StampOutcome | None]`
  (with `None` still meaning "this member failed, fall back to a single-file stamp"). Update the two
  call sites in `proofs.py` and the existing `tests/test_ots.py` expectations.

### Adoption + provenance writes

- [ ] 2.9 `proofs.stamp_pending`: before building the batch, drop from `work` any pending file whose
  canonical proof already exists **and** commits to the row's own `sha256` — record `ots_path`,
  `ots_state` (from the parsed attestation), `ots_digest`, and count it as stamped without spending a
  staging symlink or a calendar round-trip. This is the cheap path for #15's headline
  accept→restore→rescan case.
- [ ] 2.10 `proofs.stamp_pending`: write `ots_digest` on every successful placement, in the same
  transaction as `ots_path`/`ots_state`/`ots_stamped_at`. On the **kept-existing** outcome set
  `ots_state='complete'` and `ots_digest`, and **leave `ots_stamped_at` unchanged** (design D1 —
  do not stamp a three-year-old anchor with today's date).
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
- [ ] 2.13 `routes.py::verify_run`: extend the `mismatch_blame` ladder per design D7. Where
  `fe.ots_digest` is NULL, the sprint-1 heuristic and its wording are **unchanged**.
- [ ] 2.14 `src/cli.py::_cmd_verify`: the same ladder and the same distinctions, so the panel and
  the command line never disagree about which artifact is blamed.
- [ ] 2.15 `templates/partials/verify_result.html`: split the `proof` verdict's copy — with
  provenance it is a positive finding about the **proof** ("this is not the proof Cairn placed for
  these bytes"), without it, sprint 1's "Cairn cannot tell which" survives verbatim. Neither
  version may claim anything about the file's bytes, which match their baseline. Make
  `proof-stale`'s "a re-stamp is pending" clause conditional on one actually being owed.

### Tests (Slice A)

- [ ] 2.16 A complete proof at the canonical path survives a same-digest re-stamp: the `.ots` bytes
  are byte-identical afterwards, `ots_state` is `complete`, and `ots_stamped_at` was not moved
  forward.
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
  with `ots_digest != live` → `proof-stale`; with `ots_digest` NULL → sprint 1's exact wording.
  Assert the same three for `cairn verify`.
- [ ] 2.24 A pending file whose canonical proof already commits to its digest is adopted **without**
  invoking `ots stamp` (assert the subprocess/stamp helper was not called).
- [ ] 2.25 Upgrade backfill, matching case: a row with `ots_digest` NULL whose stored proof commits
  to its `sha256` comes out of `upgrade_incomplete` with `ots_digest` set to that digest, and the
  upgrade outcome (`upgraded` / `still_incomplete`) is unchanged by the fill.
- [ ] 2.26 Upgrade backfill, **non**-matching case: a row with `ots_digest` NULL whose stored proof
  commits to some other digest comes out **still NULL**, a `WARNING` is logged naming both digests,
  and the upgrade still runs. This is the anti-laundering test — assert the NULL, not just the log.
- [ ] 2.27 Upgrade backfill leaves an already-recorded `ots_digest` untouched (including when the
  proof on disk now parses to something else — that disagreement is verify's finding to report, not
  the upgrade pass's to overwrite), and an unreadable/absent `.ots` leaves it NULL without raising.

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
