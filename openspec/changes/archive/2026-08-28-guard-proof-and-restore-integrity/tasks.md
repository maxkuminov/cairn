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

- [x] 1.1 Confirm the working tree is on the intended base: `src/services/ots.py` contains
  `_place_proof` **and** `_proof_output_writable`; `src/control_panel/routes.py` contains
  `mismatch_blame`; `openspec/changes/archive/2026-08-28-fix-ux-audit-sprint1/` exists. If any is
  missing, the branch is stale — stop.
- [x] 1.2 Confirm the Alembic head is `0010_auto_baseline_new` (`ls alembic/versions/`). This
  change's revision is `0011`; if the head has moved, renumber before writing it.
- [x] 1.3 Write `alembic/versions/0011_proof_provenance_and_restored_changed.py`:
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
- [x] 1.4 `src/models/db.py`: add `FileEntry.ots_digest: Mapped[str | None] =
  mapped_column(String(64))` with a docstring/comment stating it records **the digest the proof
  Cairn placed at `ots_path` commits to**, written with `ots_path`/`ots_state` and cleared with
  them; and extend `ck_events_kind` to `('added','modified','missing','restored','moved','restored_changed')`.
- [x] 1.4a Test the migration against a **populated** database: apply `0011`, insert a
  `restored_changed` event, run `alembic downgrade` and assert it **fails** with the count-naming
  error and that every table is untouched (no event deleted or re-kinded); then delete that event and
  assert the downgrade succeeds, restoring the old CHECK and dropping `files.ots_digest` while
  preserving every other row. Lives with shared prep because shared prep owns the migration.
- [x] 1.5 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green, and `alembic upgrade head` clean against a scratch DB.
- [x] 1.6 Commit shared prep on the base branch **before** creating any worktree — agent worktrees
  branch from the committed base, so an uncommitted migration is invisible to both slices.

## 2. Slice A — a stamp never destroys a proof, and each proof records its digest (#15, D1 IOU)

Files owned: `src/services/ots.py`, `src/services/proofs.py`, `routes.py::verify_run`, `src/cli.py`
(`_cmd_verify`, `_cmd_stamp`, `_cmd_upgrade`, and `_cmd_scan`'s refusal line only),
`src/services/scheduler.py` (claim audit only), `templates/partials/verify_result.html`,
`tests/test_ots.py`, new `tests/test_proof_preservation.py`, new `tests/test_proof_serialization.py`.

### Proof reading

- [x] 2.1 `ots.py`: add an offline helper that parses a stored `.ots` **with the OpenTimestamps
  library** (lazy import inside the function, as `_verify_via_explorer` already does) and returns
  its committed digest **and** whether it carries a `BitcoinBlockHeaderAttestation` — one parse,
  both facts. It must never raise for an unreadable/absent file: return "unknown". No subprocess
  (`ots info` costs a process spawn per occupied path).

### Preservation

- [x] 2.2 `ots.py`: add the superseded-archive path builder —
  `<proof_store>/.superseded/<collection_id>/<digest[:2]>/<digest>.ots`, and
  `…/<collection_id>/unknown/<uuid>.ots` for an unreadable proof. Names are **fixed-length**: no
  watched filename may influence archive path length (design D1), so the archive can never trip
  `ENAMETOOLONG` and `_proof_output_writable` keeps applying only to the canonical path. On a taken
  name the builder yields `<digest>.1.ots`, `<digest>.2.ots`, … — the first free index (design D1,
  "Archive collision").
- [x] 2.3 `_place_proof`: when the canonical output path is **unoccupied**, behave exactly as today
  (`mkdir` + `os.replace`, same ENAMETOOLONG-permanent / everything-else-transient classification),
  plus task 2.6a's single canonical-directory `fsync` after the `os.replace`. This is the only path
  an ordinary new-file stamp takes; do not add cost to it beyond one `stat` and that one `fsync`.
- [x] 2.4 `_place_proof`: when the canonical path **is** occupied, apply the placement rule (design
  D1 table): same digest + complete + **anchor confirmed by the caller** → **keep existing, discard
  staged**; same digest + complete + **anchor disproven by the caller** → archive then place; same
  digest + complete + **no verdict** (backend unreachable, or no lookup made) → **defer**; same digest
  + incomplete → archive then place; different digest → archive under its digest then place;
  unreadable → archive under `unknown/` then place. `_place_proof` stays **offline** — it takes the
  caller's verdict as an argument (`confirmed` / `disproven` / absent) rather than reaching the
  network, so a forged syntactic attestation cannot hold the canonical path (design D1a).
- [x] 2.4a `_place_proof`, the **deferred** branch: change nothing on disk except to preserve the
  **staged** proof into its digest's archive family (task 2.6's exclusive create), leave the existing
  proof byte-identical at the canonical path, return the `deferred` outcome, and **raise nothing** —
  the caller writes no `ots_path`/`ots_state`/`ots_digest`/`ots_stamped_at` and the row stays
  `pending` for a later pass. A syntactic attestation nobody confirmed must never be recorded
  `complete`, and an outage must never demote or discard either proof (design D1a).
- [x] 2.5 `_place_proof`: **archive first, place second** — never the reverse. A failure to archive
  **refuses the placement** and raises a **transient** `OtsError` (the member stays `pending`; it is
  never dropped to `none`, because archiving is not the final output path — see `ots.py`'s module
  docstring rule, which must not be contradicted).
- [x] 2.6 `_place_proof`: the archive **never discards and never overwrites**. If the archive target
  exists, preserve under the next free monotonic suffix so **both** proofs survive (design D1 —
  strength comparison is a judgement the archive must not make; the earlier-archived proof is not
  reliably the stronger one). Write it as an **exclusive create that does not require hard-link
  support**: `os.open(candidate, O_CREAT | O_EXCL | O_WRONLY)` — `EEXIST` ⇒ try the next index — then
  copy the source's bytes in, `flush` + `os.fsync` **the file**, `close`, then **fsync the archive's
  directory chain** through the shared helper of task 2.6b (`fd = os.open(dir, os.O_RDONLY)` →
  `os.fsync(fd)` → `os.close(fd)`), and
  **only then** `os.unlink` the source. Fsyncing the file alone is not enough: a `fsync`ed file whose
  *directory entry* is not durable can lose its name across a power cut while the source's `unlink`
  persists — both names gone, the only proof destroyed. Order of the chain is **deepest-first,
  parent-after-child** (a directory's `fsync` is what makes durable the entry it holds, so it is
  synced only after that entry exists): the archived file's own directory first (this is what makes
  the new `.ots` name durable), then each ancestor **this call created**, ending with the first
  **pre-existing** ancestor (it holds the shallowest new directory's name); stop there — ancestors
  above it already existed durably.
  **(Amended by 5.3a/5.3b/5.3d.)** The chain now runs to the **proof-store root** rather than to the
  first pre-existing ancestor; the byte copy must be a **complete** write; and the suffix search has
  **no ceiling**.
  Do **not** use `os.replace` (silently overwrites) and do **not** use `os.link` (needs hard links,
  which the proof store's writable-filesystem contract does not promise — on a CIFS/FAT/FUSE store it
  would turn every occupied-path placement into a permanent retry loop reported as transient). No
  preflight probe. Index selection is safe because the whole sequence runs under the collection claim
  (task 2.8a) and the archive path is already scoped by `<collection_id>`.
- [x] 2.6a `_place_proof`: after the placement `os.replace(staged, canonical)` (the occupied-path
  branch's step 2, and the `mkdir` + `os.replace` of task 2.3), fsync the **canonical** parent
  directory the same way, through the same task-2.6b helper — plus any canonical ancestor this call had to `mkdir`, in the same
  deepest-first / parent-after-child order — **before** returning the outcome, and therefore before
  the caller records `ots_path`/`ots_state`/`ots_digest`. A rename is not durable until the directory
  holding it is; without this the datastore can record a proof path whose name did not survive a
  crash while the preserved copy sits under a name no row points at. One extra `fsync` per placement,
  on a path already off the hot loop. **(Amended by 5.3b:** the placement's chain also runs to the
  store root, through the same helper.**)**
- [x] 2.6b `ots.py`: every directory `fsync` in 2.6/2.6a goes through **one shared helper**
  (`_fsync_dir(path)`), which is where the *unsupported-operation degrade* lives. An `OSError` whose
  `errno` is exactly `EINVAL`, `ENOTSUP` or `EOPNOTSUPP` is **deterministic unsupported, never
  transient** — the filesystem cannot make directory entries durable (SMB/CIFS, FUSE, FAT-derived
  stores accept create/rename/file-`fsync` and reject this), and it will answer the same on every
  retry. On the **first** such result **per proof store**, log **one** `WARNING` naming the
  limitation — "a power loss in the instant between archiving and placement could lose the newest
  archive entry's name; canonical proofs and all previously synced entries are unaffected" — and
  record that store as **best-effort** in a module-level set keyed by the resolved proof-store path
  (process memory only: **no schema, no setting, no startup probe**). Detection is **lazy and
  in-band** — the first directory sync actually attempted for that store; do **not** add a probe or a
  startup check. Thereafter `_fsync_dir` returns immediately for that store: the sync is skipped
  **without error**, no warning repeats, and preservation/placement proceed unchanged — the file
  `fsync` still happens, the source is still unlinked last, and the caller records normally. No retry
  loop, no archive-family growth. **Any other errno** (`EIO`, `ENOSPC`, `EACCES`, …) propagates and
  keeps the transient classification of task 2.5 — placement refused, member stays `pending`, source
  never unlinked. Enumerate the three errnos **exactly**; never degrade on a bare `OSError`. This is
  a recorded **accepted limitation** (design "Crash-safety of the shuffle"): degraded name-durability
  on an exotic store beats a wedged notary, and only name-durability degrades — the proof's bytes are
  still flushed.

- [x] 2.7 `_place_proof`: log at `WARNING` naming **both** paths whenever a proof is superseded or a
  staged proof is discarded — the archive has no panel surface, so the log is its discoverability.
- [x] 2.8 `_place_proof` returns an outcome (`kind: 'placed' | 'kept' | 'deferred'`, `digest`,
  `state`) instead of `None` — `deferred` is an outcome, not a failure, and must not raise;
  `stamp_via_symlink` returns it, and `stamp_batch_via_symlink` returns `list[StampOutcome | None]`
  (with `None` still meaning "this member failed, fall back to a single-file stamp"). Update the two
  call sites in `proofs.py` and the existing `tests/test_ots.py` expectations.

### Single-writer serialization (design D10)

- [x] 2.8a `src/cli.py::_cmd_stamp`: wrap the whole stamp in the collection's operation claim.
  Replace the direct `mark_unstamped_pending` + `stamp_pending` calls with the existing
  `proofs.run_stamp_backfill` for `--all`, and for the plain (already-`pending`) case open and
  `collections.claim_run` a `kind='stamp'` run the same way before calling `stamp_pending`. A lost
  claim **refuses**: print that an operation is already in progress for that collection, stamp
  nothing, do not wait, and exit non-zero.
- [x] 2.8b `src/cli.py::_cmd_upgrade`: stop calling `upgrade_incomplete(session)` fleet-wide with no
  run. Iterate collections, and for each open + `claim_run` a `kind='upgrade'` run exactly as
  `scheduler.run_daily_upgrade` does, calling `upgrade_incomplete(session, collection)`. A collection
  whose slot is held is skipped **and named**; if every collection was skipped, exit non-zero.
  Prefer factoring the scheduler's existing per-collection upgrade body into a shared helper over
  duplicating it.
- [x] 2.8c `src/cli.py::_cmd_scan`: a collection whose scan returned `result='skipped'` (the claim
  was lost) must print that it was skipped because an operation is in progress — not the ordinary
  all-zeroes result line, which reads as a clean scan. **Exit non-zero when *every* requested
  collection was skipped** and zero when at least one was actually scanned, the same rule 2.8a/2.8b
  apply, so a cron `cairn scan` that examined nothing cannot record a clean integrity pass. Do not
  change `scanner.py` (Slice B owns it); `summary.result == "skipped"` already carries the fact.
- [x] 2.8d `src/services/scheduler.py`: audit only — confirm the scan pass and the daily upgrade both
  **(Amended by 5.3c: the audit missed `reap_orphaned_runs`, which revoked live cross-process
  claims. The scheduler is no longer audit-only.)**
  claim (they do: `scan_collection`'s `claim_run`, and `run_daily_upgrade`'s own). Expected diff is
  zero or a comment pointing at design D10. Do **not** add a claim inside `stamp_pending` or
  `upgrade_incomplete` — they are called from inside callers that already hold one (`scanner.py:540`).

### Adoption + provenance writes

- [x] 2.9 `proofs.stamp_pending`: before building the batch, adopt — and drop from `work` — any
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
- [x] 2.9a `proofs.stamp_pending`, verdict propagation: where the backend **answered** that the
  existing proof's anchor does not confirm, pass the `disproven` verdict into `_place_proof` (task
  2.4) so its keep-existing branch cannot resurrect the proof adoption just rejected; where it
  answered that the anchor confirms, pass `confirmed`; where the backend was **unreachable**, pass
  **no** verdict, so placement takes its deferred branch (task 2.4a) and the pass records nothing for
  that file rather than promoting an unverified artifact to `complete`.
- [x] 2.10 `proofs.stamp_pending`: write `ots_digest` on every successful placement, in the same
  transaction as `ots_path`/`ots_state`/`ots_stamped_at`. On the **kept-existing** outcome set
  `ots_state='complete'` and `ots_digest`, and **leave `ots_stamped_at` unchanged** (design D1 —
  do not stamp a three-year-old anchor with today's date). On the **deferred** outcome write
  **nothing** — no `ots_path`, `ots_state`, `ots_digest` or `ots_stamped_at` — and leave the row
  `pending` and in the queue; count it as neither stamped nor failed.
- [x] 2.11 `proofs.stamp_pending`: in the `OtsPathError` permanent-skip branch, clear `ots_digest`
  alongside the existing `ots_path = None` / `ots_stamped_at = None` — no provenance for a proof
  that does not exist.
- [x] 2.11a `proofs.upgrade_incomplete`: **corroborated backfill** (design D3). For each row it
  already visits whose `ots_digest` is NULL and whose `.ots` parses (reuse task 2.1's offline
  helper — no subprocess, no extra calendar traffic), set `ots_digest` to the proof's committed
  digest **if and only if** it equals `entry.sha256`. A non-matching parse leaves `ots_digest` NULL
  and logs at `WARNING` naming the relpath, `entry.sha256`, the parsed proof digest, and the action
  ("run `cairn verify` / the panel's Verify on this file") — it is the corrupted/swapped/misfiled
  case the column exists to catch. A row that **already** has `ots_digest` is never rewritten.
  Neither branch changes whether the proof is upgraded, and neither may raise out of the pass.

### Verify blame (sprint-1 design D1's IOU)

- [x] 2.12 `ots.py`: add `proof_digest: str | None = None` to `VerifyResult`; set it in
  `_verify_via_explorer` on the returns where the proof was parsed. The node backend sets nothing
  (it establishes no digest disagreement — sprint-1 D1).
- [x] 2.13 `routes.py::verify_run`: extend the `mismatch_blame` ladder per design D7, **in that
  order**. The "`proof_digest` known and ≠ `ots_digest`" branch is evaluated **first** among the
  provenance branches; `proof-stale` is reachable **only** when `proof_digest == ots_digest` and
  `ots_digest != live`. Where `ots_digest` is recorded but `proof_digest` is unavailable, fall
  through to the NULL-provenance wording — never to `proof-stale`. Where `fe.ots_digest` is NULL, the
  sprint-1 heuristic and its wording are **unchanged**.
- [x] 2.14 `src/cli.py::_cmd_verify`: the same ladder in the same order and the same distinctions,
  so the panel and the command line never disagree about which artifact is blamed.
- [x] 2.15 `templates/partials/verify_result.html`: split the `proof` verdict's copy — with
  provenance it is a positive finding about the **proof** ("this is not the proof Cairn placed for
  these bytes"), without it, sprint 1's "Cairn cannot tell which" survives verbatim. Neither
  version may claim anything about the file's bytes, which match their baseline. Make
  `proof-stale`'s "a re-stamp is pending" clause conditional on one actually being owed.

### Tests (Slice A)

- [x] 2.16 A complete proof at the canonical path whose anchor the caller **confirmed** survives a
  same-digest re-stamp: the `.ots` bytes are byte-identical afterwards, `ots_state` is `complete`, and
  `ots_stamped_at` was not moved forward.
- [x] 2.17 A different-digest stamp keeps **both**: the canonical path holds the new digest's proof
  and the old proof is byte-identical under `.superseded/…/<old digest>.ots`.
- [x] 2.18 The **full #15 narrative**, end to end: stamp → `accept_collection` (row deleted) →
  file restored on disk → rescan → stamp. Assert the original proof's bytes still exist and are
  reachable, and that `/verify` for the file does not report a proof made today.
- [x] 2.19 An unreadable existing proof is archived, not deleted, and the new proof is placed.
- [x] 2.20 An archive failure leaves the file `pending` and the **existing proof intact** at the
  canonical path — nothing placed, nothing dropped to `none`.
- [x] 2.21 A batch where one member's old proof cannot be archived still places every other member's
  proof (batch isolation is preserved).
- [x] 2.22 The unwritable-proof-path skip clears `ots_digest` along with `ots_path`.
- [x] 2.23 Blame: with `ots_digest == live` and a disagreeing proof → `proof`, established wording;
  with `ots_digest != live` **and `proof_digest == ots_digest`** → `proof-stale`; with `ots_digest`
  NULL → sprint 1's exact wording. Assert the same three for `cairn verify`.
- [x] 2.23a Blame, the **A/B/C** case: recorded provenance `A`, live == baseline `B`, stored proof
  committing to a third digest `C`. Assert **`proof`** with the established "not the proof Cairn
  recorded placing" wording, and assert the proof-predates-this-version wording is **absent** — a
  staleness-first ladder passes every other blame test and fails this one. Panel **and**
  `cairn verify`.
- [x] 2.23b Blame, provenance recorded but no parsed `proof_digest` on the result: falls back to the
  NULL-provenance wording, **not** to `proof-stale`. Panel and `cairn verify`.
- [x] 2.24 A pending file whose canonical proof commits to a **different** digest is **not** adopted:
  it is stamped normally and the existing proof is preserved intact in the archive. (The positive
  adoption case is 2.31; the refusal cases are 2.32/2.33/2.34/2.35.)
- [x] 2.25 Upgrade backfill, matching case: a row with `ots_digest` NULL whose stored proof commits
  to its `sha256` comes out of `upgrade_incomplete` with `ots_digest` set to that digest, and the
  upgrade outcome (`upgraded` / `still_incomplete`) is unchanged by the fill.
- [x] 2.26 Upgrade backfill, **non**-matching case: a row with `ots_digest` NULL whose stored proof
  commits to some other digest comes out **still NULL**, a `WARNING` is logged naming both digests,
  and the upgrade still runs. This is the anti-laundering test — assert the NULL, not just the log.
- [x] 2.27 Upgrade backfill leaves an already-recorded `ots_digest` untouched (including when the
  proof on disk now parses to something else — that disagreement is verify's finding to report, not
  the upgrade pass's to overwrite), and an unreadable/absent `.ots` leaves it NULL without raising.
- [x] 2.28 **Archive collision keeps both**: archive digest `D`, then archive a *different* proof for
  the same `D`. Assert both files exist, byte-identical to what went in, under `<D>.ots` and
  `<D>.1.ots`, and that neither was overwritten.
- [x] 2.28a **Preservation without hard links**: with `os.link` patched to raise
  `OSError(EPERM)` (a writable store that rejects hard links), a superseded proof is still archived
  byte-identically and the new proof is placed — no transient `OtsError`, nothing left retrying.
  Assert `os.link` was never called at all, so the implementation cannot be link-based.
- [x] 2.29 **Interrupted between archive and place**: simulate a failure after the archive move and
  before the placement. Assert the old proof is intact in the archive, the canonical path is absent,
  the row is still `pending` with `ots_state`/`ots_digest` unwritten, and a re-run completes the
  placement.
- [x] 2.29a **Durability ordering of the preservation sequence** (the crash window a file-only
  `fsync` leaves open): patch `os.fsync`, `os.unlink` and `os.replace` to record an ordered call log,
  archive a superseded proof onto a *newly created* archive directory chain, and assert the order —
  the archived file's descriptor is `fsync`ed, then its own directory, then each newly created
  ancestor up to the first pre-existing one (parent-after-child), and **only then** is the canonical
  source `unlink`ed; and that the canonical parent directory is `fsync`ed after the placement
  `os.replace` and before the outcome is returned. Assert no `unlink` of the source precedes the
  directory syncs. **A true power-loss test is out of scope** — nothing in this suite can cut power
  or model a filesystem's write reordering; this asserts the *call ordering* the durability argument
  rests on, which is the part the implementation can get wrong.
- [x] 2.29b **A store that cannot flush directories degrades instead of wedging**: patch `os.fsync`
  to raise `OSError(errno.ENOTSUP)` **for directory descriptors only** (file descriptors still
  succeed), then run two occupied-path placements. Assert both succeed — each superseded proof
  preserved byte-identically, each new proof at its canonical path, no `OtsError`, both rows recorded
  — that the archive family holds exactly the two preserved proofs (**no extra suffixed slot from a
  retry**), that the file's own descriptor *was* `fsync`ed and the source `unlink`ed only after that
  file `fsync`, and that **exactly one** `WARNING` naming the limitation was logged across the two
  placements (`caplog`). Then the counter-case: with `os.fsync` raising `OSError(errno.EIO)` on a
  directory, assert the placement is **refused** with a transient `OtsError`, the member stays
  `pending`, and the existing proof is still intact at the canonical path.
- [x] 2.30 **Interrupted after placement, before the DB commit**: assert the canonical path holds the
  new proof, the old proof is in the archive, and the row's un-committed state leaves the file
  `pending` — the next pass re-enters placement, finds its own same-digest proof, and (per 2.9)
  adopts it, archives-and-replaces it, or defers, in no case losing a proof.
- [x] 2.31 **Adoption, anchored**: a pending file whose canonical proof commits to its `sha256` and
  whose anchor verifies is adopted — `ots_state='complete'`, `ots_digest` set, `ots_stamped_at`
  **not** moved forward — with the stamp helper asserted un-called.
- [x] 2.32 **Recorded provenance does not qualify for adoption**: a pending row whose `ots_digest`
  already equals both its `sha256` and the canonical proof's committed digest, where that proof's
  anchor does **not** confirm, is **not** adopted and is **not** recorded `complete` — it is stamped
  normally and the existing proof is preserved. Assert the backend lookup **was** made (a
  provenance short-circuit would skip it) and that the row's state came from the newly placed proof.
- [x] 2.33 **Incomplete is never adopted**: a same-digest `incomplete` canonical proof is archived and
  a fresh proof placed; the row ends `incomplete` with `ots_stamped_at = now` (so it stays visible to
  `stale_incomplete`), and the old proof is intact in the archive.
- [x] 2.34 **Forged / unverifiable is never adopted _and_ never kept**: a same-digest proof carrying
  an attestation the backend does not confirm is archived and the fresh proof takes the canonical
  path; `ots_digest` is recorded from the **newly placed** proof, never from the rejected one. Assert
  the canonical path's bytes **changed** — a `_place_proof` that ignored the caller's verdict would
  keep the forgery and pass every other test here.
- [x] 2.35 **Unreachable backend defers: no adoption, no demotion, no recorded claim**: with the
  verification backend unreachable, a same-digest anchored proof is *not* adopted and the row is
  **not** recorded `complete` — it stays `pending` with `ots_state`/`ots_digest`/`ots_stamped_at`
  unwritten. Assert the existing proof is byte-identical at the canonical path **and** the proof
  produced in that pass exists in the archive family for its digest (nothing discarded). Then re-run
  with the backend answering and assert the file reaches a conclusive outcome. Run the same case with
  the row's `ots_digest` pre-populated and assert the outcome is identical — an outage plus recorded
  provenance must not add up to a `complete`.
- [x] 2.36 **Concurrent stampers**: two stamps of one collection from separate sessions/processes —
  the second is refused (message names the collection), places/adopts nothing, does not block, and
  every proof the first placed is intact. Cover `cairn stamp` and `cairn upgrade`; assert the
  refused-everywhere case exits non-zero.
- [x] 2.36a **A fleet run that did some work exits zero**: `cairn scan`, `cairn stamp` and
  `cairn upgrade` over two collections where one claim is held — the skipped collection is named, the
  other is processed, and the exit status is **0**. One busy collection must not fail a healthy run.
- [x] 2.37 **`cairn scan` refusal reads as a refusal, and exits non-zero when nothing ran**: a scan
  whose claim is lost prints the in-progress message and not an all-zeroes result line, and a scan in
  which **every** requested collection was skipped exits non-zero.

## 3. Slice B — a file that comes back different is not "restored" (#21)

Files owned: `src/services/scanner.py`, `routes.py::_event_view`,
`templates/collection_review.html`, `templates/partials/_event_row.html`, `tests/test_scanner.py`,
new `tests/test_restored_changed.py`.

- [x] 3.1 `scanner.py`, restore branch: capture `prior = row.sha256` **before** any assignment, hash
  the file, then branch on the comparison. `row.sha256` is still updated to the observed digest in
  every outcome — the fix is *compare before overwrite*, not *stop overwriting* (design D6).
- [x] 3.2 Identical bytes → today's behaviour **unchanged**: `ok`, `restored` born acknowledged,
  `summary.restored += 1`, `ots_state` untouched (no re-stamp).
- [x] 3.3 No recorded digest (`prior is None`) → today's `restored`, with `events.detail` recording
  that no digest was available to compare. Nothing established ⇒ nothing alarmed.
- [x] 3.4 Different bytes → `status='modified'` in **both** modes; one `restored_changed` event with
  `acknowledged_at=None`; `events.detail` carrying **both digests in full**; `summary.modified += 1`;
  `_record_alarm("restored_changed", relpath)`; and `ots_state='pending'` for `perfile` collections.
  Emit **only** `restored_changed` — not a `restored` event as well.
- [x] 3.5 The reappeared row's id joins the batched `missing`-ack list in **every** outcome
  (design D5). Keep the `kind='missing'` scoping and the same-transaction ordering exactly as
  sprint 1 left them; do not widen the `UPDATE`.
- [x] 3.6 Add a `restored_changed` counter to `RunSummary` for the CLI scan line / logging. **Do not
  add a `runs` column** — the file is already counted in `runs.modified` (design D4).
- [x] 3.7 `routes.py::_event_view` + `partials/_event_row.html`: render the new kind with its own
  label, colour and icon, and show `events.detail` for it the way `moved` already does. It must read
  as alarming, not informational.
- [x] 3.8 `templates/collection_review.html`: fix the three places that describe a check the scan did
  not perform (design "Grounding", lines ~96-97, ~131-134, ~144) so they describe the comparison that
  now happens **and both of its outcomes** — a match returns the file to OK; a mismatch raises a new
  alert rather than clearing the old one. Do not touch the Acknowledge-vs-Accept contrast card or the
  recovery panel (#12: correct as built).
- [x] 3.9 Tests: a restore with identical bytes is `ok` + `restored` and its `missing` alert is
  closed (the sprint-1 behaviour must not regress).
- [x] 3.10 Tests: a restore with different bytes is `modified` + an **unacknowledged**
  `restored_changed`, both digests appear in `detail`, `ots_state` is `pending` on a `perfile`
  collection, and the `missing` alert is still closed.
- [x] 3.11 Tests: **the churn case** — a wrong restore into a `churn` collection alarms and appears
  in `summary.alarming` (this is the reason the new kind exists; a reused `modified` would be silent
  here).
- [x] 3.12 Tests: an open WORM `modified` event on the same file survives the restore ack
  (#12 rejected fix 7).
- [x] 3.13 Tests: a wrong restore is **not** swallowed by `_reconcile_moves` when a same-content
  rename happens in the same scan, and is **not** promoted by the deep pass's auto-baseline (it is
  `modified`, not `new`) — auto-baseline is enabled in production.

## 4. Integration (on the merged result, after both slices land)

- [x] 4.1 Merge both slices onto the base and run the authoritative gate **once on the merged tree**:
  `PYTHONPATH=. pytest -q`, `ruff check .`, `alembic upgrade head`.
- [x] 4.2 **The interlock test** (belongs to neither slice, so it is written here): a `perfile` file
  goes missing → is restored with **different** bytes → the scan classifies `restored_changed` and
  queues a re-stamp → the stamp pass runs. Assert the **original bytes' proof still exists** in the
  archive and the canonical path now holds the new bytes' proof. Without #15 this scenario destroys
  the old proof; without #21 it never happens at all.
- [x] 4.2a **The accepted #39 limitation, pinned by a test** (it crosses both slices): move a file so
  `_reconcile_moves` repoints its `relpath` while `ots_path` still names the old path, then add and
  stamp a new file at that old path. Assert the moved row's original proof is **preserved** (not
  destroyed), that `/verify` on the moved row now reports the established `proof` blame rather than
  passing green, and that verification / download / export / upgrade all still resolve the moved
  row's recorded `ots_path` — i.e. the other file's proof. This is the qualification the
  canonical-consumer scenario carries; the test exists so the limitation is asserted, not assumed.
- [x] 4.3 Grep for production callers of every new export (`_place_proof`'s outcome type, the archive
  path builder, the proof parser, `ots_digest`) — fan-out ships green-but-unwired code.
- [x] 4.4 `openspec validate guard-proof-and-restore-integrity --strict` green.
- [x] 4.5 `make audit` (pip-audit) green.

## 4a. Post-audit hardening (adversarial Codex round 1 — 3 BLOCKER, 1 MAJOR, 1 MINOR)

The review found that preservation and serialization still contained evidence-loss paths. Each fix
is scoped to the finding; none expands the change's scope.

- [x] 5.3a **B1 — a short write archived a prefix and then unlinked the original.** `_preserve_proof`
  called `os.write` once and ignored the returned count; the truncated copy was fsynced, named, and
  the intact canonical proof removed. `ots._write_all` now loops until the payload is exhausted,
  treats zero progress as a failure (`OSError` → transient `OtsError`, source untouched), and the
  written size is confirmed against the payload **before** the durability sequence begins. A failed
  copy removes the slot it exclusively created, so a doomed retry loop cannot leave truncated
  impostor proofs behind.
- [x] 5.3b **B2 — a failed attempt's directory residue was treated as durable on retry.** The
  directory chain is no longer "what this call created": `_dir_chain`/`_sync_dir_chain` flush from
  the target directory up to and including the **proof-store root**, on every successful
  preservation and every placement, whichever attempt created each directory. `_mkdirs_tracked` /
  `_sync_created_chain` are gone (no cleanup needed them). The per-directory `_fsync_dir` degrade for
  stores that cannot flush directories is unchanged.
- [x] 5.3c **B3 — startup revoked live cross-process claims.** Migration `0011` (unreleased) gains
  nullable `runs.heartbeat_at`; `collections.claim_run` stamps it and every progress write refreshes
  it (`scanner._drain`, the scan's stamp tail via a new batch-granular progress callback,
  `proofs.run_stamp_backfill` and `proofs.upgrade_collection`'s callbacks, and
  `cli.py::_cmd_stamp`'s own claim, which stamped with no progress callback at all — the very
  long-running CLI claim the finding is about).
  `scheduler.reap_orphaned_runs` now reaps only runs whose `coalesce(heartbeat_at, started)` is older
  than `RUN_HEARTBEAT_TIMEOUT_SECONDS` (15 min). A live `cairn stamp`/`upgrade` survives a panel
  restart; a dead run is reaped within the threshold. Design D10 records the accepted cost (a crash
  leaves a collection claimed for up to the threshold; the dead-man's switch is unaffected).
- [x] 5.3d **M1 — the 10,000-suffix cap could wedge preservation permanently.** Removed.
  `_claim_archive_slot` tries slot 0, and on collision seeds past the existing family with one
  `scandir` (`_next_archive_index`) before continuing the `O_EXCL` claim loop — unbounded, and one
  directory read rather than one failed `open` per occupied slot.
- [x] 5.3e **MINOR — the ordering test could not pin the ordering it claimed.** The recorder now
  captures exclusive opens, per-call write byte counts, closes, each fsync tagged file-vs-directory,
  unlinks and renames **with their paths**, and the test asserts the exact sequence. Added: the
  short-write case, the no-progress case, the failed-attempt-then-retry case (asserting the full
  chain is flushed on the successful attempt), and the crowded-family case.
- [x] 5.3f Spec + design updated to match: `ots-notarization` (complete write, chain to the store
  root, no suffix ceiling, claims-are-leases + 4 new scenarios), `datastore`
  (`runs.heartbeat_at` + its migration scenario), `integrity-scanning` (the reaper requirement,
  rewritten around liveness), design "Crash-safety of the shuffle" and D10.

## 4b. Post-audit hardening (adversarial Codex round 2 — 1 BLOCKER, 1 MAJOR, 2 MINOR)

Round 2 found no defect in the scanner's classification or transaction ordering; every finding is
about a **claim** the surfaces made that the check behind it did not establish. Same shape as round
1 (evidence overstated rather than lost), scoped to the finding.

- [x] 5.3g **B — the proof-stale reading asserted artifact identity and continued validity.**
  `proof_digest == ots_digest` compares digests, and a digest identifies bytes, not an artifact: any
  `.ots` over the same earlier bytes — fabricated, unanchored, substituted — satisfies it, and the
  ladder is reached only because verification exited on the digest disagreement *before* checking any
  attestation. "The stored proof is the one Cairn placed for an earlier version" and "the older proof
  keeps covering the earlier version" therefore laundered a swapped proof into a reassurance. Both
  surfaces now claim exactly what was compared — the proof at this path commits to the file's
  previously recorded fingerprint, not its current one, and **its Bitcoin attestations were not
  validated in this check** — with the amber verdict unchanged. Panel headline for the provenance
  case becomes "Proof commits to the previously recorded fingerprint"; the CLI line becomes
  `PROOF COMMITS TO THE PREVIOUSLY RECORDED FINGERPRINT`. The NULL-provenance legacy branch keeps
  sprint 1's wording, minus the same "keeps covering" promise, plus the non-validation statement.
  Anchor verification is deliberately **not** added here (design D7, "why not simply validate the
  anchors"): an extra explorer round-trip per stale verify, on a panel request path, to answer a
  question the operator did not ask and that changes no action. The honest claim is the fix.
- [x] 5.3h **M — the pending clause came from `status`, not from the proof state.** With provenance,
  staleness is established from the digests alone, so `status in ("modified","new")` no longer
  implies a queued re-stamp: a `perfile` collection switched to `ots_mode="none"` after a
  modification sits at `modified`/`complete` forever and nothing will ever stamp it. Both consumers
  now derive the clause strictly from `ots_state == "pending"` when provenance is established; the
  status heuristic survives **only** in the NULL-provenance legacy branch, where it is the only
  signal that the row is in the re-stamp window at all.
- [x] 5.3i **MINOR — the review page's recovery copy contradicted the scanner.** A changed restore
  acknowledges the obsolete `missing` event in the same transaction, so "raises a new alert instead
  of clearing the old one" sent the operator after an alert the scan had already closed. Both the
  hint and step 3 of "How to recover" now say the missing alert is closed and replaced by a new
  "came back changed" alert; the `web-panel` requirement and its scenario say the same.
- [x] 5.3j **MINOR — `test_restored_changed.py`'s stamping stub no longer matched `stamp_pending`.**
  The stub lacked the `settings` / `progress` parameters, so every per-file test reached `pending`
  through a `TypeError` swallowed by the scanner's blanket `except` rather than through a successful
  no-op — the post-scan tail could break for real and the suite would stay green. The stub now
  mirrors the real signature (and drives the progress callback), and the fixture's teardown asserts
  the scanner logged **no** stamp failure during the test.
- [x] 5.3k Tests + specs updated to match: `test_proof_preservation.py` (softened established-stale
  assertions, plus panel and CLI regressions for the `ots_mode="none"` mode-switch pending clause,
  in both directions), `test_ux_verify.py` (the legacy branch no longer promises the old proof keeps
  covering anything), `test_panel.py` (review recovery copy), and the `web-panel` /
  `ots-notarization` deltas + design D7.

- [x] 5.3l **MAJOR — the claim lease expired only at web startup, so a post-startup orphan wedged its
  collection for good.** Kill a `cairn stamp` after its `claim_run` commits and the `running` row
  keeps satisfying `uq_runs_one_running_per_collection` forever: every later scan/stamp/upgrade on
  that collection is refused, by the scheduler, the panel *and* the CLI, until the web service is
  restarted — and permanently in a `CAIRN_SCHEDULER_ENABLED=0` / CLI-only deployment, which runs no
  reaper at all. A monitor that has silently stopped monitoring one collection is exactly the
  false-negative shape this product cannot ship. Fixed in the two layers the finding asks for:
  (1) **`claim_run` now reconciles before it refuses** — a blocked claim calls the new
  `collections.reclaim_stale_claim`, which tests the blocker's `coalesce(heartbeat_at, started)`
  against `RUN_HEARTBEAT_TIMEOUT_SECONDS` and, if abandoned, marks it `interrupted`/`finished` (the
  reaper's own terminal semantics) and retries the claim **once**; a blocker that is still
  heartbeating refuses exactly as before. Being on the claim path, this un-wedges every entry point
  with no background machinery. The revoking UPDATE re-asserts the full stale condition in its
  `WHERE` (`result='running'` AND liveness `<= cutoff`), so a heartbeat landing between the read and
  the write wins the race, the statement matches zero rows and the claim refuses — the claim is
  never taken from a live process (design D10). (2) **The scheduler tick reaps too**
  (`reap_orphaned_runs`, retained at startup), so a long-idle collection tidies up without waiting
  for someone to attempt an operation on it. `RUN_HEARTBEAT_TIMEOUT_SECONDS` moved to
  `services/collections.py` next to the claim and is re-exported by `services/scheduler.py`, so the
  two reclamation paths cannot drift apart on the threshold.
- [x] 5.3m Docs + tests for 5.3l: design **D10** gains "The lease must expire in-band, not only where
  a reaper runs" (both layers, the one-retry rationale, and the concurrency argument for the guarded
  UPDATE); the `integrity-scanning` delta gains the requirement **"An abandoned operation claim is
  reclaimed without a restart"** (+ scenarios: reclaimed by the next claim attempt with no restart, a
  live claim never reclaimed, a concurrent heartbeat defeats the reclamation) and the startup-reaper
  requirement now says startup is not the only moment it runs. Four regressions in
  `tests/test_folder_tree_and_progress.py`: `test_claim_reclaims_an_abandoned_lease_without_a_reaper`
  (SIGKILL simulation — old heartbeat, no reaper called, the new claim succeeds and the orphan reads
  `interrupted`), `test_claim_refuses_a_lease_that_is_still_heartbeating` (fresh heartbeat → refused
  as before), and `test_a_concurrent_heartbeat_beats_the_reclaiming_update` (the holder heartbeats
  between the staleness read and the guarded UPDATE → zero rows matched, claim refused), plus
  `test_the_operation_gate_releases_an_abandoned_claim` (the gate reclaims and reads free while the
  display read reports the record and writes nothing). One existing fixture had to be corrected:
  `test_ux_dashboard.py::test_accept_refuses_while_an_operation_is_in_flight` seeded its "in-flight"
  run with the module's fixed `NOW` (weeks stale) and no heartbeat, which the gate now — correctly —
  reclaims; it seeds a live heartbeat, which is what an actually in-flight run has.

## 4c. Post-audit hardening (adversarial Codex final round)

- [x] 5.3n **The fleet-wide reaper revoked live leases — the exact race the claim path already
  guarded.** `scheduler.reap_orphaned_runs` selected the stale `running` runs, then UPDATEd them by
  `id` with only `result='running'` re-asserted: a heartbeat committed by the live holder between
  the read and the write was overwritten, marking a working `cairn stamp`/`upgrade` `interrupted`
  and freeing its collection for a second proof writer (design D10) — the loss the lease exists to
  prevent, reintroduced by the cleanup meant to protect it, and worse than the in-band path's
  version because the sweep runs every tick against the whole fleet. The UPDATE's `WHERE` now
  re-asserts the **full** stale condition (`result='running'` AND
  `coalesce(heartbeat_at, started) <= cutoff`), mirroring `collections.reclaim_stale_claim`
  exactly, so a heartbeat that lands first fails the predicate, that row is not matched, and the
  returned count is what was actually reaped rather than what was selected. The selection moved to
  `scheduler._stale_run_ids` (the counterpart of `collections._stale_claim_id`) so the read and the
  guarded write are separately visible and separately testable; the liveness comparison is now the
  claim path's `<= cutoff` in both, so the two paths cannot disagree at the boundary. Regression:
  `tests/test_folder_tree_and_progress.py::test_a_concurrent_heartbeat_beats_the_reaping_update`
  (a second connection commits a heartbeat between the selection and the UPDATE → zero rows reaped,
  the run stays `running` with no `finished`; it fails without the guard). The `integrity-scanning`
  delta's reaper requirement gains the guarded-write paragraph and the scenario **"A concurrent
  heartbeat defeats the startup reconciliation"**.

- [x] 5.3o **The lease had a timeout but no keepalive and no fence — so the timeout was a scheduled
  second writer.** Every heartbeat rode on the completion of a unit of work (a scan batch drain, a
  stamp batch, one upgraded proof), which measures the shape of the work rather than the liveness of
  the process: hashing one multi-terabyte file or a batch stalled on a slow NAS mount outlasts the
  15-minute abandonment interval on its own, so a scan that was working perfectly starved its own
  lease, was legitimately reclaimed — and then carried on, unaware, into its stamp tail as a second
  writer over a collection the panel or the scheduler had already handed to someone else (design
  D10: two writers, one canonical proof path, one `os.replace` each, one submission destroyed with
  no trace). The two missing limbs of the lease pattern are now in place.
- [x] 5.3o.1 **Keepalive, independent of work completion.** `collections.run_keepalive(run_id)` — an
  async context manager wrapping every long operation body (`scanner.scan_collection`,
  `proofs.run_stamp_backfill`, `proofs.upgrade_collection`, and `cli._cmd_stamp`'s direct
  `stamp_pending` call; `cairn scan`/`--all`/`upgrade` inherit it through those service functions).
  It refreshes `heartbeat_at` every `KEEPALIVE_INTERVAL_SECONDS` (`RUN_HEARTBEAT_TIMEOUT_SECONDS/3`
  = 5 min) from **its own session**, with the write guarded on `result='running'` so it can neither
  revive nor rewrite the liveness of a run already reclaimed, and stops as soon as that stops
  matching. Cancelled and awaited on exit; never raises into the operation, and gives up after
  `_KEEPALIVE_MAX_CONSECUTIVE_FAILURES` (3) rather than looping on a broken datastore. The existing
  per-batch heartbeats stay — they are free and `processed` belongs there anyway.
- [x] 5.3o.2 **Fence before every mutation and before finalization.** `collections.lease_held(run_id)`
  re-reads the run's `result` **in its own session** (the operation's session may sit inside a
  transaction whose snapshot predates the reclamation, and a loaded ORM attribute could be answered
  from the identity map) and answers "not held" on a datastore error. It is checked in
  `scanner._drain` before each batch commit, in the scan's stamp tail before `stamp_pending`, in
  `proofs.stamp_pending` before each batch's placement, and in `proofs.upgrade_incomplete` before
  each proof is rewritten; `collections.finalize_if_held` fuses the same test into the terminal
  UPDATE's `WHERE`. A lost lease raises `collections.LeaseLost`, which every caller handles by
  stopping. **Nothing commits after the fence fires** — the in-flight batch is rolled back and no
  further unit begins — while **proofs already placed stand**, with the rows already committed for
  them: they were placed under a lease that was valid at the time, `_place_proof` never destroys a
  proof, and deleting evidence that exists on disk to tidy up bookkeeping is the trade this product
  refuses. A reclaimed run keeps the `interrupted` state the reclamation wrote (overwriting it with
  `ok` would let a pass that never finished refresh the dead-man's switch), and a scan that lost its
  lease reports `skipped`, so `cairn scan` exits non-zero rather than recording a clean pass.
- [x] 5.3o.3 Tests in `tests/test_proof_serialization.py`:
  `test_the_keepalive_refreshes_the_lease_while_a_single_hash_runs_long` (the scan is held inside
  `_hash` so no batch can drain; the heartbeat must still advance),
  `test_a_reclaimed_scan_stamps_nothing_and_does_not_overwrite_the_reclamation`,
  `test_a_stamp_pass_stops_at_the_batch_boundary_when_its_claim_is_reclaimed` (the proof placed under
  the valid lease is still on disk), and the control
  `test_an_unreclaimed_scan_is_completely_unaffected_by_the_fence`. Two existing `stamp_pending`
  stubs gained the new `run_id` keyword so their signatures still track the real function.
  Design D10 gains "The lease needs a keepalive and a fence, not just a timeout"; the
  `integrity-scanning` delta gains the liveness + fence requirements and the `ots-notarization`
  delta gains "Proof mutation stops when the claim it runs under has been reclaimed".

- [x] 5.3p **The fence was a check-then-act, so it could be raced — the lock now sits at the
  resource.** `lease_held()` reads the claim and returns; the `os.replace` happens milliseconds
  later, past a calendar round-trip. A reclamation landing in that window (reachable in production:
  the keepalive gives up after `_KEEPALIVE_MAX_CONSECUTIVE_FAILURES` while the operation keeps
  working, so the lease can age out under a live pass) leaves the original pass and the replacement
  claimant both placing proofs for one collection — both find the canonical path free, both
  `os.replace`, one submission destroyed with no trace. No amount of re-reading the datastore closes
  it: the check and the act are different operations.
- [x] 5.3p.1 **A per-collection advisory lock on the proof store.** `ots.CollectionProofLock` takes
  `fcntl.flock(LOCK_EX)` on `<proof_store>/<collection_id>/.lock` (created with its parents; outside
  the `.staging`/`.superseded` namespaces and never a proof — every proof is `<relpath>.ots`). It
  wraps the placement critical section at **both** mutation sites: `proofs.stamp_pending` around
  each batch (the batch body is now `proofs._stamp_one_batch`, extracted unchanged so the whole
  inspect → preserve → place → record sequence is one call) and `proofs.upgrade_incomplete` around
  each `ots upgrade`, keyed on that row's own collection so a fleet-wide pass never holds two at
  once. Held **per unit of work, never per pass**: holding it across a multi-hour upgrade would
  invert the outcome, blocking the live claimant until it timed out. Acquisition is bounded
  (`PLACEMENT_LOCK_TIMEOUT_SECONDS`, 60s) and polls `LOCK_EX|LOCK_NB` rather than using
  `signal.alarm`, which only fires on the main thread; it runs through `asyncio.to_thread` so the
  wait never blocks the event loop, while release runs inline (a non-blocking syscall — and the lock
  lives on the file description, not on the thread that took it). A timeout is a **transient**
  `OtsError`: nothing placed, nothing dropped to `ots_state='none'`, and the pass stops rather than
  paying one timeout per remaining batch for a resource someone else still holds.
- [x] 5.3p.2 **A post-acquisition re-check.** The lease is re-read *inside* the lock, before
  anything is mutated; a claim found gone there raises `collections.LeaseLost` exactly as the
  pre-existing fence does (the fence body is now one closure per function, used for both the cheap
  pre-lock check and the authoritative post-lock one). Whichever placer wins the lock takes its turn
  alone; the loser aborts on the re-check without writing, in either arrival order.
- [x] 5.3p.3 **Accepted limitation, with `_fsync_dir`'s errno discipline.** A store whose filesystem
  cannot lock (`ENOLCK`/`ENOSYS`/`EINVAL`/`ENOTSUP`/`EOPNOTSUPP` — CIFS/SMB, some FUSE, NFS without
  a lock daemon) logs **one** WARNING per proof store (lazily, in-band, no probe, no setting, process
  memory in `_BEST_EFFORT_PLACEMENT_LOCK`) and proceeds on the DB fence alone; every other errno
  stays transient. Classifying it transient instead would wedge notarization forever on such a
  store — a concurrency nicety costing the notary its ability to notarize.
- [x] 5.3p.4 Tests in `tests/test_proof_serialization.py`:
  `test_a_reclamation_after_the_fence_cannot_put_two_placers_on_one_proof_path` (the verdict's exact
  ordering — `lease_held` is patched to reclaim the collection at the instant it returns `True` and
  to let the replacement claimant run a full stamp; the fake calendar still routes through the real
  `ots._place_proof`, so the assertions are about real placement: exactly one canonical proof, it is
  the winner's bytes, `LeaseLost` from the loser, and an **empty** `.superseded` family — it fails
  without the post-lock re-check, archiving the winner's proof under `unknown/`),
  `test_a_second_placer_is_excluded_by_the_lock_and_gives_up_transiently` (a second descriptor on
  the same lock file holds it: nothing placed, the file stays `pending`, and the same pass succeeds
  once the holder releases), and
  `test_a_store_that_cannot_lock_warns_once_and_keeps_the_datastore_fence`. One existing test,
  `test_ots.py::test_stamp_pending_staging_dir_failure_does_not_abort_later_chunks`, had its blanket
  `os.mkdir` break narrowed to the staging dir it names — a global break also stopped the new lock
  file being created, which is a different failure with a different (correct) answer.
  Design D10 gains "The fence is check-then-act, so the last rung is a lock at the resource" and
  the full ladder (claim → reclamation → guarded writes → keepalive → fence → resource lock with a
  post-acquisition re-check); the `ots-notarization` delta gains "Proof placement for one collection
  is serialized at the proof store itself" with four scenarios.

- [x] 5.3q **Coverage completion (verifier follow-up, tests only — no production change).** Three
  behaviours the implementation already had but nothing pinned:
- [x] 5.3q.1 `tests/test_ux_review.py::test_event_feed_draws_a_changed_restore_as_an_alarm_with_both_digests`
  — the panel renders what the scanner records. A scanner-driven changed restore reaches the
  dashboard feed as **"Came back changed"** in the danger colours (label and icon chip, asserted
  inside that row so a danger colour elsewhere cannot satisfy it), carrying `detail`'s
  **recorded → found** digests in full and the file that they belong to, with the reading-log
  control still offered because it is not born acknowledged. Without the `kind_meta` entry the row
  falls through to the muted generic "Event" and nothing else fails.
- [x] 5.3q.2 `tests/test_ux_dashboard.py::test_a_changed_restore_keeps_its_collection_off_all_clear`
  (parametrized `worm`/`churn`) — an unresolved changed restore reads **"Attention"**, never
  "All clear", on the dashboard card, the collection detail and the `op-status` fragment, and
  `_collection_status` over the real counts returns `attention`. Churn is the case worth pinning:
  an ordinary churn edit re-baselines to `ok` silently, so a row handled like one would leave the
  collection green with an unacknowledged `restored_changed` event under it.
- [x] 5.3q.3 `tests/test_proof_serialization.py::test_a_failing_keepalive_gives_up_after_three_tries_and_never_touches_the_operation`
  — the keepalive's failure branch: `touch_heartbeat` is monkeypatched to raise, and the block
  still completes (`run_keepalive` awaits its task on exit, so an escaping exception would fail the
  operation the keepalive only describes), every failure is logged (first of a run with its
  traceback), the scripted success in the middle **resets** the count, and after the third failure
  in a row there are no further attempts at all.
- [x] 5.3r **The `restored_changed` digest pair was rendered on a line that clipped it (live-pass
  finding C1).** `partials/_event_row.html` put the event's detail —
  `recorded <64hex> → found <64hex>`, 146 characters, and the **only** surviving record of the
  digest the file carried before it came back — inside `.event-row__relpath`, which is
  `white-space: nowrap` + `text-overflow: ellipsis`. At 1280px roughly 42 of those characters
  survived: the "recorded" digest was cut mid-hash and the "found" digest never reached the screen
  at all, so the row that exists to say *what* came back could not answer it. The detail now has
  its own `.event-row__detail` line (appended in a marked panel.css section: mono, muted, 11.5px,
  `white-space: normal` + `overflow-wrap: anywhere`, because a hex run offers no break
  opportunities of its own), so both digests are fully visible at any width. The relpath line is
  untouched — the alarm still names its file on the line above — and the `moved` kind's
  `old → new` detail keeps that line, where it reads as the path it is. Pinned by
  `tests/test_ux_review.py::test_event_feed_draws_a_changed_restore_as_an_alarm_with_both_digests`
  (the detail is inside the new class and never inside the relpath span) and
  `tests/test_ux_docs.py::test_the_changed_restore_digest_line_wraps_instead_of_ellipsizing`
  (the CSS rule wraps and declares neither `nowrap` nor `text-overflow`).

## 5. Gates

- [x] 5.1 **`openspec-verifier` subagent** audits the implementation against the spec deltas.
  Iterate to zero blocking gaps. It must not be an agent that wrote any of the code.
- [x] 5.2 **Adversarial Codex pass — mandatory.** This change touches the scan→diff→classify path
  **and** OTS stamp/proof placement: both are CLAUDE.md's named mandatory triggers. Frame it as a
  defensive PASS/FAIL control review and tell it what "wrong" means here — **the expensive failure
  is a false negative**: a wrong restore that scans clean, an alarm that is acknowledged away, a
  proof that is destroyed or replaced by a weaker one, a `verify` that blames the wrong artifact,
  or an `ots_digest` recorded from a proof nothing corroborated. Commit first (Codex reads the
  committed tree), run it in the background with both redirects, and ask for the machine-readable
  verdict block. **Converged: final adversarial round (post-flock tree) returned PASS, zero
  findings.**
- [x] 5.3 Fix every BLOCKER/MAJOR and re-run until it converges, saying which findings were
  addressed. If successive rounds keep producing **new classes** of finding, escalate rather than
  tuning — that pattern means the design is wrong, not that the reviewer is thorough.
  **Converged: final adversarial round (post-flock tree) returned PASS, zero findings.**
- [ ] 5.4 Deploy: commit → push → `make deploy` → **`make migrate`** (this change adds revision
  `0011`, so the migrate step is required, not optional). Verify with `make status` / `/healthz`.
- [ ] 5.5 **`user-representative` pass** on the live panel: the verify card's new proof-blame
  wording, the review page's corrected restore copy, and a `restored_changed` row in the event feed.
  Brief it that this is a self-hosted tool for a technical operator, not a consumer app.
- [x] 5.6 File the follow-up issue for design **D8** (a moved file's `ots_path` still points at the
  old relpath's canonical proof — now detectable, still wrong) — filed as **#39**.
- [ ] 5.7 Update `CLAUDE.md`'s working notes with this change's summary, then
  `/openspec-archive-change` and push, closing **#15** and **#21**.
