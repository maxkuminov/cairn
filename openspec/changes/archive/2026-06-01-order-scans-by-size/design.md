## Context

The scheduler's tick (`scheduler.run_due_scans`) selects due corpora via `due_corpora()` and scans
them **sequentially** — the scanner is the single SQLite writer (DESIGN.md §3), so two corpora can
never scan at once. `due_corpora()` preserves `list_corpora()` order, which is `ORDER BY Corpus.id`
(insertion order). So whichever corpus was added earliest is scanned first, regardless of size.

The expensive case is the weekly **deep** pass: `scan_corpus(..., deep=True)` re-hashes every
tracked file, which for a 1.4 TB Photos corpus runs for hours. While that pass runs, `run_due_scans`
does not return, the scheduler tick does not wake, and **no other corpus is scanned**. If Photos is
early in `id` order it monopolizes the front of the pass; small corpora that a stat-only quick scan
would clear in seconds wait behind it. The existing "at most one deep pass per tick" guard caps how
many deep passes stack, but does nothing about *order*.

The user's question — "scan the smallest corpora first?" — is the right instinct. This design
refines "smallest" to "cheapest estimated scan cost" and orders the sequential pass accordingly.

## Goals / Non-Goals

**Goals:**
- Scan due corpora cheapest-first within each tick so quick corpora finish promptly and the long
  scan lands at the end of the pass.
- Keep the change small, deterministic, migration-free, and fully inside `scheduler.py`.
- Preserve every existing scheduling behavior (cadence selection, first-run stagger, per-corpus
  error isolation, one-deep-pass-per-tick, freshness reporting).

**Non-Goals:**
- Concurrent scanning (violates the single-writer invariant — DESIGN.md §3).
- Fixing cross-tick monopolization: while one multi-hour scan runs, corpora that become due during
  it still wait. Ordering cannot fix that; only concurrency or chunked/yielding scans would. Out of
  scope, noted as future work.
- Any schema, config, CLI, or panel change.

## Decisions

### Decision: Order by estimated cost, primarily total tracked bytes

The dominant pain is the deep re-hash, whose cost is proportional to **bytes** read, so total
tracked bytes is the best single proxy for "how long will this corpus take". Quick scans are
file-count-bound (one `stat` each), so file count is the natural secondary key. Final sort key,
ascending: `(total_bytes, file_count, corpus_id)`. The trailing `corpus_id` guarantees a total,
deterministic order even when two corpora tie on bytes and count — important so the scan order does
not jitter between ticks.

*Alternative considered:* order by file count alone. Rejected — a corpus of a few thousand huge RAW
files would sort "small" by count yet dominate a deep pass by bytes, which is exactly the Photos
case.

### Decision: Compute cost per tick from a single aggregate query — no new column

Per tick, one grouped aggregate over the existing `files` table gives every corpus's cost at once:

```sql
SELECT corpus_id, COUNT(*) AS n, COALESCE(SUM(size), 0) AS bytes
FROM files
WHERE status != 'missing'
GROUP BY corpus_id
```

`missing` rows are excluded — there are no bytes to read for a file that is gone, so they should not
inflate a corpus's estimated cost. For a homelab-scale fleet (a handful of corpora, ≤ ~200k files
each) this aggregate is sub-millisecond and runs once per tick, so caching it on the corpus row buys
nothing and would need a migration plus write-path upkeep.

*Alternative considered:* persist `total_bytes`/`file_count` columns on `corpora`, updated by the
scanner. Rejected — adds a migration and a maintenance burden for no measurable gain at this scale.

### Decision: Keep selection and ordering as separate, pure steps

`due_corpora(corpora, next_due, now)` stays the pure "which corpora are due" filter (already unit
tested). Ordering becomes a second pure step — extend `due_corpora` with an optional
`cost: dict[int, tuple] | None` argument (sort by `(cost.get(id, (0,0)), id)` when provided; current
`id` order when omitted), or add a sibling `order_by_cost(due, cost)` helper. `run_due_scans` does
the impure work: run the aggregate query, build the `cost` map, then call the pure ordering. This
keeps the cost proxy testable without a DB and leaves existing `due_corpora` callers/tests working.

### Decision: Leave the deep-pass selection logic alone

`run_due_scans` already assigns the single per-tick deep slot to the first owed corpus it iterates.
Because ordering now puts the largest corpus last, the common single-owed case naturally runs the
big deep pass *after* the fleet's quick passes — the desired outcome — with no change to that logic.
When several corpora are owed a deep pass, the cheapest owed one takes the slot and the larger ones
fall back to quick passes and go deep on a later tick; given the weekly deep cadence this converges
within a few ticks and is acceptable. Noted as a trade-off rather than a change.

## Risks / Trade-offs

- **A large corpus could have its deep pass repeatedly deferred** when smaller corpora keep winning
  the one-deep-slot-per-tick race. → Weekly `verify_cadence_seconds` means a small corpus is only
  owed a deep pass briefly each week, so the large corpus gets its slot within a few ticks; the
  scheduler runs frequently relative to the weekly cadence. If this ever bites, a future tweak can
  prefer the most-overdue deep candidate.
- **Cross-tick starvation persists** (Non-Goal): while Photos' multi-hour scan runs, the tick is
  blocked and nothing else scans. Ordering improves the *first* pass's fairness, not this. Called
  out explicitly so it is not mistaken for solved.
- **Cost is an estimate from the last-known index**, not live disk state — a corpus that grew a lot
  since its last scan is undercounted for one tick. Self-correcting (next tick sees the new index)
  and harmless (worst case: one slightly-suboptimal ordering).

## Migration Plan

No data migration. The change is internal scheduler ordering. Deploy is the standard commit → push
→ `make deploy`; no Alembic revision, so no `make migrate`. Rollback is reverting the commit —
ordering reverts to `id` order with no state to undo.

## Open Questions

None. (Whether to also tackle cross-tick monopolization via concurrency or chunked scans is deferred
to a separate proposal, per Non-Goals.)
