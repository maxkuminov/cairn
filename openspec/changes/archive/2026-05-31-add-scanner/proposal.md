## Why

The foundation gives us a datastore and an app shell but nothing yet *watches* anything. The
core value of Cairn is detecting that a file was deleted, modified, or silently corrupted. This
change implements the scanner: the walk → diff → hash → classify loop that turns a corpus root
into `files` rows, `events`, and `runs`, plus the nag-until-accept lifecycle. It is the engine
the scheduler (later) drives and the panel (later) displays.

References: DESIGN.md §5 (per-run flow, schema), §8 (nag-until-accept lifecycle from the photo
tripwire), §7 (reuse the obsidian_mcp indexer's hash-based change-detection loop).

## What Changes

- **Scanner** (`src/services/scanner.py`): `scan_corpus(corpus)` walks the corpus root honoring
  `exclude_globs`, diffs the filesystem against the `files` table by `relpath`, fast-paths on
  `size`+`mtime`, computes a **streamed SHA-256** only for new/changed files (corpora reach 186k
  files / 1.4 TiB — never load a file into memory), and classifies each as
  `added` / `modified` / `missing` / `ok` / `restored`.
- **Events & runs**: writes an `events` row per detected change and a `runs` row per scan
  (started/finished, added/modified/missing counts, result). Unacknowledged events drive the
  nag-until-accept lifecycle.
- **WORM vs churn policy**: in `worm` mode a content change raises a nagging (unacknowledged)
  `modified` event; in `churn` mode a content change silently re-baselines the stored hash
  (no nag) since change is expected. **Missing files always nag, in both modes.**
- **Restored detection**: a file previously `missing` that reappears with matching content is
  classified `restored` (event kind `restored`, status back to `ok`).
- **CLI**: `cairn scan [--corpus NAME] [--once]` runs a scan for one or all corpora and prints a
  summary; `cairn accept [--corpus NAME]` re-baselines — sets `new`/`modified` files to `ok`,
  drops accepted-missing rows, and acknowledges their events. A minimal
  `cairn add-corpus --name --root [--mode] [--ots-mode] [--cadence] [--exclude ...]` creates a
  corpus owned by the implicit user so scans can run headlessly.

### Out of scope (deferred)

- OTS stamping/re-stamping on new/changed files — wired in `add-ots-notary` (the scanner exposes
  the seam: it records what changed; the notary acts on it).
- Scheduling/cadence/staggering and `/healthz` freshness — `add-scheduler`.
- The web panel views of files/events/runs — `add-web-panel`.
- Alert routing on events — `add-notifiers`.
- Root-jailing enforcement under an admin-provisioned base and multi-user scoping — `add-multi-user`.
- Importing the photo `manifest.tsv` — `add-manifest-import`.

## Capabilities

### New Capabilities

- `integrity-scanning`: the scan/diff/hash/classify engine, the run record, the event lifecycle
  (added/modified/missing/restored), WORM-vs-churn nag policy, and accept/re-baseline.
- `corpus-management`: minimal creation of a corpus (CLI) owned by a user, with a resolved
  absolute root that must exist and be a readable directory.

### Modified Capabilities

None. (Builds on `datastore`/`app-runtime` without changing their requirements.)

## Impact

- **Code**: `src/services/scanner.py` (new), `src/services/corpora.py` (create/list helpers),
  `src/cli.py` (implement `scan`, `accept`, `add-corpus`).
- **Database**: writes `files`, `events`, `runs`; no schema change (all columns exist from
  `0001_initial`).
- **Tests**: `tests/test_scanner.py` — a temp corpus over a temp dir exercising
  added/modified/missing/restored, fast-path no-rehash, WORM-vs-churn nag, and accept.
- **Docs**: mark `scan`/`accept`/`add-corpus` implemented in `CLAUDE.md`.
