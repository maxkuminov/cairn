## Why

Per-file OTS stamping runs one `ots stamp` subprocess — a network round-trip to every calendar —
for each file (measured ~0.3 stamps/sec). A `perfile` corpus with thousands of files therefore
never finishes stamping and its proof store stays effectively empty, defeating the notary
(DESIGN.md §5/§6). Separately, *which* files get stamped is implicit in the scan, which makes it
unclear how to (a) avoid stamping a large pre-existing baseline you never intended to notarize
(e.g. a 186k-file photo archive) while still stamping new arrivals, and (b) deliberately backfill
proofs for an existing set (e.g. tax records) on demand.

## What Changes

- **Batched stamping.** Stamp many files in a single `ots stamp <f1> … <fN>` invocation — one
  calendar round-trip yields **N independent per-file `.ots` proofs** (this is NOT a folder/manifest
  aggregate: every file keeps its own separately-verifiable proof). `proofs.stamp_pending()` chunks
  its work; a new batch function is added to `ots.py`; batch size is bounded by a new
  `CAIRN_OTS_STAMP_BATCH_SIZE` (default 256).
- **Failure isolation.** A batch that fails to produce a proof for some members falls back to
  stamping those members individually; a stamp problem still never fails a scan.
- **Explicit auto-stamp scope.** Automatic end-of-scan stamping covers ONLY files newly added or
  whose content changed in that scan. The pre-existing unstamped baseline (`ots_state = none`) is
  left untouched — so a `perfile` photo archive stamps new arrivals only, never its backlog.
- **On-demand "Stamp all".** A new `cairn stamp --corpus X [--all]` command and a panel control
  stamp every currently-unstamped file in a corpus (backfill). It SHALL NOT re-stamp files that
  already hold a proof for their current content.
- **Changed files (unchanged behavior).** A content change already re-queues the file; it is
  re-stamped on the next scan and the new proof **overwrites** the prior one (no version history —
  deliberate decision).

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `ots-notarization`: stamping MAY process multiple files per calendar submission (batched, still
  one proof per file) with per-file failure isolation; automatic stamping is scoped to newly
  added/changed files; an on-demand operation stamps all currently-unstamped files in a corpus.
- `web-panel`: the corpus view offers an owner/admin control to "Stamp all" unstamped files.

## Impact

- **Code**: `src/services/ots.py` (batch stamp function), `src/services/proofs.py` (chunked
  `stamp_pending`, plus a stamp-all selection that marks `none`-state files pending), `src/cli.py`
  (`stamp` command), `src/api/routes.py` + `src/control_panel/` (stamp-all endpoint + button),
  `src/config.py` and `.env.example` (`ots_stamp_batch_size`).
- **No DB schema change, no Alembic migration, no API change to verify/export/upgrade.**
- **External behavior**: far fewer OpenTimestamps calendar requests (~N× fewer) — minutes instead
  of days for a large corpus.

## Non-goals

- No folder/manifest aggregate proof — every file keeps its own independent `.ots` (DESIGN.md §5/§6).
- No version history on re-stamp — a changed file's new proof overwrites the old (explicit choice).
- No change to the `none | perfile` model, to `none` (tripwire) corpora, or to verify / export /
  the daily `upgrade` pass.
- No cross-process/thread stamping concurrency — batching alone removes the bottleneck.
