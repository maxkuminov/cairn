# Guard the two paths where Cairn can silently lose the truth: proof overwrite and blind restore

## Why

GitHub **#12** (the Aug-2026 audit's meta issue) opens with the finding that survived every
adversarial pass:

> **No OpenTimestamps proof file is ever deleted by any code path in this app.** … The two places
> where real, silent, irreversible loss is possible are **#15** (proof overwrite) and **#21**
> (restored-file digest).

Sprint 1 fixed the panel's *description* of the evidence. This change fixes the two places where
the evidence itself, or the claim it backs, is destroyed. Both are false-negative machines in a
product whose entire proposition is *these bytes are the ones you had, and they existed by this
date*. A false negative here does not annoy an operator — it voids the evidentiary value of a
proof while the panel stays green.

### Failure narrative 1 — a multi-year Bitcoin anchor overwritten by a proof made today (#15)

`ots._place_proof` ends in `os.replace(staged_ots, out_ots_path)` with **no existence check**. The
proof store is keyed by path — `<proof_store>/<collection_id>/<relpath>.ots` — so any second stamp
of the same relpath lands on top of the first, whatever it attests and however old it is.

It is reachable through the product's own recommended workflow, with no misuse:

1. A volume is unmounted (or a folder is moved out) and a scan marks a tax document `missing`.
2. The operator, following the review page, presses **Accept all changes**. `accept_collection`
   *deletes* the `files` row (#12 R2) — the row that held `ots_path`, `sha256`, `first_seen`.
3. The volume comes back. The file is at its old path again.
4. The next scan finds no row for that path, so it is `added` — status `new`, `ots_state='pending'`.
5. `stamp_pending` stamps it and `_place_proof` **replaces** the 2023 `.ots`, whose Bitcoin
   attestation proved the document existed three years ago, with one submitted this morning. The
   old proof's bytes are gone from the disk. The DB now records a stamp time of today.

The operator's evidence for "this contract existed before the dispute" silently became "this
contract existed since Tuesday". At scale, one mass-accept after an unmounted volume does this to
every file in the collection.

The same overwrite fires from `cairn stamp --all` after any path that reset a row to
`ots_state='none'`, and from a re-stamp of a modified file, where the old proof is the *only*
remaining evidence for the previous version of those bytes.

### Failure narrative 2 — a wrong restore adopted as "restored / OK" (#21)

`scanner.py`'s restore branch, on a file whose row says `missing`:

```python
row.sha256 = await _hash(full)   # overwrite FIRST
row.status = "ok"                # …then declare it fine
```

The recorded digest — the only record of what that file used to be — is overwritten by the fresh
hash **before anything compares them**. Whatever bytes appear at that path are adopted:

1. A file goes missing; Cairn alerts; the operator restores from backup.
2. They pick the wrong snapshot, the backup is truncated, or an attacker puts a different file at
   the path while the real one is known-absent.
3. The scan sets `status='ok'`, writes an informational `restored` event **born acknowledged**, and
   (sprint 1) system-acknowledges the open `missing` alert.
4. Every red count clears. The dashboard reads **All clear**. `files.sha256` now describes the
   attacker's bytes, and the stored `.ots` attests a digest nothing in the product will ever compare
   against again.

The review page currently tells the operator this check happens — *"recovered files flip back to
restored / OK"* and *"Scan now … to confirm"* — copy that describes a comparison the scan does not
perform.

### Why both, in one change

They interlock, in both directions:

- The fix for #21 sets `ots_state='pending'` on a file that came back with different bytes. Without
  #15's guard, that re-stamp would land on the canonical path and **destroy the proof for the
  original bytes** — the one artifact that could later show what the file used to be. Shipping #21
  alone converts a detection bug into an evidence-destruction bug.
- The fix for #15 needs to know which digest each stored proof commits to, which is also the
  provenance record the sprint-1 design deferred to this change (below).

### The IOU from sprint 1 (design D1)

Sprint 1 made `/verify` and `cairn verify` attribute a digest disagreement instead of blaming the
file for it, and recorded an accepted limitation:

> Deciding it properly requires **recording each proof's own digest beside it**, written atomically
> with `ots_path`/`ots_state`, with legacy rows staying neutral; that is the proof-versioning work
> (**GitHub #15**) and lands with the next change.

Today the tiebreaker is `files.sha256`, which is the digest of the bytes the **last scan** saw, not
the digest the **stored proof** was made from — so "the proof is corrupt" and "the proof predates
this version" are indistinguishable and both are reported as undecidable. The proof-preservation
work has to record each placed proof's digest anyway; recording it once settles the IOU, and the
verify blame attribution becomes provable rather than heuristic.

DESIGN.md references: **§6 "OpenTimestamps handling"** (proof lifecycle and the per-file store),
**§5** (per-run scan flow, the `files` shape), **§3 "Locked decisions"** (watched folders read-only,
the DB is an index — the guarantee is bytes + `.ots`).

## What Changes

### A stamp never destroys an existing proof (#15)

- **`_place_proof` stops being an unconditional `os.replace`.** When the canonical output path is
  already occupied it reads the existing proof's committed digest and attestation state (offline,
  via the OpenTimestamps library — the same parse `_verify_via_explorer` already does) and decides:

  | existing proof at the canonical path | outcome |
  |---|---|
  | commits to the **same** digest and is **complete** (Bitcoin-anchored) | the existing proof is **kept canonical**; the freshly staged proof is discarded. An anchored proof is never demoted for a same-bytes re-stamp — this is #15's headline case. |
  | commits to the **same** digest and is **incomplete** | the existing proof is archived; the new one is placed. A never-anchored proof can still be refreshed, and nothing is lost. |
  | commits to a **different** digest | the existing proof is archived under that digest; the new one is placed. |
  | **unreadable** | archived under an opaque name; the new one is placed. |

- **Superseded proofs are preserved, never deleted**, in a content-addressed archive alongside the
  canonical tree: `<proof_store>/.superseded/<collection_id>/<dd>/<digest>.ots` (`.superseded`
  cannot collide with a collection directory — those are integers — exactly as `.staging` does not).
  The name is a fixed-length digest, so no watched filename can push the archive path past
  `NAME_MAX` and re-open the class of failure `tolerate-unstampable-proof-paths` closed.
- **The canonical path keeps serving the current digest's proof**, so `/verify`, "Download .ots
  proof", `cairn verify`, `cairn export` and `upgrade` are unchanged — they read `files.ots_path`
  and find exactly what they find today.
- **Archive-then-place ordering, and preservation failure is transient.** The archive move and the
  placement are two `os.replace` calls on one filesystem: each atomic, the pair not. Archiving runs
  first, so no interruption can destroy a proof before it is preserved, and a failure to preserve
  **refuses the placement** (a transient `OtsError` → the file stays `pending` for retry) rather
  than proceeding over an unpreserved proof.
- **`stamp_pending` stops paying a calendar round-trip for a proof it already has**: a pending file
  whose canonical proof already commits to its recorded digest is *adopted* (state and provenance
  recorded from the existing proof) before anything is submitted. The `_place_proof` guard remains
  the authoritative backstop.

### Each stored proof's committed digest is recorded (#15 + sprint-1 D1)

- **New `files.ots_digest` column** (nullable TEXT, additive migration): the digest the proof Cairn
  placed at `ots_path` commits to, written **atomically with `ots_path`/`ots_state`** in the same
  transaction, and cleared whenever `ots_path` is cleared.
- **Written only where the file's own bytes corroborate it** — at placement, at adoption, or in the
  one backfill below. It is never inferred from whatever `.ots` happens to be on disk: an
  uncorroborated fill would launder an already-swapped proof into "recorded" and destroy the very
  detection this column exists for. No read path (`/verify`, proof download, export, a scan) writes
  it.
- **The daily upgrade pass backfills it, corroborated.** For a row whose `ots_digest` is NULL and
  whose stored proof parses, the pass writes the proof's committed digest **only when it equals the
  row's recorded `sha256`**. The baseline is the witness: a swapped proof cannot be laundered,
  because to be written it would have to commit to the digest Cairn already recorded for those
  bytes. This gives the ~28k-row `incomplete` backlog provenance at no extra IO — the pass already
  opens every one of those proofs — while preserving the column's detection property exactly.
- **A parsed digest that does *not* match `sha256` stays NULL and is logged at `WARNING`**, naming
  both digests and telling the operator to run `/verify` on that file. That mismatch is precisely
  the corrupted/swapped/misfiled proof the column exists to catch; recording it would erase the
  finding.
- **NULL means legacy**, and legacy stays exactly as neutral as sprint 1 left it.

### Verify blame becomes provable where provenance exists (sprint-1 D1 IOU)

- `VerifyResult` gains `proof_digest` (the digest the parsed `.ots` commits to; explorer backend
  only, which is the only backend that establishes a digest disagreement at all).
- `verify_run` (panel) and `_cmd_verify` (CLI) upgrade the `mismatch_blame` tiebreaker: with
  `ots_digest` recorded, *live == recorded baseline* no longer collapses to "Cairn cannot tell".
  - `ots_digest` **equals** the live digest ⇒ Cairn recorded placing a proof for exactly these
    bytes and the `.ots` at that path commits to something else ⇒ **the proof is corrupted, swapped
    or misfiled** — now an established finding, not a guess.
  - `ots_digest` **differs** from the live digest ⇒ the recorded proof was made from earlier bytes
    ⇒ **the proof predates this version**, established without the `pending`/`modified`/`new`
    heuristic (which stays as the fallback for NULL provenance, and whose "a re-stamp is pending"
    wording is only used when one actually is).
- Legacy (`ots_digest IS NULL`) rows keep sprint 1's wording and its explicit undecidability.

### A file that comes back with different bytes is not "restored" (#21)

- **The restore branch hashes first and compares before overwriting.** The recorded digest is
  captured, the file is hashed, and classification is decided from the comparison:
  - **identical** → today's behaviour exactly: `ok`, an informational `restored` event born
    acknowledged, no re-stamp;
  - **different** → status `modified` **in both modes**, a new **`restored_changed`** event left
    **unacknowledged** (it alarms), `events.detail` recording **both digests in full**, and — for
    `perfile` collections — `ots_state='pending'` so the new bytes get their own proof while #15
    preserves the old one;
  - **no recorded digest** (legacy row) → today's `restored`, with the absence noted in `detail`.
    Nothing is established, so nothing is alarmed.
- **New event kind `restored_changed`** (CHECK-constraint migration). Reusing `modified` was
  rejected: in a **churn** collection a `modified` event does not nag at all, so a wrong restore
  would be silently re-baselined — a brand-new false negative created by the fix (see design D4).
- **It alarms in both modes and it notifies.** `restored_changed` joins `missing` in the
  any-mode alarm set, so the batched email/webhook fires for it.
- **It still closes the file's own `missing` alerts.** "This file is no longer absent" is true
  whatever came back; the alarm rides on the unacknowledged `restored_changed` event, which says
  more. Sprint-1's `kind='missing'` scoping and same-transaction invariant are preserved (design D5).
- **Review-page copy is corrected** to describe the comparison that now happens and both of its
  outcomes, and the event feed renders the new kind.

## Non-goals

- **No retroactive repair.** Files wrongly adopted as `restored` by past scans are not re-examined;
  their recorded digest is already the wrong one and Cairn has nothing to compare against. The next
  deep pass treats them as the baseline they now are.
- **No in-migration backfill of `ots_digest`, and no read-time fill.** Parsing ~186k `.ots` files
  inside a migration is an offline outage for a read-side refinement, and a lazy fill from `/verify`
  is both unsafe (it would record an uncorroborated digest) and a `GET` turned writer against the
  single-writer discipline. The corroborated fill in the daily upgrade pass is the only backfill
  (design D3); rows it cannot corroborate stay NULL, which reads exactly as neutrally as sprint 1
  already ships.
- **No proof GC, pruning, retention policy or panel surface for the `.superseded` archive.** It
  grows monotonically; it is small (a `.ots` is well under a kilobyte) and deleting evidence is the
  thing this change exists to prevent. Discoverability is a documented layout plus a `WARNING` log
  line naming both paths.
- **No per-file accept (#30), no `accept` scoping or vocabulary work (#16), no fleet-wide review
  page (#27).** The accept→restore→re-stamp route is the *reachability* story for #15, not its fix;
  making accept safer is sprint-2 work under its own issues.
- **No `status='gone'`** — #12's rejected fix 3, explicitly. Nothing here adds a `files.status`
  value. (The new `restored_changed` is an **event kind**, which that rejection does not cover.)
- **No blanket event acknowledgement** — #12's rejected fix 7. The restore ack keeps its
  `kind='missing'` scoping.
- **No move-aware proof relocation.** `_reconcile_moves` leaves `ots_path` pointing at the old
  relpath's canonical proof; a later file appearing at that old path and being stamped therefore
  archives a proof another row still points at. Today that is a *silent overwrite*; after this
  change the proof survives and `ots_digest` makes the misfiling **detectable** by verify. Fixing
  the pointer is its own change — see design D8, filed as [#39](../../../issues/39).
- **No new CLI commands or config keys.**

## Issue index

| Issue | Audit ref | Covered here |
|---|---|---|
| [#15](../../../issues/15) | A3 | proof preservation, content-addressed archive, `files.ots_digest` |
| [#21](../../../issues/21) | A9 | hash-before-compare restore, `restored_changed`, review copy |
| [#12](../../../issues/12) | meta | root causes R2/R4, rejected fixes 3 and 7 honoured |
| sprint-1 design **D1** | — | the deferred per-proof provenance and the provable verify blame |
