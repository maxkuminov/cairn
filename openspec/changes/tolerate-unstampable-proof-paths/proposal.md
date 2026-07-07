# Tolerate un-writable proof output paths and never abort a stamp batch

## Why
On 2026-07-07 the live deploy crash-looped while batch-stamping the Skate SharePoint tree. A single
Cyrillic filename made the batch raise an uncaught `OSError`, and because the container runs
`restart: unless-stopped`, every restart re-ran scan-all-on-startup over the whole read-only tree —
pinning the host (the co-located document-extraction pool saturated ~10 of 12 cores; the box went
intermittently unresponsive). Mitigation was `docker stop cairn`.

Root cause (`src/services/ots.py`, `ots-notarization` §"Stamp pending files in batches"):

```
File "/app/src/services/ots.py", line 250, in stamp_batch_via_symlink
    os.replace(staged_ots, out)
OSError: [Errno 36] File name too long:
  '…/Договор займа от 09.19.2024 - Линева Елена - Расчет процентов.xlsx.ots'
```

- Errno 36 = **ENAMETOOLONG**. A filesystem caps one path component at `NAME_MAX` **bytes** (255 on
  ext4), not characters. Cyrillic UTF-8 is ~2 bytes/char, so a name that *looks* short is ~2× its
  length in bytes; the proof name is the file's own name **plus `.ots`**, which tips an already-long
  multi-byte name past the limit.
- The stage → final rename is `<proof_store>/.staging/<hash>.ots` (safe, fixed length) →
  `<proof_store>/<cid>/<relpath>.ots` (the **unbounded** name). `stamp_batch_via_symlink` decided a
  member's success by *filesystem truth* (did `<link>.ots` get produced) and handled a member that
  yielded **no** proof — but the proof here **was** produced; it was the `os.replace` **onto the
  output path** that raised, and that call was not guarded. The exception escaped the batch, so the
  whole pass aborted and no file in it was stamped.
- Both stamp callers (`scanner.scan_collection`, `proofs.run_stamp_backfill`) wrap `stamp_pending`
  in `except Exception`, so the *scan* itself recorded terminal — but the offending files stayed
  `ots_state='pending'`, so **every** subsequent scan re-queued and re-failed them. Combined with the
  restart-storm re-walking the tree, this is the host-saturation loop.

This is the same failure *class* as the already-fixed surrogate-filename wedge
(`tolerate-unencodable-paths`), one layer down: there the scanner couldn't **store the relpath**;
here the notary can't **write the proof file** for a relpath it stored fine.

## What Changes
- **Guard the proof write; a filesystem refusal is per-file, never batch-fatal.** A new
  `OtsPathError` (subclass of `OtsError`) marks an output path a filesystem will not accept. Both
  `stamp_via_symlink` and `stamp_batch_via_symlink` now (a) **pre-check** the output name's byte
  length against `NAME_MAX` and skip an un-writable member before spending a symlink or a calendar
  round-trip, and (b) move the produced proof through `_place_proof`, which wraps
  `mkdir`+`os.replace` and re-raises any `OSError` as `OtsPathError`. A skipped/failed member leaves
  the rest of the batch stamped, exactly as the existing "failed batch member" isolation already
  promised for the no-proof case.
- **Permanent skip, not perpetual retry.** In `proofs.stamp_pending`, a member that comes back with
  `OtsPathError` from the single-file fallback is **skipped and counted** and dropped from `pending`
  to `ots_state='none'` — so a normal scan (which only queues *added/changed* files) will not
  re-queue and re-fail it every pass. It is warned (one summary line names the count) and left
  unstamped, mirroring the scanner's reported-and-skipped treatment of un-storable paths. A
  transient `OtsError` (unreachable calendar, timeout) is unchanged: left `pending` for the next
  pass. An on-demand `stamp --all` may still retry it — the pre-check makes that a cheap no-op.
- **Operational / throttle:** no schema change, no migration. Fixing the crash removes the
  crash-loop that re-walked the tree and saturated the host — that is the cairn-side throttle. Cairn
  already scans and stamps strictly sequentially (one collection, one file, one `ots` subprocess at a
  time) and the deployment already caps it at `cpus: "2.0"` + `cpu_shares: 256`; a further host-wide
  CPU/IO guardrail (cgroup) is tracked separately in the host-hardening work, out of this repo.

## Non-goals
- **Stamping the un-writable file under a safe alternate name.** Deriving a length-capped sidecar
  name (cap the base component + append a content hash, or a flat hash-named proof store) would let
  the file be notarized despite its name. It is deliberately deferred (see `design.md`): the file is
  **reported-and-skipped, not stamped** — the conservative fix for a live safety tool, and it keeps
  the proof-store layout (`<proof_store>/<cid>/<relpath>.ots`) that `verify`/`export`/`upgrade` read
  via the stored `ots_path`. The skipped count is surfaced so the gap is never silent.
- **A schema column / migration.** The permanent skip reuses the existing `ots_state='none'`
  (unstamped) state; no `files`/`runs`/`events` change.
- **Changing scan classification or alerting.** `ots_state` is orthogonal to `status`; a skipped file
  is still change-monitored and still alerts on `missing`/WORM-`modified` exactly as before.
- **Host-level cgroup throttling.** The system-wide CPU/IO guardrail on the host is a separate
  sysadmin item; this change only removes the cairn-side runaway (the crash-loop).

## Impact
- **Affected specs:** `ots-notarization` (new requirement: tolerate un-writable proof output paths;
  modified requirement: a failed batch member does not drop the batch's proofs — now covers a member
  whose proof cannot be written to its output path, and the permanent-vs-transient skip distinction).
- **Affected code:** `src/services/ots.py` (`OtsPathError`, `_proof_output_writable`, `_place_proof`,
  guarded `stamp_via_symlink` / `stamp_batch_via_symlink`), `src/services/proofs.py`
  (`stamp_pending` skip-and-count → `none`). No caller signatures change.
- **Operational:** the stopped container can be redeployed and restarted safely once this ships —
  the bad file is now skipped-and-counted and the batch completes. Files previously wedged `pending`
  by this bug drop to `none` on their next stamp pass. Deploy/restart of the live host is gated on
  Max's go-ahead (see the queue ticket's stop conditions) because restarting re-runs the full batch.
