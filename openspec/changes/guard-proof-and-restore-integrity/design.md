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
| digest == staged, existing **complete**, anchor **not disproven** | **keep existing; discard staged** | #15's headline case. A fresh stamp is always `incomplete`; replacing a Bitcoin-anchored proof with a same-bytes pending one is a strict downgrade of the claim, which is exactly the loss the issue describes |
| digest == staged, existing **complete**, anchor **disproven by the caller** | archive existing; place staged | a "complete" proof is only *syntactically* anchored here — `_place_proof` reads the file, it does not check the chain. Keeping it would let a fabricated attestation hold the canonical path and discard the real proof produced seconds earlier: D1a's rule relocated into placement. The caller (`stamp_pending`) already knows when a backend answered "this anchor does not confirm"; it passes that verdict down |
| digest == staged, existing **incomplete** | archive existing; place staged | a proof the calendars never anchored can legitimately be refreshed — the `stale_incomplete` requirement exists so a never-confirmed proof *can* be re-stamped. Nothing is lost: the old one is archived |

The digest and the completeness both come from **one** offline library parse of the existing `.ots`
(`DetachedTimestampFile.deserialize` + scanning for a `BitcoinBlockHeaderAttestation`), the same
parse `_verify_via_explorer` already performs. No subprocess: `ots info` would cost a process spawn
per occupied path.

**"Re-stamping the same digest stays cheap" is satisfied, but adoption is not a bare digest match.**
The common shape of #15 (accept → restore identical bytes → rescan → re-stamp) can skip the calendar
entirely: `stamp_pending` may adopt a pending file's existing canonical proof before any staging
symlink or `ots stamp` invocation. For an ordinary pending set (brand-new files) the check is one
negative `stat` per file; only an occupied path costs the small local parse.

**A same-digest match is necessary but not sufficient to adopt.** Adopting means recording a proof
Cairn did not just place, and recording `ots_digest` — provenance — from it. Three conditions must
all hold (see D1a); otherwise the file takes the ordinary stamp path, where the four-way rule above
preserves whatever is on disk and a fresh proof is placed. Nothing is lost by declining to adopt:
declining costs one calendar round-trip, while adopting wrongly records provenance for a proof
nothing vouched for.

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

**Archive collision: the archive never discards and never overwrites.** An earlier revision of this
design discarded the incoming duplicate when `<digest>.ots` was already taken, reasoning that two
proofs for one digest attest the same fact. That reasoning is wrong, and the counterexample is
ordinary: digest D is first archived while `incomplete`; the proof left canonical for D is later
upgraded and acquires a Bitcoin attestation; a subsequent content change displaces *that* proof, and
the collision would then throw away the anchored one to keep an unanchored one. Proofs for a digest
are not interchangeable in evidentiary strength, and **comparing their strength is a judgement the
archive must not make** — an archive that reasons about what is worth keeping is an archive that
deletes evidence.

So on collision the incoming proof is placed under a **monotonically suffixed** name in the same
shard: `<digest>.ots`, then `<digest>.1.ots`, `<digest>.2.ots`, … — the first index not already
taken. Nothing in the archive is ever replaced or removed. The suffix is bounded-length digits, so
the fixed-length-name property (no watched filename can influence the archive path) is untouched.

The archive write is therefore an **exclusive create**: it must fail rather than replace an existing
name, so `os.replace` is not usable for it (it silently overwrites). Link-then-unlink (`os.link` to
the chosen name — `EEXIST` means "try the next index" — then `os.unlink` the source) is atomic per
name and stays within the proof-store volume, exactly as `os.replace` does. Scanning for a free
index is a check-then-act sequence, and D10 is what makes it safe: the whole
inspect→archive→place→record sequence runs under the collection's single-operation claim, and the
archive path is already scoped by `<collection_id>`, so no two writers can ever be choosing an index
in the same shard directory. The exclusive create is the belt to that braces — a lost race costs an
extra index, never a lost proof.

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
  `ots_digest = <staged digest>`. A real submission happened, so `now` is the truth.
- **kept existing** → `ots_path = out`, `ots_state = 'complete'`, `ots_digest = <existing digest>`,
  and **`ots_stamped_at` is left as it is** — NULL on a row re-created by accept→restore.
  *(Flagged for Max.)* The alternative, stamping it with `now`, would print "notarized today" over a
  three-year-old anchor: the same class of lie as #12's rejected fix 6 (labelling a download
  "existed by `<ots_stamped_at>`"). The proof's own attestation, which `/verify` reads, carries the
  real date; an unknown submission time is the honest record. The one cost is that
  `stale_incomplete` filters on `ots_stamped_at IS NOT NULL`, so a row recorded this way is never
  flagged stale — harmless, because this branch only ever records a `complete` proof, and
  `stale_incomplete` only reports `incomplete` ones. **No branch of placement or adoption can leave a
  row `incomplete` with a NULL `ots_stamped_at`** (D1a), so nothing can fall out of the stuck-proof
  report through this door.

### D1a — when an existing proof may be adopted instead of submitted

Adoption records a proof Cairn did not place *and* writes provenance from it. A bare same-digest
match is not enough to justify either: an attacker who can write to the proof store can leave a
syntactically well-formed `.ots` committing to the file's real digest and carrying only a
`PendingAttestation` (or a fabricated Bitcoin attestation), and a bare-match rule would promote the
row out of `pending`, record that forgery as the file's proof, and stamp it with recorded
provenance. The rule is therefore:

`_place_proof` is offline by construction — it parses a local file and never touches the network — so
"complete" there means *carries a `BitcoinBlockHeaderAttestation`*, not *anchored to the real chain*.
That is the right primitive for a placement backstop, but it means the keep-existing branch must be
told when the caller has already **disproven** the existing proof's anchor, or the adoption rule
below is trivially bypassed by writing the forgery to the canonical path and letting placement keep
it. So `_place_proof` takes the caller's verdict as an input; absent a verdict (the ordinary case,
where nothing was checked) it behaves exactly as the table says.

**Adopt without submitting if and only if all three hold:**

1. the canonical proof **parses**; and
2. it commits to the **digest recorded for the file** (`entry.sha256`) — the file's own bytes
   corroborate the value being written to `ots_digest`; and
3. **either** the row already records that same digest as its `ots_digest` — Cairn itself recorded
   placing this proof for these bytes, so the provenance is its own — **or** the proof's Bitcoin
   anchor **verifies** against the configured verification backend at adoption time, which is the
   same check `/verify` performs and the only evidence available for a proof Cairn has no record of.

Otherwise the file is **not** adopted: it takes the ordinary stamp path, where the four-way rule
archives whatever is at the canonical path and places the newly produced proof.

Consequences, each deliberate:

- **An `incomplete` same-digest proof is never adopted.** It has no anchor to verify (condition 3's
  second limb is unavailable), and if the row already had provenance for it, adoption would freeze a
  never-anchored proof in place with no submission behind it. The four-way rule's "same digest,
  existing incomplete → archive and place" branch is exactly the refresh `stale_incomplete` exists to
  make possible, and adoption must not defeat it. This is what closes the "adopted, `incomplete`,
  `ots_stamped_at` NULL, therefore invisible to `stale_incomplete` forever" hole.
- **An unverifiable same-digest proof is never adopted** — forged, corrupt in its attestation, or
  simply anchored to a block the backend disagrees with. It archives and re-stamps, and the
  disproven verdict is carried into `_place_proof` so its keep-existing branch cannot resurrect the
  proof adoption just rejected.
- **An unreachable backend degrades to re-stamping, not to adopting.** If the explorer/node cannot
  be reached, condition 3 is not satisfied, so the file is stamped normally. Failing *open* here
  (adopt on an unreachable backend) would make provenance forgeable by anyone who can take the
  backend offline. Nothing is *disproven* in this case, so no verdict is passed down and placement's
  ordinary keep-existing branch applies: the anchored proof stays canonical, the staged proof is
  discarded, and the cost of the outage is one wasted calendar round-trip — never a demotion of the
  claim and never a lost proof.

**`ots_stamped_at` per branch, explicitly:**

| branch | `ots_state` | `ots_digest` | `ots_stamped_at` |
|---|---|---|---|
| adopted via the row's own recorded provenance | from the parsed proof (`complete`, since an `incomplete` proof is never adopted) | the parsed digest (equal to what was already recorded) | **unchanged** — no submission happened |
| adopted via a verified anchor | `complete` | the parsed digest | **unchanged** — NULL stays NULL. The proof's own attestation carries the real date; `now` would assert a submission Cairn did not make |
| placed (staged proof written) | `incomplete` | the staged digest | `now` |
| kept existing (same digest, anchored, at placement time) | `complete` | the existing proof's digest | **unchanged** |

The adoption log line names the file, the digest, and — for the verified-anchor branch — the block
the anchor was confirmed against, so an adoption is auditable after the fact.

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

### D4a — downgrading past `0011` refuses rather than losing audit rows

`0011` widens `events.kind`'s CHECK by rebuilding the table (as `0005` did for `moved`). The
`downgrade()` is not symmetric with that, and pretending it is would destroy data. SQLite's batch
rebuild copies every row into a table carrying the **old** CHECK, so once a single
`restored_changed` event exists the copy fails — and the three ways to make it succeed are all
unacceptable:

- **delete the rows** — deletes the audit record of the most dangerous incident the product detects;
- **map them to `modified`** — rewrites history into the kind D4 rejected precisely because it means
  something else, and in a `churn` collection means nothing at all;
- **drop the CHECK on the way down** — leaves a schema the older code does not expect, and the older
  code's rendering has no case for the kind, so the row shows as an unlabelled event.

So the downgrade **refuses**: if any `events` row has `kind='restored_changed'`, `downgrade()`
raises with a message naming the count and what the operator must decide (export or re-classify
those events deliberately, then retry). A refusing downgrade is a correct downgrade here — the
migration cannot reverse itself without a judgement call about evidence, so it hands the judgement
back rather than making it silently. With no such rows the downgrade proceeds normally, so the
common case (a deploy rolled back before anything detected a changed restore) is unaffected.

`files.ots_digest` has no such problem: dropping a nullable column loses only provenance that the
older code cannot read anyway, and `0011`'s downgrade drops it unconditionally.

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

The ladder is **ordered**, and the order is the correctness content: the "not the proof Cairn
recorded placing" branch is evaluated **first**, before any staleness reading. Evaluating staleness
first is the A/B/C false reassurance the round-1 audit found — recorded provenance `A`, live and
baseline `B`, and an on-disk proof committing to `C`. `ots_digest` (`A`) ≠ live (`B`) is true, so a
staleness-first ladder reports "this is simply an older proof of your file", when in fact the `.ots`
at that path is neither the recorded proof nor this file's proof. Staleness may only be concluded
once the on-disk proof has been shown to **be** the recorded proof.

| # | condition (evaluated in order) | blame | reading |
|---|---|---|---|
| 1 | no recorded baseline (`files.sha256` empty) | `unknown` | unchanged |
| 2 | live ≠ recorded baseline | `file` | unchanged |
| 3 | live == baseline, `ots_digest` known, `proof_digest` known and ≠ `ots_digest` | `proof` | the `.ots` at this path is **not the proof Cairn recorded placing** — corrupted, swapped or misfiled. Established. **This is the first provenance branch.** |
| 4 | live == baseline, `ots_digest` == live | `proof` | Cairn recorded placing a proof for **these** bytes and the stored proof disagrees with them ⇒ the same conclusion, reachable without a parsed `proof_digest` (a digest mismatch already establishes proof ≠ live == `ots_digest`) |
| 5 | live == baseline, `ots_digest` known and ≠ live, `proof_digest` known and **==** `ots_digest` | `proof-stale` | the on-disk proof **is** the proof Cairn recorded placing, and it was made from earlier bytes ⇒ it predates this version. Established, no `pending`/status heuristic needed |
| 6 | live == baseline, `ots_digest` known and ≠ live, `proof_digest` **unknown** | fall through to row 7 | nothing was parsed, so neither "swapped" nor "stale" is established; asserting staleness here is the A/B/C error with the evidence merely absent instead of contradictory |
| 7 | live == baseline, `ots_digest` NULL | `proof-stale` if a re-stamp is owed, else `proof` (undecidable wording) | **unchanged** — sprint 1's heuristic, for legacy rows |

Row 6 is reachable only defensively: a `digest_mismatch` is set exclusively by
`_verify_via_explorer`, which parses the proof to detect it, so `proof_digest` is populated wherever
a disagreement exists (sprint-1 D1: the node backend has no mismatch site). It is spelled out anyway
because the fallback must be the *undecidable* wording, never the staleness wording — a future
backend that reports a mismatch without a parsed digest must not silently inherit row 5.

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

**What this costs, stated precisely, because a spec must not assert an invariant it knows to be
false.** "Every consumer finds the current file's proof through its recorded proof path" is true for
every row whose `ots_path` still corresponds to its own `relpath` — which is every row that has not
been through `_reconcile_moves` and had another file stamped onto its old path. For a row that
*has*, the archive holds its proof and **no product surface can reach it**:

| consumer | behaviour for such a row until #39 lands |
|---|---|
| `/verify`, `cairn verify` | reads the other file's proof; now reports an established `proof` blame (D7 row 3) instead of silently passing |
| "Download .ots proof" | serves the other file's proof |
| `cairn export` / `export_bundle` | bundles the other file's proof beside this file's bytes |
| the daily upgrade pass | upgrades the other file's proof, not this one's |

Recovering the row's own proof is a **manual** operation: locate
`.superseded/<collection_id>/<dd>/<digest>.ots` by the digest and re-point or copy it by hand. That
is a real limitation, accepted here, and the spec's canonical-consumer scenario is qualified to
exclude these rows rather than asserting an invariant they violate.

## D10 — proof mutation is single-writer per collection, by claim, not by lock

The four-way placement rule is *check-then-act*: inspect the canonical path, decide, archive, place,
record. Under concurrency that is a lost-update machine — two processes can both find the path
unoccupied and both `os.replace` onto it, and the loser's proof is gone. Locking `_place_proof`
alone does not fix it, because the decision that matters (adopt? archive? place?) is taken outside
the rename and the DB row is written outside it too.

**Cairn already has the right primitive, and it is not a lock.** `collections.active_run` +
`collections.claim_run` implement one in-progress operation per collection, enforced by the partial
unique index `uq_runs_one_running_per_collection` (`collection_id` WHERE `result='running'`). It is
a *database-enforced claim*, so it serializes across processes and across hosts sharing the DB —
which a `threading.Lock`, an `asyncio.Lock` or a per-process flag does not. The panel routes and the
scheduler already claim it. The decision here is: **every production entry point that can mutate a
collection's proofs claims the slot, and the whole inspect→archive→place→record sequence runs inside
that claim.** Single-writer then holds by construction, and `_place_proof` needs no lock of its own.

The audit of the entry points as they stand:

| entry point | claims today? | after this change |
|---|---|---|
| scheduler scan pass | yes — `active_run` pre-check, and `scan_collection`'s `claim_run` is the real claim | unchanged |
| scheduler daily upgrade | yes — `claim_run` on a `kind='upgrade'` run | unchanged |
| panel "Scan now" / "Stamp all" | yes — `active_run` guard + `scan_collection` / `run_stamp_backfill`'s `claim_run` | unchanged |
| `cairn scan` | yes — via `scan_collection`; a lost claim already returns `result='skipped'` | the CLI must **say so**; today the refusal prints as an ordinary result line with zero counts |
| `cairn stamp` | **no** — `_cmd_stamp` calls `proofs.mark_unstamped_pending` + `proofs.stamp_pending` directly | must claim a `kind='stamp'` run for the collection, refusing if the slot is held |
| `cairn upgrade` | **no** — `_cmd_upgrade` calls `proofs.upgrade_incomplete(session)` fleet-wide with no run at all | must claim per collection, skipping (and naming) any collection whose slot is held |

`stamp_pending` called from **inside** a scan needs nothing new: `scanner.py:540` runs it while the
scan's own `running` run is still open, so it is already inside a claim. That is the model for the
CLI — the claim wraps the work, it is not taken inside `stamp_pending`, which stays a plain service
function callable under someone else's claim. Putting the claim inside `stamp_pending` would make it
un-callable from the scan that already holds one.

**CLI refusal is refusal, not waiting.** A blocked CLI invocation prints that the collection has an
operation in progress and does not perform it; it never spins or blocks. Waiting would turn a cron
`cairn upgrade` into an unbounded stall behind a multi-hour deep scan, and the work is idempotent —
the next invocation picks it up. Where every collection the command was asked to act on was refused,
it exits non-zero so a cron job's failure is visible; a fleet-wide `cairn upgrade` that processed
some collections and skipped others is a success that names the skips.

**Reusing the run row is a feature, not a side effect.** A `cairn stamp` claim is a real
`kind='stamp'` run, so the panel's operation badge shows the CLI's work, the startup reaper clears
it if the process is killed, and `compute_health` is unaffected (only `kind='scan'` runs feed
freshness). None of that is new machinery — it is what `run_stamp_backfill` already does, called
from a second front door.

**What this does not claim to solve.** The claim serializes Cairn against Cairn. It is not a defence
against a second, unrelated process writing into the proof store, nor against two Cairn deployments
pointed at one proof store with *different* databases — that configuration has no shared claim to
take and is out of scope (the proof store is documented as owned by one deployment). Within one
deployment, which is every supported topology, inspect→archive→place→record is single-writer.

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
| `src/cli.py` → **`_cmd_verify`, `_cmd_stamp`, `_cmd_upgrade`, `_cmd_scan`'s refusal line only** | **Slice A** |
| `templates/partials/verify_result.html` | **Slice A** |
| `src/services/scheduler.py` | **Slice A** |
| `src/services/scanner.py` | **Slice B** |
| `src/control_panel/routes.py` → **`_event_view` only** | **Slice B** |
| `templates/collection_review.html`, `templates/partials/_event_row.html` | **Slice B** |
| new `tests/test_proof_preservation.py` | **Slice A** |
| new `tests/test_restored_changed.py` | **Slice B** |

`routes.py` is shared **by function**, as in sprint 1. Neither slice edits the other's function, and
neither reformats the file. `src/cli.py` is Slice A's outright (Slice B touches no CLI code); the
`_cmd_scan` change is the one refusal line D10 requires, not scanner logic, which stays Slice B's.
`src/services/scheduler.py` is Slice A's, and only for D10's claim audit — the scheduler already
claims correctly, so the expected diff there is zero or a comment.
