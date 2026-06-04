## Context

DESIGN §8: Cairn imports the tripwire's `manifest.tsv` into `files` under the Photos corpus, no
re-hash, "everything imported is pre-existing, don't stamp; only files first-seen after import get
per-file OTS." The exact on-disk column layout of the legacy manifest is not pinned in this repo,
so the importer must be tolerant rather than assume a fixed schema.

## Decisions

### D1 — Imported rows are an "already OK" baseline, which is exactly why they are never stamped
The scanner stamps only files it classifies `added` (first-seen) or content-`modified`. By
inserting imported files as `status='ok'` with their known `sha256` (and `ots_state='none'`), the
next scan matches them as unchanged and takes the fast-path or the "hash matches, metadata moved"
branch, neither of which stamps. No special "do not stamp" flag is needed; the no-stamp behavior
falls out of the existing classification. A genuinely new file has no imported row, so the scanner
sees it as `added` and stamps it (in a perfile corpus). This is the entire "stamp new photos only"
rule, achieved by data, not by a code branch.

### D2 — No `added` event on import
The import path inserts rows directly without writing `added` events, so the import does not flood
the dashboard with 186k "new file" events or alerts. The import is a baseline load, not a
detection.

### D3 — Tolerant, auto-detecting parser
For each non-blank, non-`#` line, split on tab first; if that yields one field, fall back to
splitting on runs of whitespace (handles `sha256sum`'s `<hash>  <path>`). Within the fields:
the field matching `^[0-9a-fA-F]{64}$` is the SHA-256; among the rest, purely-integer fields are
candidate size/mtime (larger magnitude is size in bytes; an epoch-like value is mtime, but both
are optional and only used to seed the fast-path), and the remaining (longest, may contain spaces
when whitespace-split is avoided by preferring tab) field is the relative path. A line without a
valid sha256 or a path is counted as skipped. This survives the common layouts
(`relpath\tsha256`, `relpath\tsize\tmtime\tsha256`, `sha256  relpath`).

### D4 — Idempotent upsert by (corpus_id, relpath)
The unique `(corpus_id, relpath)` constraint backs an upsert: an existing row is updated (hash,
size, mtime refreshed; status left `ok`), a new one inserted. Re-running the import is safe and
converges. Counts returned: `imported` (new rows), `updated` (existing), `skipped` (malformed).

### D5 — `--rehash` is an opt-in trust check, default off
Default import trusts the manifest (no disk reads, fast). With `--rehash`, the importer streams
each file under `corpus.root/relpath` through SHA-256 (reusing `scanner.sha256_file`) and records a
mismatch (manifest hash != recomputed) as a warning + a returned list, without aborting and
without changing the no-stamp rule. This is the one-time "is the baseline still trustworthy"
check from the tripwire. Missing files during `--rehash` are reported, not fatal.

### D6 — `first_seen` = import time
Imported rows get `first_seen = now` (the import moment). DESIGN's rule is about *detection* order:
imported files are the baseline (not stamped); files the scanner first sees later are stamped. Using
import time for `first_seen` is honest (Cairn first recorded them at import) and does not affect the
no-stamp behavior, which is driven by `status='ok'` + known hash, not by `first_seen`.

## Risks / Trade-offs

- **Unknown exact manifest format**: mitigated by the tolerant parser + the skipped-line count, so a
  surprising column order degrades to skipped rows (visible) rather than silently wrong data. If the
  real manifest needs a specific mapping, a `--columns` hint can be added later.
- **Trust without `--rehash`**: importing without re-hashing trusts the legacy manifest's integrity.
  That is the intended speed/΄safety trade (re-hashing 1.4 TiB is the thing we are avoiding); the
  `--rehash` option exists for the one-time verification Max can run opportunistically.
- **mtime/size absent**: if the manifest lacks them, imported rows seed `size=0`/`mtime=null`; the
  first scan backfills them via a single hash that matches and does not stamp. Acceptable one-time cost.
