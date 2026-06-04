## Why

A working bash photo-integrity tripwire protects Max's ~186k photos right now and must not
regress when Cairn takes over. Cairn reaches parity by importing the tripwire's existing
`manifest.tsv` baseline into the `files` table under the Photos corpus, WITHOUT re-hashing 1.4 TiB
and WITHOUT treating those long-existing files as brand-new. The whole "stamp new photos only"
rule hinges on this import being correct: everything imported is pre-existing (do not stamp); only
files first-seen after the import get a per-file OTS proof. DESIGN calls this a first-class, tested
step before the bash script is retired.

References: DESIGN.md §8 (migration from the photo tripwire; `manifest.tsv` at
`/srv/integrity/`), §1 (the silent-loss fear the tripwire guards), §10 (Phase 1
parity).

## What Changes

- **Manifest importer** (`src/services/manifest.py`): `import_manifest(session, corpus, path,
  rehash=False)` reads the tripwire manifest and upserts one `files` row per entry into the target
  corpus as a pre-existing baseline. Imported rows carry the manifest's SHA-256, get
  `status='ok'`, `ots_state='none'`, and `first_seen` set to the import time, and do NOT produce an
  `added` event. Because they are `ok` with a known hash, the next scan recognizes them as
  unchanged and never stamps them. Genuinely new files (first seen after the import) are classified
  `added` by the scanner and stamped per the corpus's `ots_mode` as usual.
- **Tolerant TSV parser**: the parser auto-detects columns so it survives the exact on-disk format.
  It recognizes the 64-hex SHA-256 field, treats the longest remaining field as the relative path,
  and reads optional integer size / mtime fields when present. It also accepts `sha256sum`-style
  `<hash>  <path>` lines. Blank/comment/malformed lines are skipped and counted, not fatal.
- **Idempotent re-import**: importing the same manifest again updates existing `(corpus, relpath)`
  rows rather than duplicating them, so a re-run is safe.
- **Optional `--rehash`**: off by default (the whole point is to avoid re-reading 1.4 TiB). When
  on, the importer recomputes each file's SHA-256 from disk and warns on any mismatch with the
  manifest (a one-time trust check), without changing the no-stamp rule.
- **CLI**: `cairn import-manifest --corpus NAME --file PATH [--rehash]` runs the import and prints
  imported / updated / skipped counts (and mismatches when `--rehash`).

### Out of scope (deferred)

- Retiring the bash script / cron / Kuma monitor (an operational step Max performs once parity is
  confirmed, not code here).
- Importing the tripwire's `pending-deletions.tsv` acknowledgement state (the panel's
  accept/acknowledge flow supersedes it).
- Auto-discovery of the manifest path from the host (the path is passed explicitly).

## Capabilities

### New Capabilities

- `manifest-import`: import a photo-tripwire `manifest.tsv` into a corpus as a pre-existing,
  unstamped baseline, with a tolerant auto-detecting TSV parser, idempotent re-import, and an
  optional re-hash trust check.

### Modified Capabilities

None. (Relies on the scanner's existing rule that only `added`/`modified` files are stamped; the
import deliberately creates `ok` rows so they are not.)

## Impact

- **Code**: `src/services/manifest.py` (new), `src/cli.py` (the `import-manifest` subcommand).
- **Database**: inserts/updates `files` rows in the target corpus. No schema change.
- **Tests**: `tests/test_manifest.py` — import sets `ok`/`none`/manifest-hash with no `added`
  events; a subsequent scan does NOT stamp imported files but DOES stamp a genuinely-new file in a
  perfile corpus; re-import is idempotent; the parser handles tab and `sha256sum`-style lines and
  skips malformed ones; `--rehash` flags a mismatch.
