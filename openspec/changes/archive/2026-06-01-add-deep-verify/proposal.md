## Why

The scanner fast-paths on size+mtime: it only streams a SHA-256 for files whose size or mtime
changed (or that have no prior hash), and skips everything else (`scanner.py` "Fast-path:
unchanged"). That is exactly right for cheap nightly scans — but it means **silent bit-rot is
never caught**. When a file's bytes change while its size and mtime stay identical (a flipped bit
on disk, a bad sector, a botched in-place write that preserves mtime), no scan after the first
ever re-reads those bytes, so the corruption goes undetected forever. For a file-integrity
monitor whose whole promise is "your archive is intact", that is the one gap that matters.

The fix is a periodic **deep verify**: a full re-hash that re-reads every tracked file regardless
of size/mtime, on a per-corpus cadence (default weekly) that is independent of the cheap scan
cadence (default nightly). Plus a `cairn bench` command so an operator can measure local SHA-256
throughput (~100 MB/s on the homelab host) and estimate how long a deep pass will take per corpus
before enabling it.

References: DESIGN.md §5 (per-run flow — the fast-path is described there; this change adds the
periodic full pass it implies for archival corpora).

## What Changes

- **Deep scan mode** (`src/services/scanner.py`): `scan_corpus` gains a keyword-only `deep`
  flag. In deep mode every tracked, non-missing file is re-hashed, bypassing the size+mtime
  fast-path; the existing hash-comparison then classifies the result with no other change — a
  byte difference is a `modified` nag (worm) / silent re-baseline (churn) and re-queues an OTS
  stamp exactly as today, while an intact file stays `ok` and is **not** re-stamped. The run
  records whether it was a deep pass.
- **Per-corpus deep cadence** (`Corpus.verify_cadence_seconds`, default weekly `604800`,
  `0` = disabled) and a `Corpus.last_full_scan_at` timestamp. The scheduler runs a deep pass for
  a corpus when its deep cadence has elapsed since `last_full_scan_at` (wall-clock, so an overdue
  deep pass survives restarts); a deep pass replaces — never doubles — the quick pass on that
  due tick, and at most one corpus goes deep per tick so a long re-hash can't starve the fleet.
- **`Run.deep`** boolean so the panel/history can distinguish deep runs from quick ones.
- **`cairn bench [--path DIR] [--bytes N] [--estimate]`**: measures local SHA-256 throughput
  (in-memory probe, or real files under `--path`) and optionally prints a per-corpus deep-scan
  duration estimate (`total size / throughput`). Read-only, no new dependencies.
- **Corpus form + `add-corpus`**: a deep-verify cadence selector on the corpus form and a
  `--verify-cadence` flag on `cairn add-corpus`, threaded through `create_corpus`/`update_corpus`.

### Out of scope (deferred)

- Surfacing the deep-scan ETA inside the panel UI beyond the cadence selector — the estimate
  lives in `cairn bench --estimate` for now.
- Chunked/resumable deep scans and parallel hashing — the single-writer sequential model stands;
  the one-deep-per-tick cap is the starvation guard.
- Emitting an info-level event when a churn corpus re-baselines bit-rot — churn semantics
  (change is expected, no nag) are unchanged; documented as a known trade-off.

## Capabilities

### Modified Capabilities

- `integrity-scanning`: a scan gains a deep mode that re-hashes every tracked file (bypassing the
  fast-path) to detect silent bit-rot, plus a throughput benchmark that estimates deep-scan cost.
- `scan-scheduling`: the scheduler runs a periodic deep verify per corpus on its
  `verify_cadence_seconds`, replacing the quick pass on the owed tick and capped to one deep pass
  per tick.
- `corpus-management`: a corpus persists its deep-verify cadence (`verify_cadence_seconds`,
  `0` = disabled) alongside its scan cadence.

## Impact

- **Code**: `src/services/scanner.py` (deep param + one-line fast-path guard + `run.deep`),
  `src/services/scheduler.py` (`_deep_owed` + deep gating + per-tick cap),
  `src/models/db.py` (+3 columns), `src/services/corpora.py` (thread cadence through),
  `src/cli.py` (`bench` + `--verify-cadence`), `src/control_panel/routes.py` +
  `templates/corpus_form.html` (cadence field).
- **Database**: new `corpora.verify_cadence_seconds` (default 604800), `corpora.last_full_scan_at`
  (nullable), `runs.deep` (default false). New Alembic revision `0002_deep_verify` off
  `0001_initial`, server-defaulted so existing rows backfill (existing corpora → weekly, never
  deep-scanned).
- **Tests**: `tests/test_scanner.py` (deep re-hashes unchanged files; deep detects same-size
  same-mtime bit-rot in worm; churn re-baselines; intact files not re-stamped; `Run.deep` set),
  `tests/test_scheduler.py` (`_deep_owed` truth table; deep chosen when owed + `last_full_scan_at`
  persisted; quick when not owed; not persisted on failure; one-deep-per-tick cap).
