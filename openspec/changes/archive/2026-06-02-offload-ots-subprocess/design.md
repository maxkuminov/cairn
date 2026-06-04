# Design — offload blocking OTS subprocess work off the event loop

## Context
Cairn runs as a single uvicorn process with one asyncio event loop that serves the panel **and**
hosts the in-process scheduler. The maintained `ots` CLI is a subprocess (DESIGN.md §6); every OTS
operation shells out via `subprocess.run` in `ots._run_ots`. `subprocess.run` is synchronous: the
calling thread blocks until the child exits. When that calling thread is the event loop, the loop
cannot service any other coroutine — including HTTP handlers — for the whole call (process spawn +
calendar/explorer network round-trip). The scanner already sidesteps this for hashing
(`await asyncio.to_thread(sha256_file, path)`); the proof path did not.

## Decision — wrap the call sites in `asyncio.to_thread`, keep them sequential
Push each blocking call onto a worker thread at the async boundary, exactly as the scanner does:

```python
complete = await asyncio.to_thread(ots.upgrade, entry.ots_path)             # proofs.upgrade_incomplete
outcomes = await asyncio.to_thread(ots.stamp_batch_via_symlink, pairs, cals, staging)  # stamp_pending
await asyncio.to_thread(ots.stamp_via_symlink, real, out, cals, staging)    # stamp_pending fallback
digest = await asyncio.to_thread(scanner_svc.sha256_file, source)           # /verify re-hash
result = await asyncio.to_thread(ots_svc.verify, fe.ots_path, digest)       # /verify
await asyncio.to_thread(proofs_svc.export_bundle, fe, dest, corpus.root)    # /export
```

Why call-site wrapping (not async variants in `ots.py`): the existing convention is to keep the
subprocess wrappers synchronous and `to_thread` them at the async caller (`scanner.py`,
`manifest.py`, `smtp.py`, `main.py` all do this). It keeps `ots.py` a pure, testable subprocess
shim and puts the concurrency concern where the event loop actually lives.

Why still sequential: each call is `await`ed before the next, so at most one `ots` subprocess runs
at a time — unchanged from today. The shared symlink staging dir
(`<proof_store>/.staging`) and calendar request rate are therefore untouched. The *only* difference
is that the loop thread is free to run other coroutines while the worker thread blocks. This buys
panel responsiveness without introducing any new concurrency hazard.

## What this does and does not fix
- **Fixes:** the panel no longer freezes while a stamp/upgrade/verify/export runs. A 28k-proof
  upgrade pass now serves concurrent dashboard requests in milliseconds instead of ~20 s.
- **Does not fix:** the scheduler still `await`s `run_due_scans` then `run_daily_upgrade` inline per
  tick, so a multi-hour upgrade still postpones the next scan tick and a corpus can momentarily go
  `stale` (false `degraded`). Decoupling the passes is a separate change (its fix is structural —
  run the upgrade as its own task, or cap work per tick — not just threading). Called out as a
  non-goal so the scope stays the one-line incident fix.

## Risks
- `to_thread` uses the default executor (a bounded thread pool). Because calls stay sequential, at
  most one OTS subprocess thread is live at a time, so the pool is never exhausted by this path.
- SQLAlchemy objects are mutated only on the event-loop thread (before/after the `to_thread` call),
  never inside the worker — the worker only runs the pure subprocess/IO function. No cross-thread
  session access is introduced.

## Test approach
The `ots` subprocess is always mocked in tests. To prove offloading without timing flakiness, the
mock records `threading.current_thread()` and the test asserts the OTS function ran on a thread
other than the main (event-loop) thread — a direct, deterministic check that the call was
`to_thread`-ed, plus the existing functional assertions (state transitions, counts) still hold.
