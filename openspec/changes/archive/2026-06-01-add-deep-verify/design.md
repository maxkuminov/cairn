## Context

`scan_corpus` already does the cheap thing well: it compares size+mtime and only streams a
SHA-256 when those differ or no hash exists. The gap is silent bit-rot — bytes that change with
size+mtime intact are never re-read. A deep verify closes that gap by re-hashing everything on a
slower cadence. The design constraint is that the scanner is the single SQLite writer and the
scheduler is sequential, so a deep pass over a large corpus is a long, blocking operation we must
keep from starving other corpora's freshness.

## Decisions

### 1. Deep mode is a one-line widening of the existing fast-path guard

`scan_corpus(session, corpus, *, deep=False)`. The only classification change is the guard that
currently reads `row.size != size or row.mtime != mtime or row.sha256 is None` — it becomes
`deep or (... unchanged ...)`. Everything downstream is reused verbatim: a re-hash that differs
from the stored digest flows into the existing modified-worm / silent-rebaseline-churn branch
(including the `ots_state="pending"` re-queue and the alarm), and a re-hash that matches flows
into the existing "metadata only" branch that keeps the file `ok` and refreshes `last_checked`.

Consequence (important and intended): **intact files are never re-stamped on a deep pass.** The
OTS re-queue lives only inside the byte-changed block, so a deep pass over a healthy `perfile`
corpus queues and stamps nothing. Only genuinely changed bytes get a fresh proof.

### 2. Deep cadence is per-corpus and wall-clock; quick gating stays monotonic

The existing due-gating uses `time.monotonic()` (`next_due` dict) — correct for a short cadence
that need not survive restarts. The deep decision is different: cadence is days, and we do **not**
want a restart to forget that a deep pass is overdue. So `last_full_scan_at` is a wall-clock
`datetime` persisted on the corpus, and `_deep_owed` compares `now_wall - last_full_scan_at`
against `verify_cadence_seconds`. `verify_cadence_seconds <= 0` disables deep verify;
`last_full_scan_at is None` (never deep-scanned, including all rows right after migration) counts
as owed.

### 3. Deep replaces quick on the owed tick; one deep per tick

A deep pass is a strict superset of a quick pass (it re-hashes everything *and* still detects
new/missing). So when a corpus is due and deep is owed, we run a single deep scan that tick — we
never run both. The trigger is unchanged (`hash_cadence_seconds` via `next_due`); deep only
chooses the flavor. To bound the worst case, `run_due_scans` runs **at most one** deep pass per
tick: the first owed corpus goes deep, any other owed corpora that tick fall back to quick and
get their deep pass on a later tick. This naturally spreads the post-migration catch-up (every
corpus is owed at once) across ticks instead of blocking on a multi-hour back-to-back run.

`last_full_scan_at` is persisted only **after** a deep scan returns successfully, so a crash
mid-scan leaves the deep clock un-advanced and the pass retries next tick (mirroring how the
quick clock still advances on failure to avoid monopolizing ticks).

### 4. Health freshness is unaffected

`compute_health` keys off the newest `ok`/`partial` run and the threshold uses
`hash_cadence_seconds` (not the deep cadence), so the dead-man's-switch window is unchanged. A
deep run still writes a normal run row, so completing one refreshes freshness as usual. The
one-deep-per-tick cap is what keeps a long deep scan from aging *other* corpora past their
threshold.

### 5. Benchmark is read-only and dependency-free

`cairn bench` reuses `scanner.sha256_file` / `scanner.CHUNK`. The default mode hashes an
in-memory buffer (no temp files) to report pure hash throughput; `--path` streams real files for
end-to-end (disk+hash) throughput; `--estimate` sums `files.size` per corpus and prints
`size / throughput`. It never writes anything.

## Risks / trade-offs

- **Churn + bit-rot**: in churn mode a deep-detected byte change silently re-baselines (no nag),
  matching churn semantics. An operator who wants bit-rot alerts should use worm mode. Documented,
  not changed.
- **verify_cadence < hash_cadence**: every due tick becomes deep. Valid (deep is a superset) but
  expensive; left to operator judgement (no hard validation).
- **First deep pass after upgrade**: existing corpora backfill to `last_full_scan_at = NULL`
  (owed now). The one-deep-per-tick cap rate-limits the catch-up; we deliberately do not backfill
  to `utcnow()` because that would delay the first real integrity check by a full week.
