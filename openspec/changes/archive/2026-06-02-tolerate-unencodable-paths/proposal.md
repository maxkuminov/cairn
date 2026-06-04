# Tolerate non-UTF-8 filenames and never wedge a scan run

## Why
A single file with a non-UTF-8 name silently freezes a whole corpus. On the live deploy the
**Photos** corpus (186k files) has had run 113 stuck at `result='running'` since
2026-06-01 22:58 — every scheduled scan since has been refused — while the other corpora scan
every cadence. Root cause: exactly one file,
`…/Wedding Irkutsk 19 Sept 09/All pictures-small/1à.jpg`, whose name is the Latin-1 byte `0xe0`,
not valid UTF-8.

The failure chain (DESIGN.md §5 *Per-run flow*; §3 *SQLite is the index*):

- `os.walk` decodes that on-disk name via `surrogateescape` into the Python string
  `'1\udce0.jpg'` (a lone surrogate). The filesystem ops — `stat`, `open`, SHA-256 — all accept it.
- SQLite cannot bind a lone surrogate as TEXT: the batch `session.commit()` inside the scanner's
  `_drain()` raises `UnicodeEncodeError: surrogates not allowed`. The per-file `try/except OSError`
  does not catch it (it is not an `OSError`), so it escapes to the scan body's outer handler.
- The outer handler sets `result='error'` but leaves the async session in a pending-rollback state,
  so the **final** `await session.commit()` — the one that would move the run from `running` to a
  terminal state — itself raises and `scan_corpus` propagates. **The run row is never updated.**
- `corpora.active_run()` is the single concurrency guard: a perpetually-`running` run makes the
  scheduler skip the corpus every tick and the panel refuse manual scans. The startup reaper clears
  it on restart, but the next scan re-hits the same file and re-wedges — a permanent loop.
- Side effects: `/healthz` reports the corpus `stale` → `degraded` (a dead-man's-switch **false
  alarm** that masks real staleness), and the corpus is effectively un-monitored.

Two independent defects compound here: (1) the scanner cannot store a path SQLite rejects, and
(2) a scan body that raises can leave its run wedged at `running`. This change fixes both.

## What Changes
- **Skip un-storable paths, don't abort the scan.** Before a walked file is recorded, the scanner
  checks that its relative path round-trips through UTF-8 (what SQLite TEXT requires). A path that
  does not (a non-UTF-8 on-disk name surfaced as lone surrogates) is **skipped**: it is counted
  among the run's errors (so the run finishes `partial`, keeping the dead-man's switch fresh), and
  one batched `WARNING` names the skipped paths (raw bytes) so the operator can find them. No
  `files` row is created, so the file never churns as `missing`/`added` across scans.
- **Guarantee a terminal run state.** A scan SHALL always finalize its run to `ok`/`partial`/`error`
  even if its body raises: the error path now `rollback()`s the session before finalizing, and the
  finalizing commit has a last-ditch fallback that force-updates the run to `error` in a clean
  transaction. A scan can no longer leave a corpus perpetually `running` (and thus blocked) between
  process restarts — defense-in-depth complementing the existing startup reaper.
- **Operational:** no schema change and no migration. The currently-wedged runs (Photos 113 and
  any other leftover `running`) are cleared by the existing startup reaper when this build deploys
  and restarts; the next Photos scan then completes `partial`, skipping the one bad file.

## Non-goals
- **Tracking/notarizing the bad file.** This change makes the corpus scannable; it does **not**
  store, hash-track, or OTS-stamp the un-encodable file — it is reported-and-skipped, not monitored.
  Faithfully persisting arbitrary filesystem bytes (a reversible relpath encoding, or fixing the
  name at the source) is a deliberate follow-up (see `design.md`). The byte count of skipped files
  is surfaced, so the gap is never silent.
- **A new schema column / migration.** Skips reuse the existing `errors → partial` summary path; no
  `runs`/`files`/`events` change.
- **Changing classification.** `added`/`modified`/`missing`/`moved`/`restored` semantics for
  storable files are untouched.
- **Retroactive cleanup of past wedged runs by code.** Stale `running` runs are reconciled by the
  existing startup reaper on deploy, not by a one-off migration here.

## Impact
- **Affected specs:** `integrity-scanning` (new requirement: tolerate un-storable paths; new
  requirement: a scan always reaches a terminal run state; amended classification requirement to
  note the skip).
- **Affected code:** `src/services/scanner.py` only — a `_db_storable()` guard at the top of the
  walk loop, a batched skip log, and a hardened finalize (`rollback()` on the error path + a
  force-terminal fallback on the finalizing commit).
- **Data migration:** none. Existing rows untouched; corpora with only UTF-8 names behave exactly
  as today (`ok`, not `partial`).
- **DESIGN.md:** consistent with §3 (the DB is an index over read-only bytes — an un-indexable name
  must degrade gracefully, not halt monitoring) and §5 (a per-file problem must never abort the run,
  matching the existing "unreadable file does not abort the scan" guarantee).
