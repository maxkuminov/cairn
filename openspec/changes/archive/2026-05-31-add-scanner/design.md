## Context

The scanner is the single writer to SQLite (DESIGN §3). It must scale to ~186k files / 1.4 TiB
without re-hashing everything every pass, and must be safe to run from the CLI now and from the
scheduler later. It mirrors the obsidian_mcp indexer's "diff by path, hash only changes" shape
but classifies for integrity instead of embedding.

## Decisions

### D1 — Two-tier change detection (fast-path then hash)
For a file present in both FS and DB, compare `size` and `mtime` first. If both match the stored
values, treat as unchanged (`ok`, update `last_checked`) and **do not hash**. If either differs
(or `sha256` is NULL), compute the SHA-256 and compare:
- hash differs → `modified` (worm) / silent re-baseline (churn);
- hash matches (only mtime moved) → `ok`, refresh stored `mtime`.
This keeps steady-state scans cheap; only genuinely-changed bytes are read.

### D2 — Streamed hashing
SHA-256 is computed by reading the file in fixed chunks (e.g. 1 MiB) so a multi-GiB file never
loads into memory. Hashing runs in a thread (`asyncio.to_thread`) so the event loop stays free
when the scanner is later driven from the async scheduler. Unreadable files (permission/IO) are
recorded as a scan error on the run, not crashes.

### D3 — Diff is set-based by relpath
Build the FS set (relpaths under root, minus `exclude_globs`) and the DB set (existing `files`
rows for the corpus). `added = FS − DB`, `missing = DB − FS` (rows whose status isn't already
missing get a `missing` event + status), `present = FS ∩ DB` (fast-path/hash). A row currently
`missing` that reappears in `present` → `restored`.

### D4 — Exclusions
`exclude_globs_json` is a JSON list of glob patterns matched against the POSIX relpath with
`fnmatch`/`PurePosixPath.match`. Patterns skip caches/temp and (for document corpora) the Obsidian
vault. Excluded paths are neither added nor counted as missing.

### D5 — WORM vs churn
`worm`: modified content → status `modified` + an unacknowledged `modified` event (nag). `churn`:
modified content → update stored `sha256/size/mtime`, status stays `ok`, **no** nag event (the
later notary still re-stamps churn changes). `missing` always produces an unacknowledged event in
both modes — a vanished file is always signal. `added` produces an `added` event in both modes
(informational; not itself an "issue" for the dashboard's open-issues count).

### D6 — Run lifecycle & batching
Open a `runs` row (`result='running'`) at scan start. Commit DB writes in batches (e.g. every
500 files) so a huge scan doesn't hold one giant transaction. On success set
`finished`/counts/`result='ok'`; on a caught error set `result='error'` (or `'partial'` if some
files processed) and still record counts. The scanner serializes per corpus (single writer).

### D7 — Accept semantics (nag-until-accept)
`accept(corpus)`: in one transaction, set `new`/`modified` files → `ok` (new baseline); delete
rows for `missing` files (accepted as gone); set `acknowledged_at=now`, `acknowledged_by=<user>`
on every unacknowledged event for the corpus. Idempotent: a second accept with nothing pending is
a no-op.

### D8 — Minimal corpus creation now; jailing later
`create_corpus(user, name, root, mode, ots_mode, cadence, excludes)` resolves `root` to an
absolute realpath and requires it to exist and be a directory. Root-jailing under an admin base
and per-user scoping are explicitly deferred to `add-multi-user`; in single mode the implicit
user owns the corpus and any readable directory is allowed. The resolved absolute path is stored.

## Risks / Trade-offs

- **mtime granularity / clock skew**: relying on size+mtime can miss a same-size, same-mtime
  in-place edit. Acceptable: WORM sets rarely edit; the periodic full re-hash option and OTS
  re-stamp on detected change cover the rest. A future "deep scan" can force-hash everything.
- **Very large corpora**: a full first scan of 1.4 TiB is expensive; that is a scheduling concern
  (nightly, staggered) handled in `add-scheduler`. The scanner itself is cadence-agnostic.
- **Symlinks**: the scanner does not follow symlinks at all — `os.walk(followlinks=False)` for
  directories and an explicit skip of symlinked files. This is the most conservative option: a
  symlink cannot be used to escape the read-only jail or to smuggle external bytes into a corpus's
  tracked set. (Hashing in-root symlink targets could be added later behind a flag if a real need
  appears; no spec scenario depends on symlink handling today.)
