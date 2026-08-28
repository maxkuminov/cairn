# Design: relocate-proofs-on-move

## Context

`_reconcile_moves` (`src/services/scanner.py`) reconciles a 1:1 content-matched missing+added
pair into one surviving row: it repoints `relpath`, keeps identity (`first_seen`, `sha256`,
`ots_path`, `ots_state`, `ots_stamped_at`, `ots_digest`), emits one `moved` event. It mutates
only the index — so the proof file stays at the OLD relpath's canonical location
(`proof_path()` = `<proof_store>/<collection_id>/<relpath>.ots`) and `ots_path` keeps naming
it, truthfully. The defect: the old canonical slot is claimable — a later stamp of a different
file at the old relpath displaces the moved file's proof into `.superseded/` (never destroyed,
since guard-proof-and-restore-integrity) and the moved row's pointer then resolves to a
stranger's proof. `files.ots_digest` makes that detectable at verify time — as a proof
mismatch on a perfectly healthy file: a false alarm on the product's core signal.

Round 1 of the spec audit (9 BLOCKERs) established that relocating from inside the scanner
adds a second proof-mutation path with its own crash windows, ordering hazards against the
same scan's stamp pass, and lease questions. This design therefore splits the fix into a
**guard** (immediate, closes the loss path everywhere) and a **relocation sweep** (single code
path, converges pointers), instead of teaching the scanner to move files.

## Goals / Non-Goals

**Goals:**

- No stamp, at any entry point, may ever displace a proof that any row currently records as
  its `ots_path` — the loss path is closed by construction, not by relocation winning a race.
- Moved files' proofs converge to the canonical location for their current relpath — including
  every pre-fix moved row on the live deployment — via one relocation code path, with no
  migration.
- **The pointer invariant**: at every moment, including across a crash at any phase boundary,
  `ots_path` names a location actually holding this row's proof (corroborated, not assumed).
- No branch, on any input, may discard proof bytes.

**Non-Goals:**

- Deriving `ots_path` from `relpath` (cannot represent mid-heal/deferred states; 18 read
  sites).
- Repairing pointers already misfiled before this ships: the sweep must *detect* them (source
  corroboration) and refuse to act, warning toward `.superseded/` recovery — un-misfiling is
  manual, per guard-proof-and-restore-integrity's accepted scope.
- Any change to move-matching rules, to `_reconcile_moves`'s index-only contract, or to
  `_place_proof`'s occupied-canonical-path rules for stamping.

## Decisions

### D1 — The referenced-slot stamp guard (the actual fix for the loss path)

Before anything else touches a canonical slot, the stamp path (batched `stamp_pending`, its
per-file fallback, and the backfill) defers any member whose canonical output path is currently
recorded as `ots_path` by a **different** row in the collection — one bounded query over the
batch's output paths. A deferred member stays `pending` with a warning naming the blocking row;
it is retried on later passes and proceeds once the sweep (D2) has relocated the blocker away.

**Position is load-bearing** (audit round 2a): the guard is the FIRST canonical-slot decision —
evaluated under the proof-store lock after the claim is re-confirmed, and BEFORE the adoption
pass (`_adopt_or_verdict` would otherwise adopt a byte-identical blocker's proof onto the
newcomer, putting two rows on one artifact), before writability classification, before staging
symlinks exist, and before calendar submission. A deferred member therefore never produces a
proof, so the batch teardown has nothing of its to discard. The guard-to-placement window needs
no re-query: a move reconciliation referencing a batch slot can only commit under the
collection's operation claim, i.e. only after the stamp's claim was reclaimed — and the
existing lease fence already refuses ALL placement under a reclaimed claim, whole-batch,
members left `pending`.

This guard alone makes the #39 hazard unreachable from every entry point (scheduler, panel,
CLI): the stranger's stamp waits instead of displacing. It also covers the same-scan case — a
newcomer appearing at a just-vacated old path defers its stamp until the mover's proof has
been relocated by the sweep, rather than racing it. The scanner itself remains free of proof
mutation.

Cost: the newcomer-at-a-moved-path's proof is delayed by up to one healing cycle (a day, or
an operator's `cairn upgrade`). Accepted: a delayed stamp is recoverable; a displaced proof
pointer is a false alarm on the core signal.

### D2 — Relocation lives in ONE place: the healing sweep of the upgrade pass

The daily upgrade pass (and `cairn upgrade`) gains a per-collection stale-pointer sweep: rows
where `ots_path IS NOT NULL` and `ots_path` differs from the canonical path for the current
`relpath`. **Stale-pointer existence is an independent admission reason** for the upgrade
operation — a collection with no `incomplete` proofs (or a tripwire collection carrying
historical proofs) still claims its run slot and sweeps; the sweep's work is counted in the
run's progress totals.

The canonical-location comparison is computed **through `proof_path`** (D6) — candidate rows
may be selected by SQL prefix comparison, but the authoritative staleness test for each
candidate is `Path(row.ots_path) != proof_path(settings, cid, row.relpath)` in Python, so the
SQL expression can never disagree with the helper.

The sweep runs under the collection's operation claim with the standard lease discipline
(heartbeat, in-band reclamation checks) and takes the per-collection proof-store flock for
each relocation, **re-confirming the claim after lock acquisition and holding the flock across
every phase** of D4 (inspect → publish → pointer commit → unlink) — the same fencing the
stamp tail uses.

### D3 — Corroborate the source before believing it (misfiled pointers must not propagate)

Before relocating, parse the proof at the source (`read_proof_facts`):

- **Row has `ots_digest`**: the source proof's committed digest must equal it. A mismatch
  means the pointer is already misfiled (e.g. the old slot was re-stamped by a stranger
  before this change deployed): do NOT relocate, do NOT archive, do NOT touch the row; warn
  naming the row, both digests, and `.superseded/` recovery. The sweep must never launder a
  stranger's proof into the mover's canonical slot.
- **Legacy row (`ots_digest` NULL)**: the source proof's committed digest must equal the
  row's recorded `sha256` (its last-scanned baseline). Equality is the explicit safe legacy
  rule — the proof provably commits to this file's bytes. Anything else (mismatch, unreadable
  source, no digest) → preserve all bytes, touch nothing, warn for operator recovery.
- An unreadable or absent source: touch nothing, warn. (An absent source with `ots_path` set
  is a store-integrity problem relocation cannot fix.)

`ots_digest` is never written by the sweep (M4 of the audit): relocation changes `ots_path`
and nothing else on the row.

### D4 — The relocation phases, crash-safe and never-destroy

Under the claim + flock, per row, with `src = ots_path`, `dst = proof_path(current relpath)`:

1. **Aliasing check**: if `src` and `dst` resolve to the same directory entry
   (`os.lstat` dev+inode equality — a case-only rename on a case-insensitive filesystem),
   the proof is already in place: update `ots_path` to the canonical spelling, commit, and
   STOP — never unlink, since there is only one entry.
2. **Destination inspection**, ordered — the defer test comes first:
   a. `dst` is recorded as `ots_path` by a different row → **defer** (chain moves A→B, C→A
      converge over successive sweeps; cycles A→B, B→A in one scan defer each other until one
      side's slot is freed by its own relocation — each sweep relocates what it can and the
      pair resolves over passes; nothing is ever placed over a referenced slot).
   b. `dst` occupied, **byte-identical** to `src` (full content compare — committed digest
      alone is NOT sufficient, since two distinct proofs can commit to the same file digest
      with different attestation value) → the relocation is already half-done (a crash
      between phases 3 and 5, or a hard-link surviving both): skip to phase 4.
   c. `dst` occupied by anything else → archive the occupant to `.superseded/` via the
      existing preservation helper (it is unreferenced, per (a), and never discarded), then
      continue.
3. **Publish**: `mkdir -p` parents; hard-link `src → dst`. Where the filesystem refuses links
   (EPERM/EXDEV/ENOTSUP): copy to a temp name in `dst`'s directory, fsync, then publish with
   a **no-replace** primitive (`os.link(temp, dst)` + unlink temp — EEXIST restarts phase 2's
   classification; never a bare `os.rename` over a checkable window). Fsync the directory
   chain to the store root (existing durability helper). Both paths now hold the proof.
4. **Pointer commit**: set `ots_path = dst`, commit the DB. A datastore failure here rolls
   back and aborts the sweep's current item through the operation's normal error handling —
   it is NOT swallowed per-file (the session is not reusable after a failed commit); the
   filesystem state (proof at both paths) is exactly the phase-3/5 crash window and heals on
   the next sweep via 2b.
5. **Unlink** the old entry (`suppress(OSError)`) and fsync its directory. A leftover old
   copy after a crash between 4 and 5 is harmless: it sits in a slot that is no longer
   referenced; a future stamp there archives it (never-destroy). The sweep makes **no
   promise to garbage-collect** such copies (the audit's M3: a pointer already equal to
   canonical is not selected as stale, so no such promise is implementable there).

Crash walk: before 3 → nothing changed. Between 3 and 4 → pointer names `src`, which exists;
next sweep hits 2b and finishes. Between 4 and 5 → pointer names `dst`, which exists; stale
copy handled as above. The invariant holds at every boundary, and every completion path
re-established that the bytes at the destination are this row's proof (corroborated at D3,
byte-compared at 2b) before the pointer moved.

### D5 — Failure classification

Filesystem/precondition failures (phases 1–3, 5) are per-row: warn, leave the row unchanged
(pointer still truthful), continue the sweep; they re-warn on later sweeps. A permanent
destination refusal (`_NAME_MAX_BYTES` pre-check on components below the store root,
ENAMETOOLONG) is the same per-row warn — deliberately NOT the unstampable-path drop-to-`none`
rule, which would discard a placed proof's provenance; the operator keeps hearing about a
proof that cannot sit where the index expects it. Datastore failures follow D4 phase 4.

### D6 — `proof_path()` is the single canonical-location oracle

The stamp guard (D1), the sweep's staleness test (D2), and every relocation phase compute
locations via `proofs.proof_path(settings, collection_id, relpath)`. SQL may pre-filter
candidates, but no comparison that decides an action is string-assembled at a call site; a
test asserts the SQL pre-filter and the helper agree on a representative row set (including
names with `%`/`_`/quotes).

## Risks / Trade-offs

- [A deferred stamp (D1) could stay pending indefinitely if the sweep never converges the
  blocking row (e.g. a permanently refused destination)] → the member re-warns on every stamp
  pass and the row's state is visible in the panel as queued; the pairing warning names the
  blocking row. Accepted as loud, lossless behavior.
- [One healing cycle of pointer-at-old-path exposure after a move] → harmless once D1 is in:
  nothing can stamp over a referenced slot; verify follows `ots_path`, which is truthful.
- [Byte-compare at 2b reads two full proofs] → proofs are KB-sized.
- [Sweep scans `files` per collection daily] → indexed prefix pre-filter + Python
  confirmation; the upgrade pass already does heavier per-proof work.
- [Case-insensitive-store aliasing check relies on lstat identity] → dev+inode equality is
  the portable truth for "same entry"; on filesystems where even that is unreliable the 2b
  byte-compare still prevents loss (worst case: defer/warn).

## Migration Plan

No schema change, no migration. Deploy standard (`make deploy`). D1's guard is effective from
the first post-deploy stamp pass; D2's sweep converges live stale pointers on the next daily
upgrade or an operator `cairn upgrade`. Rollback = previous image; relocated rows stay
correct under old code (it reads `ots_path`, which is truthful either way).

## Open Questions

_None._
