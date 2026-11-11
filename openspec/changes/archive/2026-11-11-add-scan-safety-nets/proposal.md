## Why

A scan decides what the operator hears about, and four of its decisions are wrong today
(coordination issue #64, follow-on to #62):

1. **An unreachable folder reads as "every file deleted".** If a collection's root is unmounted or
   comes up empty, the walk finds nothing, every tracked file is marked `missing`, one alarm is
   raised per file, and the run is recorded `ok`. Reproduced on a copy of real data: 4,672 files,
   4,672 alarms. The review page then offers "Accept 4,672 deletions" — one confirmed click from
   wiping the index, first-seen dates and proof links for the whole collection.
2. **Silent corruption is accepted without a word in Working (`churn`) collections.** A deep verify
   exists to catch bytes that changed while size and modification time did not. Under `churn` that
   finding is re-recorded as the new contents with no event and no alert (it is even a tested
   behaviour); under `worm` it is flagged, but as an ordinary change.
3. **A Working collection has no ceiling on change.** Every file rewritten in one night — a bad
   sync, an encryptor that keeps names — is accepted exactly like one edited note.
4. **The contents on record before a change are discarded at detection.** The scanner overwrites
   the stored digest the moment it sees a change. So restoring the original is reported as one more
   change, nothing but an accept can close the issue, and neither the event nor the review page can
   say what the file was.

This is DESIGN.md §5 "Per-run flow" and §3's WORM/churn decision: the policies keep their meaning;
what changes is that neither of them may absorb corruption, a disaster, or a missing volume.

## What Changes

- **A scan refuses an unreachable or empty root.** If the root is absent or not a directory, or the
  walk sees no file at all while the collection tracks files, the pass changes nothing: the run is
  recorded `error` with the reason, no file is marked missing, and one alert says the folder is
  unreachable (once per outage, not once per scan). `cairn scan --allow-empty` is the deliberate
  override for a collection that really was emptied.
- **Possible corruption is its own finding, under both policies.** A content change with size and
  modification time unchanged raises a new alarming event kind, `corrupted`, sets the file
  `modified`, and alerts — in `worm` and in `churn`.
- **A mass change in a Working collection is not absorbed.** When the changes a pass would accept
  silently reach both a minimum count and a share of the collection (defaults: 200 files and 25 %),
  all of them are listed as issues and alerted on instead.
- **The contents on record before an unaccepted change are kept** (digest, size, modification
  time) until the change is accepted. A file that returns to them closes its own issue — whether it
  was edited back, or deleted and restored from a backup of the original — and each change event
  records what the file was and what it became.
- **Panel copy follows the scanner**: possible corruption is labelled as such on the review row,
  a changed row says what the file's size was, the guidance says a changed file put back as it was
  clears itself, the Working description drops its "not yet flagged" caveat, a failed scan states
  its reason, and recent activity names the new kind.
- **BREAKING (stored data):** migration `0013` adds three nullable columns to `files` and the event
  kind `corrupted` (a rebuild of the `events` CHECK constraint).

## Non-goals

- **No path rules** (per-folder policies) and **no history views** — the next change. In particular
  an ordinary edit in a Working collection still writes no event.
- **No change to how a deletion is classified**: a missing file still raises a `missing` alarm
  under both policies, however many there are, as long as the root itself is reachable.
- **No automatic repair.** Cairn still never writes to a watched folder.
- **No change to stamping policy**: a changed file's new contents are queued for a stamp as today,
  including corrupted contents (each content state keeps its own proof; the earlier proof is
  archived, not destroyed).
- **The mass-change rule is evaluated at the end of a pass**, on what that pass saw. It is not a
  rate limiter across passes.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `integrity-scanning`: the mode requirement and the deep-verify requirement stop exempting
  corruption under `churn`; new requirements for the unreachable-root refusal, the possible-
  corruption finding, the mass-change ceiling, and keeping (and returning to) the contents on
  record before an unaccepted change.
- `alerting`: alerts cover possible corruption, a mass change and an unreachable folder.
- `web-panel`: the review row, the recovery guidance, the Working description, the failed-scan note
  and the activity summary follow the scanner.
- `datastore`: `files` gains the prior-contents columns; `events.kind` gains `corrupted`.
- `configuration`: two settings for the mass-change ceiling.

## Impact

- `src/services/scanner.py` — root check, corruption classification, prior-contents bookkeeping,
  revert detection, mass-change conversion, accept clears prior contents; `src/services/manifest.py`.
- `src/models/db.py`, `alembic/versions/0013_*` — schema.
- `src/config.py` — `mass_change_percent`, `mass_change_min_files`.
- `src/cli.py` — `cairn scan --allow-empty`, the scan summary line.
- `src/notify/` — alert summary wording; no channel change.
- `src/control_panel/` — `routes.py` (row facts, kinds, failed-scan reason), `partials/review_row.html`,
  `partials/review_help.html`, `learn.html`, `_macros.html`, `dashboard` activity kinds.
- Tests: `tests/test_scanner.py` (the `churn` bit-rot test changes meaning), new
  `tests/test_scan_safety_nets.py`, `tests/test_migration_0013.py`, panel tests for the copy.
- Docs: `README.md`, `DESIGN.md` §5, `CLAUDE.md`.
- Deploy: the migration applies on container start (`CAIRN_AUTO_MIGRATE=1`); the `events` rebuild
  touches ~130k rows on the reference deployment.
