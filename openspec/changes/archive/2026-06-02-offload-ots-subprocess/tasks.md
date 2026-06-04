# Tasks — offload blocking OTS subprocess work off the event loop

## 1. Offload the proof-service call sites
- [x] 1.1 Add `import asyncio` to `src/services/proofs.py`.
- [x] 1.2 In `proofs.stamp_pending`, run the batched `ots.stamp_batch_via_symlink` call via
  `await asyncio.to_thread(...)`, and the per-file `ots.stamp_via_symlink` fallback via
  `await asyncio.to_thread(...)`.
- [x] 1.3 In `proofs.upgrade_incomplete`, run `ots.upgrade` via `await asyncio.to_thread(...)`.

## 2. Offload the panel route call sites
- [x] 2.1 In the `/verify` route (`control_panel/routes.py`), run the `scanner_svc.sha256_file`
  re-hash and the `ots_svc.verify` call via `await asyncio.to_thread(...)`.
- [x] 2.2 In the `/export` route, run `proofs_svc.export_bundle` via `await asyncio.to_thread(...)`.

## 3. Tests & verification
- [x] 3.1 Unit: monkeypatch `ots.upgrade` to record `threading.current_thread()`; assert
  `proofs.upgrade_incomplete` ran it on a non-main (worker) thread and still flips `incomplete →
  complete` and returns the right counts.
- [x] 3.2 Unit: monkeypatch the stamp wrapper to record its thread; assert `proofs.stamp_pending`
  ran it off the main thread and still records `ots_state='incomplete'` for each stamped file.
- [x] 3.3 Run the proof/scanner suites:
  `PYTHONPATH=. .venv/bin/pytest tests/test_ots.py tests/test_scanner.py -q` (mocked `ots`; no
  network).
- [x] 3.4 `openspec validate offload-ots-subprocess --strict` passes.

## 4. Deploy
- [x] 4.1 Commit, archive (sync specs), push, `make deploy` (restart ends the in-flight blocking
  upgrade; no migration). Confirm the panel responds quickly during the next upgrade pass and
  healthz returns to `ok` once the starved corpora are re-scanned.
