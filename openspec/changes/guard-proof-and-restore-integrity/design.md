# Design notes — guard proof and restore integrity

Only the decisions that go beyond what #15 and #21 already state, plus the ordering statements an
implementer needs to get right. Where the issue text is explicit, that is the spec.

## Grounding: line references verified against HEAD (`9249795` + sprint 1, HEAD `e6ebc14`)

- **#15 → `src/services/ots.py:262-280`** (`_place_proof`). Confirmed: `out_ots_path.parent.mkdir`
  then `os.replace(staged_ots, out_ots_path)`, and the only `OSError` handling is the
  ENAMETOOLONG-vs-transient classification added by `tolerate-unstampable-proof-paths`. No
  existence check anywhere on the path.
- **#15's reachability → `src/services/scanner.py:661-663`** (`accept_collection`): `elif f.status
  == "missing": await session.delete(f)`. The row — and with it `ots_path` — is deleted while the
  `.ots` stays on disk, so a re-appearance is classified `added` and re-stamped onto the live proof.
- **#21 → `src/services/scanner.py:395-414`** (the `elif row.status == "missing":` branch).
  Confirmed: `row.sha256 = await _hash(full)` is the **first** statement; no comparison exists in
  the branch at all.
- **#21.3 "fix `collection_review.html:56`"** — the line has drifted. The copy that describes a
  check the scan does not perform is now at **`collection_review.html:144`** (`"Run Scan now on the
  collection page — recovered files flip back to restored / OK"`) and **`:131-134`** (`"…to bring
  them back, then Scan now on the collection to confirm"`); `:96-97` (`"The files are back and
  nothing is missing or changed right now"`) is a third instance in the restored-alerts card.
  Sprint 1 rewrote the surrounding cards but did not touch these three claims — it deliberately
  left #21 out. All three are in scope.
- **Event-kind rendering** lives in `partials/_event_row.html:7` (a kind → label/colour/icon map)
  and `routes.py::_event_view`.
- **Alembic head is `0010_auto_baseline_new`**; this change adds **`0011`**.

## D1 — #15's scheme: keep both proofs, canonical path serves the current digest

Three schemes were considered.

**Rejected — refuse the stamp when a proof exists.** Preserves the old proof and loses the new one:
the current bytes never get notarized, and nothing ever retries (the row would have to be dropped to
`none` or left `pending` forever). "Never destroy evidence" and "never fail to take evidence" are
both requirements; refusal trades one for the other.

**Rejected — version the canonical name** (`<relpath>.<n>.ots`, or a digest suffix on the live
path). Every consumer — `/verify`, the proof download route, `cairn verify`, `cairn export`,
`upgrade_incomplete`, `export_bundle` — resolves a proof through `files.ots_path`, so versioning the
live name means the DB pointer decides which version is "the" proof, and a row whose pointer is
stale or NULL (a re-created row after accept — exactly #15's scenario) can no longer find its proof
at all. It also puts an attacker-influenced filename component next to a length-sensitive path:
`<name>.<64 hex>.ots` blows past `NAME_MAX` for any moderately long name, re-opening the failure
class `tolerate-unstampable-proof-paths` closed, and `<relpath>.<digest>.ots` can collide with the
canonical proof of a *real* watched file literally named `<relpath>.<digest>`.

**Chosen — canonical stays canonical; superseded proofs move to a content-addressed archive.**

```
<proof_store>/<collection_id>/<relpath>.ots            # canonical: the CURRENT digest's proof
<proof_store>/.superseded/<collection_id>/<dd>/<digest>.ots   # every proof ever displaced
<proof_store>/.staging/                                        # unchanged
```

- Every existing consumer is untouched: the canonical path is where it always was and holds the
  proof for the bytes on disk now.
- The archive name is a **fixed-length hex digest** with a two-hex-character shard directory, so no
  watched filename can influence its length — the archive can never be the thing that trips
  ENAMETOOLONG, and the `_proof_output_writable` pre-check keeps applying only to the canonical
  path, as its spec requires.
- Content-addressing is semantically exact: a `.ots` attests **bytes**, not a path. Two files that
  ever held the same content share one archived proof, and re-archiving the same digest is a no-op.
- `.superseded` cannot shadow a collection directory (`str(collection_id)` is always digits) — the
  same argument that makes `.staging` safe.
- Its **`<collection_id>`** level is kept even though the digest alone would be unique, so a
  collection's proofs can be moved or backed up as one subtree and a `DELETE` of a collection has an
  obvious archive counterpart if pruning is ever built.

### The four-way placement rule, and why same-digest is not simply a no-op

| existing canonical proof | action | rationale |
|---|---|---|
| unreadable | archive under `unknown/<uuid>.ots`; place staged | an unparseable file may still be a valid proof this build cannot read; deleting it is the failure mode being fixed |
| digest ≠ staged digest | archive under its digest; place staged | the old bytes' anchor is evidence for the old bytes; the new bytes need theirs |
| digest == staged, existing **complete** | **keep existing; discard staged** | #15's headline case. A fresh stamp is always `incomplete`; replacing a Bitcoin-anchored proof with a same-bytes pending one is a strict downgrade of the claim, which is exactly the loss the issue describes |
| digest == staged, existing **incomplete** | archive existing; place staged | a proof the calendars never anchored can legitimately be refreshed — the `stale_incomplete` requirement exists so a never-confirmed proof *can* be re-stamped. Nothing is lost: the old one is archived |

The digest and the completeness both come from **one** offline library parse of the existing `.ots`
(`DetachedTimestampFile.deserialize` + scanning for a `BitcoinBlockHeaderAttestation`), the same
parse `_verify_via_explorer` already performs. No subprocess: `ots info` would cost a process spawn
per occupied path.

**"Re-stamping the same digest stays cheap" is satisfied twice over.** The common shape of #15
(accept → restore identical bytes → rescan → re-stamp) never reaches the calendar at all:
`stamp_pending` adopts a pending file whose canonical proof already commits to the row's recorded
digest, before any staging symlink or `ots stamp` invocation. That check costs one `stat` and, only
when a proof is actually there, one small local parse — and for an ordinary pending set (brand-new
files) it is one negative `stat` per file.

### Crash-safety of the shuffle

Both moves are `os.replace` within the proof-store volume, so each is atomic; the **pair** is not.
Ordering is therefore load-bearing:

1. archive the existing proof (`os.replace` old → archive path),
2. `os.replace` staged → canonical.

An interruption between them leaves the canonical path **absent** and the old proof **safe in the
archive**. That is recoverable: the DB write that would set `ots_state='incomplete'` has not
happened either (the caller updates the row only after the stamp call returns), so the file is still
`pending` and the next pass re-stamps it. The reverse order would destroy the proof before
preserving it — the exact bug — so it is forbidden, not merely discouraged.

A **failure to archive refuses the placement.** It raises a transient `OtsError`, which under the
existing classification leaves the member `pending` for retry and never drops it to `none`. This is
consistent with the governing rule in `ots.py`'s module docstring: only a failure on the **final
output path** may be permanent, and archiving is not that path. A permanent-looking archive failure
cannot occur by construction (fixed-length names), so treating every archive failure as transient
costs only retries.

**Archive collision:** if the archive target already exists, the incoming duplicate is **discarded,
not overwritten**. Two proofs for one digest attest the same fact and the already-archived one was
displaced earlier, hence stamped earlier, hence the stronger "existed by" claim. Overwriting could
demote an anchored archived proof to a pending one.

### Interaction with the batch stamper

`stamp_batch_via_symlink` already treats per-member placement failure as "leave `False`, let the
caller's single-file fallback classify it". Preservation slots in underneath `_place_proof` and
inherits that behaviour unchanged: one member whose old proof cannot be archived is left `pending`
while the rest of the batch is placed. The staging flow (symlink → `<uuid>.ots` → move) is
untouched; preservation acts only on the destination.

**`_place_proof` must report which branch it took**, because the caller's row update differs
(below). It returns a small outcome (`placed: bool`, `digest: str | None`, `state:
'incomplete'|'complete'`) rather than `None`; `stamp_via_symlink` and `stamp_batch_via_symlink`
propagate it (`StampOutcome | None` per member replaces `list[bool]` — `None` still means "failed,
fall back"). Two call sites and their tests, no behavioural change for failures.

### What the caller records

- **placed** → `ots_path = out`, `ots_state = 'incomplete'`, `ots_stamped_at = now`,
  `ots_digest = <staged digest>`.
- **kept existing** → `ots_path = out`, `ots_state = 'complete'`, `ots_digest = <existing digest>`,
  and **`ots_stamped_at` is left as it is** — NULL on a row re-created by accept→restore.
  *(Flagged for Max.)* The alternative, stamping it with `now`, would print "notarized today" over a
  three-year-old anchor: the same class of lie as #12's rejected fix 6 (labelling a download
  "existed by `<ots_stamped_at>`"). The proof's own attestation, which `/verify` reads, carries the
  real date; an unknown submission time is the honest record. The one cost is that
  `stale_incomplete` filters on `ots_stamped_at IS NOT NULL`, so an adopted proof is never flagged
  stale — harmless here, since an adopted proof in this branch is `complete` by definition.

## D2 — `files.ots_digest`: what it means, and why it is never lazily filled

**Meaning:** *the digest Cairn recorded the proof it placed at `ots_path` as committing to.* It is a
record of an action Cairn took, not a description of whatever file is at that path now. That
distinction is the whole value of the column: `ots_digest` vs. the parsed proof is precisely the
comparison that detects a swapped, corrupted or misfiled `.ots`.

Consequences, all of which are spec'd:

- Written **only** at placement or adoption, in the same transaction as `ots_path`/`ots_state`.
- **Cleared with `ots_path`.** The unwritable-path skip already nulls `ots_path`/`ots_stamped_at`
  "so no stale pointer is left behind"; `ots_digest` joins them. A digest without a proof is a
  provenance claim for nothing.
- **Not touched** when a scan sets `ots_state='pending'` on a modified file. The stored proof still
  commits to what it commits to; that the file has moved on is the *point*.
- **Never filled from an *uncorroborated* later read.** A fill that takes the digest of whatever
  `.ots` is on disk and records it as what Cairn placed would launder an already-swapped proof into
  "recorded" and permanently disable the detection. So no read path may write it: not `/verify`, not
  the proof download, not `export`, not a scan. *(This is the deliberate deviation from the brief's
  "verify can opportunistically fill it".)*
- **One exception, and only because the file's own bytes vouch for it:** the daily upgrade pass
  fills a NULL `ots_digest` when — and only when — the parsed proof's digest **equals the row's
  recorded `sha256`** (D3). That is the same corroboration adoption uses, so it is not a lazy fill.

**Corroboration is the whole distinction.** `stamp_pending`'s adoption and the upgrade pass's
backfill both record `ots_digest` from an existing proof only when that proof's digest **equals the
row's own recorded `sha256`**. The file's own bytes vouch for the value, so a swapped proof cannot
be laundered into "recorded": it would have to match the recorded baseline, and if it did it would
not be a swap. An uncorroborated fill has nothing vouching for it, and is forbidden everywhere.

**Column shape:** `files.ots_digest TEXT NULL` — a plain SQLite `ADD COLUMN`, no table rebuild, on a
~186k-row table. Same width/type as `files.sha256` (`String(64)`), lower-case hex.

## D3 — Backfill: corroborated, and only inside the daily upgrade pass

Existing `complete`/`incomplete` proofs *could* be parsed for their digest. Two of the three places
to do it are rejected; the third is adopted.

**Rejected — in-migration backfill.** Parses ~186k `.ots` files with the DB held for the duration —
an offline outage taken for a read-side refinement, on a deploy flow (`make deploy` then
`make migrate`) whose migrations are otherwise seconds.

**Rejected — lazy fill from `/verify`.** Unsafe as such (D2: an uncorroborated read-time fill
launders a swapped proof into "recorded"), *and* it turns a `GET` into a writer contending with the
scanner, against the project's single-writer discipline.

**Chosen — a corroborated fill inside the daily upgrade pass.** The upgrade pass is already a
writer, already holds the row, and already touches every `incomplete` proof on disk. For a row whose
`ots_digest` is NULL and whose `.ots` parses, fill `ots_digest` with the proof's committed digest
**only when it equals the row's recorded `sha256`**. Otherwise leave it NULL.

Why this is safe where a read-time fill is not:

- **The recorded baseline is the corroborating witness.** A swapped proof cannot be laundered into
  "recorded", because to be written it would have to commit to exactly the digest Cairn already has
  on file for those bytes — and a proof that commits to the file's own recorded digest is, by
  definition, not a swap. The column's detection property (`ots_digest` vs. the parsed proof
  disagreeing ⇒ this is not the proof Cairn placed) survives intact: every value the column can
  acquire is one the file's own baseline agreed with at the moment it was written.
- **It costs nothing extra.** The pass already walks each `incomplete` proof and already spawns the
  upgrade for it; the digest comes from the same offline library parse Slice A adds for placement
  (D1), on a sub-kilobyte file already in the page cache. No calendar traffic, no extra pass, no new
  query — one local parse per row that has no provenance yet, and none at all once the backlog
  drains.
- **It reaches the rows that actually lack provenance.** The `incomplete` backlog is ~28k rows on
  the homelab host — proofs placed before this change, which would otherwise stay NULL until their
  file happened to change and be re-stamped. They get provenance without anyone touching a file.

**A parsed digest that does *not* match `sha256` is left NULL and logged at `WARNING`.** This is not
an edge case to shrug at: it is exactly the condition the column exists to catch — the stored proof
commits to bytes that are not the bytes Cairn recorded for this file, so the proof is corrupted,
swapped or misfiled. Recording it would destroy the finding; silently skipping it would hide it. The
log line names the file, its recorded `sha256`, the proof's committed digest, and **what to do**: run
`/verify` on that file (panel or `cairn verify`) to get the full attribution. The upgrade itself
proceeds normally — a mismatch is a provenance finding, not a reason to stop upgrading proofs.

Rows still NULL after the pass stay neutral: sprint 1 already specifies exactly what a missing
baseline reads as, so a legacy row loses nothing it had, and rows also gain provenance naturally as
files change and are re-stamped.

## D4 — #21's event kind: a new `restored_changed`, not a reused `modified`

Reusing `modified` is cheaper (no migration) and was rejected on a correctness ground, not a
cosmetic one:

**In `churn` mode a `modified` event does not exist.** The spec is explicit — *"in `churn` mode a
content modification SHALL silently re-baseline the stored hash/size/mtime with no nag event"* — and
`alerting` fires only for `missing` (any mode) or `modified` **in a WORM corpus**. So a wrong
restore into a churn collection would be re-baselined in silence and never notified: the fix for a
false negative would have created a new one, in the mode where the operator has explicitly told
Cairn that ordinary edits are expected. A restore is not an ordinary edit — the file was *absent*,
and something the operator or their backup tool put back does not match what left. That must alarm
in both modes, like `missing` does, and it needs its own kind to say so.

Signal clarity is the secondary argument: `restored_changed` names the sequence (*it went away and
what came back is not what left*), which is what the operator has to act on; a bare `modified`
loses the "you restored the wrong thing" reading entirely, and would put an unacknowledged
`modified` event on a churn collection where every other `modified` event is impossible.

**Cost is known and acceptable.** `events.kind`'s CHECK cannot be altered in place on SQLite, so
migration `0011` rebuilds `events` in batch mode — precisely what **`0005_rename_detection`** did to
add `moved`, on the same table, and it was unremarkable. A rebuild is a single-pass table copy; at a
few hundred thousand rows it is seconds, and it is a one-shot cost taken during `make migrate`.

**#12's rejected fix 3 does not apply.** That rejection is about a new **`files.status`** value
(`'gone'`): a `ck_files_status` rebuild of a ~186k-row table, `uq_files_collection_relpath` then
blocking re-add at that path, and the missing-sweep re-alarming the row every scan. None of those
mechanisms touch `events.kind`, which has no uniqueness constraint, no sweep, and one CHECK. New
**event** kinds are the established way to add a story here (`moved` did it).

**No new `runs` column.** A restored-changed file is counted in `runs.modified` (its status *is*
`modified`); the event kind carries the distinction and `events` is where the per-file story lives.
Adding `runs.restored_changed` would widen the migration for a number no surface reads.

## D5 — the restore-ack composes: a changed restore still closes its `missing` alerts

Sprint 1 (integrity-scanning, *"A restored file closes its own open missing alerts"*) system-acks a
reappeared file's open `missing` events inside the same committed transaction as the restore. The
question is whether a file that came back **different** should keep its `missing` alert open.

**It should not.** The proposition that alert asserts is *"this file is absent"*, and that
proposition is now false: something is at that path. Keeping it open would:

- leave a nag that nothing in the product can clear — the exact defect (#22) sprint 1 shipped this
  requirement to fix, and it would be re-introduced only for the more dangerous case;
- double-count one incident as two open alerts on one file, with no way for the operator to tell
  which of the two is the one that matters;
- add nothing: the alarm rides on the **unacknowledged `restored_changed` event**, which is strictly
  more informative than the `missing` event it replaces — it says the file is back *and* that what
  came back is wrong, and it carries both digests.

So the branch that closes `missing` alerts is keyed on **"this file reappeared"**, not on "this file
is healthy". Concretely, the ids collected for the batched ack (`restored_ids`, now read as
*reappeared* ids) include restored-changed rows. Both invariants sprint 1 spec'd are preserved
verbatim: the ack is scoped to `kind='missing'` (#12's rejected fix 7 — an open WORM `modified`
event on the same file survives), and it is applied **before** the commit that persists the
reappearance, so no commit can leave a file recorded as present with the alert its absence raised
still open. The spec sentence generalizes from "recorded healthy" to "recorded as present"; the
mechanism does not change.

## D6 — precise ordering inside the scan

The compare happens **in the walk loop**, in the `row.status == "missing"` branch, before any
mutation of the row and therefore before any `_drain` batch:

1. `sha = await _hash(full)` — the file is hashed regardless (this branch always hashed).
2. `prior = row.sha256` is captured **before** any assignment. This is the whole fix: today's first
   statement destroys it.
3. `row.size`, `row.mtime`, `row.last_checked`, `row.last_changed`, and `row.sha256 = sha` are
   updated in every outcome. The index must keep describing the bytes on disk now — sprint 1's D1
   attribution depends on `files.sha256` being the last-seen digest — so the fix is *compare before
   overwrite*, not *stop overwriting*.
4. Branch on the comparison:
   - `prior is None` → `ok` + `restored` (born acked), `detail` noting no recorded digest was
     available. Nothing was established; nothing is alarmed. (Mirrors "no baseline ⇒ blame neither".)
   - `sha == prior` → unchanged from today: `ok`, `restored` born acked, `summary.restored += 1`,
     **`ots_state` untouched** (the stored proof still commits to these bytes — re-stamping here is
     the pointless work #15's adoption path also avoids).
   - `sha != prior` → `status='modified'` in **both** modes; one `restored_changed` event with
     `acknowledged_at=None` and `detail` carrying **both digests in full**; `summary.modified += 1`;
     `_record_alarm("restored_changed", relpath)`; `if perfile: row.ots_state='pending'`.
5. The row id is appended to the reappeared-ids list in **every** outcome (D5).

Relative to the rest of the pass — each of these is a *non*-interaction that must stay true:

- **`_drain` batching:** unchanged. The comparison is complete before the row is added to the batch,
  so the same commit persists the classification, the event, and the `missing` ack (D5).
- **Missing sweep:** a reappeared path is in `seen`, so it is never a `newly_missing` candidate.
- **`_reconcile_moves`:** operates on rows *created this scan* (`new_rows`) × `newly_missing`. A
  restored row is neither — it is a pre-existing row and it is not newly missing — so a
  restored-changed file can never be swallowed as a move. Worth an explicit test: a rename that
  happens to coincide with a wrong restore must not silently reconcile the alarm away.
- **Auto-baseline:** promotes only rows whose status is still `new`. A restored-changed row is
  `modified`, so the deep pass can never quietly clear it. Also worth a test — auto-baseline is on
  for the Photos collection in production.
- **Stamp pass:** runs post-commit; the `pending` restored-changed row is stamped there, and #15's
  preservation is what keeps the original bytes' proof. This is the interlock; test the pair
  together, not only apart.
- **Alert dispatch:** post-commit, driven by `summary.alarming`, and unchanged apart from the new
  kind entering that list in both modes.

## D7 — verify blame with provenance

`VerifyResult` gains `proof_digest: str | None`, set by `_verify_via_explorer` (the only backend
that can establish a digest disagreement at all — `_verify_via_cli` has no mismatch site, sprint-1
D1). The blame ladder for `digest_mismatch` becomes, in both `verify_run` and `_cmd_verify`:

| condition | blame | reading |
|---|---|---|
| no recorded baseline (`files.sha256` empty) | `unknown` | unchanged |
| live ≠ recorded baseline | `file` | unchanged |
| live == baseline, `ots_digest` NULL | `proof-stale` if a re-stamp is owed, else `proof` (undecidable wording) | **unchanged** — sprint 1's heuristic, for legacy rows |
| live == baseline, `ots_digest` known, `proof_digest` known and ≠ `ots_digest` | `proof` | the `.ots` at this path is **not the proof Cairn placed** — corrupted, swapped or misfiled. Established. |
| live == baseline, `ots_digest` == live | `proof` | Cairn recorded placing a proof for **these** bytes, and the proof disagrees ⇒ same conclusion, established without needing `proof_digest` |
| live == baseline, `ots_digest` ≠ live | `proof-stale` | Cairn recorded placing a proof for **earlier** bytes ⇒ the proof predates this version. Established, no `pending`/status heuristic needed |

Two copy consequences:

- The `proof` verdict's wording must split. With NULL provenance it stays sprint 1's *"Cairn cannot
  tell which"*; with provenance it becomes a positive finding about the proof — and it must still
  not claim anything about the **file**, whose bytes match their baseline.
- The `proof-stale` verdict's *"a re-stamp is pending"* clause is only true when one is. With
  provenance the stale case can be established while nothing is queued (e.g. a collection switched
  from `perfile` to `none` after a modification, so the scan never set `pending`). The copy states
  the staleness always and the pending re-stamp only when the row says so.

`proof_digest` is displayed nowhere new; it exists to make the attribution provable.

## D8 — known limitation this change *exposes* (filed as #39, not fixed here)

`_reconcile_moves` repoints a moved row's `relpath` but leaves `files.ots_path` pointing at the
**old** relpath's canonical proof (deliberate: the proof is valid, and rewriting proof paths on
every rename is its own design). If a *different* file later appears at that old relpath and is
stamped, its proof takes that canonical path.

- **Today:** the moved file's proof is silently `os.replace`d out of existence, and its `ots_path`
  now resolves to another file's proof. Both evidence and pointer are wrong, invisibly.
- **After this change:** the displaced proof is preserved in the archive, and the moved row's
  `ots_digest` no longer matches the `.ots` at its `ots_path`, so `/verify` reports an established
  **`proof`** blame ("this is not the proof Cairn placed") instead of green.

The pointer is still wrong. Fixing it means either relocating the proof with the file or making
`ots_path` derived rather than stored. Out of scope; filed as **[#39](../../../issues/39)**, which
references this section.

## D9 — file ownership for the parallel slices

Two implementation slices plus a shared prep step. The shared schema work (migration `0011` +
`src/models/db.py`) is done **once on the base branch before fan-out**, not assigned to a slice:
both slices need the column/CHECK, worktrees branch from the committed base, and two worktrees must
never create the same migration file (the numbered-migration collision gotcha).

| Path | Owner |
|---|---|
| `alembic/versions/0011_*.py`, `src/models/db.py` | shared prep (pre-fan-out) |
| `src/services/ots.py`, `src/services/proofs.py` | **Slice A** |
| `src/control_panel/routes.py` → **`verify_run` only** | **Slice A** |
| `src/cli.py` → **`_cmd_verify` only** | **Slice A** |
| `templates/partials/verify_result.html` | **Slice A** |
| `src/services/scanner.py` | **Slice B** |
| `src/control_panel/routes.py` → **`_event_view` only** | **Slice B** |
| `templates/collection_review.html`, `templates/partials/_event_row.html` | **Slice B** |
| new `tests/test_proof_preservation.py` | **Slice A** |
| new `tests/test_restored_changed.py` | **Slice B** |

`routes.py` is shared **by function**, as in sprint 1. Neither slice edits the other's function, and
neither reformats the file.
