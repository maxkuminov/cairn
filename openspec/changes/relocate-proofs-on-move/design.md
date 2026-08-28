# Design: relocate-proofs-on-move

## Context

`_reconcile_moves` (`src/services/scanner.py`) reconciles a 1:1 content-matched missing+added
pair into one surviving row: it repoints `relpath`, keeps identity (`first_seen`, `sha256`,
`ots_path`, `ots_state`, `ots_stamped_at`), emits one `moved` event. It deliberately mutates
only the index — so the proof file stays at the OLD relpath's canonical location
(`proof_path()` = `<proof_store>/<collection_id>/<relpath>.ots`), and `ots_path` keeps naming
it. The pointer is truthful but the old canonical slot is claimable: a later stamp of a
different file at the old relpath displaces the moved file's proof into `.superseded/` (never
destroyed, since guard-proof-and-restore-integrity) and the moved row's pointer then resolves
to a stranger's proof. `files.ots_digest` makes that misfiling detectable at verify time — as a
proof mismatch on a perfectly healthy file, which is a false alarm on the product's core
signal.

Machinery already in place (guard-proof-and-restore-integrity): proof mutation is single-writer
under the collection's operation claim; placement itself is serialized by a per-collection
flock at the proof store; `_place_proof` implements never-destroy placement with a durability
(fsync) chain to the store root; the daily upgrade pass walks proofs and backfills provenance.

## Goals / Non-Goals

**Goals:**

- After a reconciled move, the proof lives at the NEW relpath's canonical location and
  `ots_path` says so — the old slot is vacated before anything else can be stamped into it.
- **The pointer invariant**: at every moment, including mid-crash, `ots_path` names a path
  where this row's proof actually exists. No intermediate state may leave it dangling.
- Failure is per-file and never lossy; stale pointers (failed relocations AND every pre-fix
  moved row on the live deployment) converge via a daily healing pass with no migration.

**Non-Goals:**

- Deriving `ots_path` from `relpath` (can't represent mid-heal/failed states; 18 read sites).
- Repairing pointers already misfiled before this ships (verify's provenance ladder reports
  them; recovery from `.superseded/` stays manual).
- Any change to move-matching rules or to `_place_proof`'s stamping-time rules.

## Decisions

### D1 — A `relocate_proof(old, new, store_root)` primitive in `ots.py`, crash-safe by link-then-unlink

The move is not an `os.replace` (which could destroy an occupant) and not move-then-update
(which strands the pointer if the process dies between filesystem and DB). The order is:

1. **Pre-checks** (under the collection's proof flock, post-acquisition re-checked): source
   exists; destination canonical path unoccupied (else see D2); destination component lengths
   pass the `_NAME_MAX_BYTES` pre-check for components Cairn creates below the store root.
2. **Link phase**: `mkdir -p` the destination's parents; hard-link source → destination
   (`os.link`); fsync the destination directory chain up to the store root (the existing
   durability helper). Filesystems refusing `link` (EPERM/EXDEV/ENOTSUP) fall back to
   copy-to-temp + fsync + `os.rename` into place — same visible result: **both paths hold the
   proof**.
3. **Commit phase** (caller): update `ots_path` to the new location and commit the DB.
4. **Unlink phase**: remove the old path (`suppress(OSError)` — a leftover old copy is
   harmless, see below) and fsync its directory.

Crash between 2 and 3: pointer still names the old path, which still exists — truthful; the
healing pass later finds `ots_path` ≠ canonical and re-runs relocation, whose destination is
now occupied *by the same digest* → adopt-and-finish (D2). Crash between 3 and 4: pointer names
the new path, which exists — truthful; the stale old copy squats in the old canonical slot
until a future stamp there archives it (never-destroy) or the healing pass's cleanup removes it
when it matches the mover's recorded digest. At no point is the pointer dangling and at no
point is a proof byte lost.

### D2 — Occupied destination: adopt same-digest, defer otherwise; never displace silently

If the new canonical path is already occupied:

- **Same committed digest as the source proof** (parse both with `read_proof_facts`): adopt the
  occupant — treat the relocation as already done (finish at phase 3; the source copy is then
  the redundant one and is removed in phase 4). This is what makes retry/healing idempotent.
- **Referenced by another row**: if any other `files` row in the collection has
  `ots_path` == destination (one bounded query), DEFER — keep the old pointer, warn. Chain
  moves (A→B while C→A) converge over successive healing passes as each mover vacates its slot.
- **Occupied by anything else** (orphaned/unknown proof): archive the occupant into
  `.superseded/` via the existing preservation helper (never-destroy), then proceed. It is not
  referenced by any row and the archive keeps its bytes.

### D3 — Relocation runs inside `_reconcile_moves`, after the added-row delete/flush, before the stamp pass

The scanner already holds the run claim; `_reconcile_moves` already runs after the
missing-sweep and **before alerts/stamp/finalize**, so a same-scan newcomer at the vacated old
path stamps into an already-vacated slot. Rows with `ots_state` in (`none`, `pending`) or
`ots_path IS NULL` have nothing to relocate and skip untouched. The docstring's "mutates the
index only" contract is updated: reconciliation now moves proof files, under the same lease +
proof-flock discipline as stamping.

Failure of any phase for one file: log one WARNING, keep the OLD pointer (still truthful),
count nothing into `summary.errors` (the scan's own work succeeded; the proof store is a
parallel artifact — same philosophy as "a stamp never fails a scan"), and let healing retry.
The reconciliation itself (relpath repoint, `moved` event) proceeds regardless — the index must
describe the collection truthfully even when the proof store is briefly behind.

### D4 — The daily upgrade pass heals stale pointers

`run_daily_upgrade`'s per-collection work gains a bounded healing sweep: select rows where
`ots_path IS NOT NULL` and `ots_path` ≠ the canonical path for the current `relpath`
(expressible in SQL as a concat against the collection's store prefix; the store path comes
from settings). Each hit re-runs `relocate_proof` under the same claim + flock the upgrade
pass already holds. This single mechanism covers: relocations that failed transiently, chain
moves deferred by D2, crashes between D1's phases, and **every moved-before-this-change row on
the live deployment** — no migration, convergence within a day of deploy (or immediately via
`cairn upgrade`).

A path that fails permanently (`_NAME_MAX_BYTES`, ENAMETOOLONG) re-warns daily; deliberate —
an operator should keep hearing about a proof that cannot sit where the index expects it.
(The alternative — dropping to `ots_state='none'` like unstampable paths — would discard a
placed proof's provenance; refused.)

### D5 — `proof_path()` is the single canonical-location oracle

Both the scanner's relocation call and the healing sweep compute locations via
`proofs.proof_path(settings, collection_id, relpath)`. No string concatenation at call sites
(same rule as `panel_url`). The SQL prefix used by D4's sweep is derived from the same helper
applied to the empty relpath, keeping the two views of "canonical" from drifting.

## Risks / Trade-offs

- [FS mutation from the scanner widens the scanner's failure surface] → every phase is wrapped
  per-file; no relocation failure can fail a scan or a reconciliation (D3), mirroring the
  stamp-never-fails-a-scan rule.
- [Hard-link fallback (copy) doubles I/O for one proof] → proofs are KB-sized; negligible.
- [Stale old-slot copy after a crash between phases 3 and 4] → harmless by construction: a
  future stamp there archives it (never-destroy); healing removes it only when it matches the
  mover's recorded digest.
- [Healing sweep scans `files` per collection daily] → one indexed-prefix comparison over
  ~200k rows per collection per day; the upgrade pass already does heavier per-proof work.
- [Adopt-same-digest at destination could adopt a proof with same digest but different (better)
  attestation state] → adoption records what is on disk via the existing corroboration rules
  (`ots_digest` written only from parsed facts); a same-digest occupant is this file's proof by
  definition of the content-addressed store.

## Migration Plan

No schema change, no migration. Deploy standard (`make deploy`); healing converges live rows
within one daily upgrade pass, or immediately via `cairn upgrade`. Rollback = previous image;
already-relocated proofs remain correct under the old code (it reads `ots_path`, which is
truthful either way).

## Open Questions

_None._
