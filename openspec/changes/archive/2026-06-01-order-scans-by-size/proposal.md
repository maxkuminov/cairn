## Why

The scheduler scans due corpora **sequentially** (the scanner is the single SQLite writer —
DESIGN.md §3) and currently in a fixed order by corpus `id` (insertion order). When a large corpus
is early in that order, every corpus behind it waits for it to finish before it is scanned. With a
1.4 TB Photos corpus whose deep (full re-hash) pass runs for hours, a tick that starts on Photos
blocks the entire fleet's freshness for that whole window — small corpora that a glance would clear
in seconds sit unscanned behind it. Ordering the smallest/cheapest corpora first lets the quick
ones finish promptly and pushes the long scan to the end of the pass, where it no longer starves
anything ahead of it.

## What Changes

- The scheduler's per-tick scan order changes from "by corpus `id`" to **cheapest-estimated-cost
  first**: due corpora are sorted ascending by estimated scan cost (primarily total tracked bytes,
  the dominant cost of the deep re-hash that produces the multi-hour scans; tie-broken by tracked
  file count, then `id` for determinism).
- Cost is estimated per tick from a single cheap aggregate over the existing `files` table
  (`COUNT(*)` and `SUM(size)` of non-`missing` rows per corpus) — **no schema change, no migration**.
- The existing "at most one deep pass per tick" guard is unchanged, but because the largest corpus
  now sorts last, its deep pass naturally runs after the fleet's quick passes rather than ahead of
  them.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `scan-scheduling`: the requirement that due corpora are scanned in a stable order changes — the
  order becomes cheapest-cost-first (ascending estimated scan cost) rather than by `id`, while
  remaining deterministic and still sequential.

## Impact

- Code: `src/services/scheduler.py` — `due_corpora()` ordering (or a new cost-aware ordering helper)
  and `run_due_scans()` (compute the per-corpus cost map via one aggregate query, pass it to the
  ordering). No change to `scanner.scan_corpus`, the data model, config, CLI, or the panel.
- Spec: `openspec/specs/scan-scheduling/spec.md` (DESIGN.md §5).
- Behavior: scan ordering only — what is scanned, how it is classified/stamped, and freshness
  reporting are all unchanged. No new dependencies.

## Non-goals

- **Concurrent / parallel scanning.** True parallelism is deliberately out of scope: it violates the
  single-writer SQLite invariant (DESIGN.md §3). Ordering does not fix the case where one in-flight
  multi-hour scan blocks corpora that become due *during* its run — only concurrency or
  chunked/yielding scans would, and that is a larger architectural change for a later proposal.
- Persisting a cached size/file-count column on the corpus row (the per-tick aggregate is cheap for
  a homelab-scale fleet).
- Changing deep-verify cadence, the one-deep-pass-per-tick guard, or any user-facing configuration.
