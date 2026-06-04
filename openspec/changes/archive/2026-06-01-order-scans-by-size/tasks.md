## 1. Cost estimation

- [x] 1.1 Add a helper in `src/services/scheduler.py` that returns a per-corpus cost map via one
  grouped aggregate over `files` — `SELECT corpus_id, COUNT(*), COALESCE(SUM(size),0) ... WHERE
  status != 'missing' GROUP BY corpus_id` — keyed by `corpus_id`, value `(total_bytes, file_count)`;
  a corpus with no tracked rows defaults to `(0, 0)`.

## 2. Cheapest-first ordering

- [x] 2.1 Make ordering a pure, testable step: extend `due_corpora()` with an optional
  `cost: dict[int, tuple] | None` arg that, when provided, sorts the due list ascending by
  `(total_bytes, file_count, corpus_id)`; when omitted it preserves today's `id` order (keeps
  existing callers/tests intact). (Or add a sibling `order_by_cost(due, cost)` helper — either is fine.)
- [x] 2.2 In `run_due_scans()`, build the cost map (task 1.1) and pass it to the ordering so due
  corpora are scanned cheapest-first. Leave the deep-pass selection (`_deep_owed` / one-per-tick) and
  `next_due` bookkeeping unchanged.

## 3. Tests

- [x] 3.1 Unit-test the ordering: given mixed `(bytes, count)` costs, due corpora come out ascending
  by bytes; equal-bytes ties break by file count then `id`; the no-cost path still yields `id` order.
- [x] 3.2 Test `run_due_scans()` end-to-end with seeded corpora of differing total `size` and assert
  `scan_corpus` is invoked in cheapest-first order (e.g. patch/spy on the scanner and capture call
  order), and that a large corpus owed a deep pass still gets the single deep slot correctly.
- [x] 3.3 Run the suite (`pytest`) and confirm existing scheduler tests still pass.

## 4. Spec + verification

- [x] 4.1 `openspec validate order-scans-by-size --strict` passes.
- [x] 4.2 Smoke-test the loop locally: seed ≥2 corpora of clearly different size, run a tick
  (`cairn scan` / the scheduler startup pass or a focused harness), and confirm from logs/run rows
  that the smaller corpus is scanned before the larger one.
