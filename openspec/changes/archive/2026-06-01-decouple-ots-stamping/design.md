## Context

`perfile` corpora queue a file for stamping by setting `files.ots_state = 'pending'`: on first
sight (`added`) and on any content change (`modified`/re-baseline, both worm and churn). At the end
of a scan, `proofs.stamp_pending()` loops the `pending` rows and calls `ots.stamp_via_symlink()`
once per file — each call shells out to `ots stamp <symlink>`, contacts every calendar, and writes
the `.ots`. Corpus roots are read-only, so the stamp is taken through a throwaway symlink in
`<proof_store>/.staging` and the `.ots` is moved to `<proof_store>/<corpus_id>/<relpath>.ots`.

Two facts shape this change:
- Measured ~0.3 stamps/sec (calendar-round-trip bound). ~93k pending files = days; the store is
  effectively never completed.
- The pre-existing baseline is distinguishable from new work by state: a file deliberately not
  notarized is `ots_state = 'none'`; a file awaiting a stamp is `'pending'`. The 186k photo archive
  is `ok/none`, so it is already excluded from automatic stamping — the gap is a deliberate way to
  stamp such a baseline *when asked*.

## Goals / Non-Goals

**Goals:**
- Stamp at near-aggregation speed while preserving one independent `.ots` per file.
- Make the stamp policy explicit: auto = new/changed only; on-demand = backfill everything unstamped.
- Preserve read-only-mount safety, the "stamp never fails a scan" guarantee, and per-file resilience.

**Non-Goals:**
- Version history (changed files overwrite — decided), folder-aggregate proofs, concurrency,
  and any change to verify / export / `upgrade` / `none` corpora.

## Decisions

**1. Batch with one `ots stamp` over N staging symlinks.**
Build the staging symlinks for up to `ots_stamp_batch_size` pending files, invoke
`ots stamp <link1> … <linkN>` once (OTS aggregates the N digests into one calendar commitment), then
move each produced `<linkI>.ots` to its destination. Each file still gets its own proof and
`ots_state='incomplete'`. *Alternative — a thread pool of per-file calls:* rejected; still N
round-trips and N× calendar load. Batching is the OTS-native fix and strictly fewer requests.

**2. Batch-then-fallback (filesystem-truth) for failure isolation.**
After the batch call, check which `<linkI>.ots` actually exist; move + mark those stamped. For any
member with no `.ots` (whole-batch failure, timeout, or one unreadable file aborting the run), retry
those members via the existing single-file `stamp_via_symlink()`. Members that still fail stay
`pending` and are logged, exactly as today. Correctness does not depend on `ots`'s exit code.

**3. Default auto-stamp = scan-queued only (no behavior change, now documented).**
`stamp_pending()` continues to select `ots_state='pending'` — i.e. only files this scan added or
changed. The pre-existing `none` baseline is never auto-stamped. This is the "stamp new files from
now on" guarantee.

**4. On-demand "Stamp all" = mark-then-stamp.**
`cairn stamp --corpus X --all` (and the panel button) set `ots_state='pending'` for every file that
currently has no proof — `ots_state='none'` and `status != 'missing'` — then run the same batched
`stamp_pending()`. Files already `incomplete`/`complete` are skipped (a re-stamp would only yield a
later, weaker date). `cairn stamp --corpus X` without `--all` stamps the already-`pending` set,
decoupled from a re-hash (no scan needed). Neither path writes under the corpus root.

**5. Config knob `ots_stamp_batch_size` (default 256).** Bounds argv length (links are short uuid
names) and Merkle build; cuts ~93k calls to ~370. Operators can raise it.

## Risks / Trade-offs

- A single calendar timeout affects a whole batch → per-file fallback recovers its files; bounded
  default keeps batches small.
- argv / Merkle size for very large batches → bounded by `ots_stamp_batch_size`.
- Staging holds N symlinks + `.ots` at once → always clean up links and stray `.ots` in `finally`.
- "Stamp all" on a huge `none` baseline (e.g. the 186k photos) is a deliberate, potentially long
  action → it is opt-in only and reports its count; it is never triggered automatically.

## Migration Plan

Pure code change — **no DB migration**. Deploy via `make deploy` + recreate; re-enable the scheduler
(`CAIRN_SCHEDULER_ENABLED=1`, currently paused). A normal scan then stamps new/changed files via the
batched path; `cairn stamp --corpus X --all` (or the button) backfills a chosen baseline.
**Rollback:** revert the commit and redeploy; existing `.ots` proofs stay valid and the per-file
loop still works.

## Open Questions

- Default batch size (256 is conservative; `cairn bench` / real runs may justify higher).
- The daily `upgrade` pass remains per-file; if it becomes a bottleneck it is a separate follow-up.
