# Offload blocking OTS subprocess work off the event loop

## Why
The web panel becomes unresponsive whenever a large OTS pass runs. Observed on the live deploy: the
daily upgrade pass over **a large Documents collection** (28,632 `incomplete` proofs) pegged a core and
made the dashboard take **~20 s** to load while healthz flapped to `degraded`.

Root cause: the maintained `ots` CLI is invoked through synchronous `subprocess.run`
(`src/services/ots.py:99`, via `_run_ots`), and the async callers run it **directly on the app's
single asyncio event loop** with no offloading:

- `proofs.upgrade_incomplete` → `ots.upgrade()` per proof (`proofs.py:210`) — the daily pass.
- `proofs.stamp_pending` → `ots.stamp_batch_via_symlink()` / `ots.stamp_via_symlink()`
  (`proofs.py:91`, `:98`) — scan-time stamping and the "Stamp all" backfill.
- The panel `/verify` route → `scanner.sha256_file()` (re-hash, possibly a multi-GB video) **and**
  `ots.verify()` (network to a block explorer) (`routes.py:1108`, `:1115`).
- The panel `/export` route → `proofs.export_bundle()` (copies file bytes) (`routes.py:1176`).

Each call blocks the event loop for the duration of a process spawn plus a network round-trip (the
calendar for stamp/upgrade, the explorer for verify). While blocked, **no other coroutine runs** —
every concurrent panel request queues behind it. The scanner already avoids exactly this by hashing
through `asyncio.to_thread` (`scanner.py:54`); the OTS/proof path never got the same treatment.

This is the maintained-`ots`-CLI subprocess model from DESIGN.md §5/§6 colliding with the
single-event-loop uvicorn runtime: correct functionally, but it must not run on the loop thread.

## What Changes
- Offload every blocking OTS/IO call that is reachable from the event loop to a worker thread via
  `asyncio.to_thread`, mirroring the scanner's hashing:
  - `proofs.stamp_pending`: the batched `ots.stamp_batch_via_symlink` call and the per-file
    `ots.stamp_via_symlink` fallback.
  - `proofs.upgrade_incomplete`: the per-proof `ots.upgrade` call.
  - `/verify` route: the `sha256_file` re-hash and the `ots.verify` call.
  - `/export` route: the `export_bundle` copy.
- Semantics are **unchanged**: calls remain sequential (each `await`ed before the next), so the
  proof staging directory and calendar usage behave exactly as today — only the thread the
  subprocess blocks on changes, freeing the event loop to serve the panel concurrently.

## Non-goals
- **Parallelizing OTS calls.** Stamps/upgrades stay sequential (the symlink staging dir is shared
  and calendars deserve politeness); this change is about not *blocking the loop*, not throughput.
- **Scheduler serialization / staleness during long passes.** The scheduler still `await`s each
  pass inline, so a multi-hour upgrade still delays the next scan tick and a corpus can briefly read
  `stale` (a transient false `degraded`). That is a separate concern with a different fix
  (interleave or background the passes) and is left as a follow-up; this change only removes the
  event-loop blocking that freezes the panel.
- **CLI offloading.** `cairn verify`/`scan`/`import-manifest` run in a one-shot process that does
  not share the panel's event loop; their synchronous calls are fine and are left as-is.
- **Changing OTS behavior or outputs.** Same `.ots` files, same classification, same run records.

## Impact
- **Affected specs:** `ots-notarization` (new requirement: notarization operations do not block the
  application event loop).
- **Affected code:** `src/services/proofs.py` (3 call sites + add `import asyncio`),
  `src/control_panel/routes.py` (`/verify` re-hash + verify, `/export` bundle).
- **Data migration:** none. No schema change.
- **DESIGN.md:** consistent with §5/§6 (OTS via the `ots` CLI subprocess) — it just runs those
  subprocesses off the event loop, the same discipline the scanner already applies to hashing.
- **Operational:** deploying restarts the container, which ends the in-flight blocking upgrade pass
  immediately (idempotent — the next daily pass resumes the still-`incomplete` proofs).
