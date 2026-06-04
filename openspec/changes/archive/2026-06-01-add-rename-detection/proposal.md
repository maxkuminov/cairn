# Add content-addressed move/rename detection

## Why
Cairn keys every tracked file solely on its path (`files` UNIQUE `(corpus_id, relpath)`) and
detects deletions with a post-walk "in the DB but not on disk" sweep (DESIGN.md §5, *Per-run
flow*). A file that is **moved or renamed** therefore reads as two unrelated changes: the old
path becomes `missing` and the new path becomes `added`. Three concrete harms follow:

- **False alarms.** A `missing` file is alarming in *every* mode — DESIGN.md §5 marks
  `missing → alert`, and the `alerting` spec dispatches on any newly-missing file. Reorganizing a
  folder (routine for the 186k-file Photos corpus) pages the operator even though no byte was lost.
- **Wasted notarization.** Because the new path looks first-seen, a `perfile` corpus queues and
  burns a fresh OpenTimestamps stamp (a calendar round-trip) for bytes that already hold a valid
  proof — DESIGN.md §6: proofs attest to *content*, not path.
- **Fragmented history.** `first_seen`, the existing `.ots` proof, and the event trail do not
  follow the file to its new path; the durable record is split across two rows.

The SHA-256 needed to recognize a move is **already computed** for the added file — the scanner
just never correlates it with the file that went missing in the same run.

## What Changes
- Add **content-addressed move/rename reconciliation** to the scan: after the missing-sweep, a
  file newly classified `missing` whose stored SHA-256 **and** size match **exactly one** file
  newly `added` in the same run (a key shared by no other candidate on either side) is reconciled
  as a **move**. One surviving `files` row carries the new `relpath` while preserving the original
  `first_seen`, `sha256`, and OTS proof (`ots_path`/`ots_state`/`ots_stamped_at`).
- A reconciled move emits a single informational **`moved`** event (new event kind, recording the
  old → new path) instead of a `missing` + `added` pair, **does not** raise an alarm, and is **not**
  re-queued for OTS stamping.
- Reconciliation is **conservative**: only unambiguous 1:1 content matches reconcile. Ambiguous
  cases (a hash shared by several missing and/or added files) and empty/zero-byte files fall back
  to today's `missing` + `added` behavior and are logged.
- Schema: add `moved` to the `events.kind` constraint, a nullable `events.detail` (old → new path),
  and a `moved` count on `runs`; one additive Alembic revision (SQLite batch rebuild for the
  CHECK change).

## Non-goals
- **Copy detection.** A copied file leaves the original in place (not missing), so it stays a
  genuine `added` file with its own proof. Out of scope.
- **Cross-corpus moves.** Reconciliation is scoped to a single corpus within one run; a file moved
  between corpora is not correlated.
- **Fuzzy / many-to-many matching.** No path-similarity heuristics, no partial-content matching —
  only exact `(sha256, size)` 1:1 matches.
- **Retroactive repair.** Existing `missing` + `added` pairs recorded by past scans are not
  reconciled; this applies to moves detected going forward.
- **Deep-scan changes.** Deep verify (re-hashing tracked files) is untouched; reconciliation only
  concerns the `missing`/`added` sets a scan produces.

## Impact
- **Affected specs:** `integrity-scanning` (new reconciliation requirement + amended
  classification), `alerting` (a reconciled move is not alarming), `datastore` (`moved` event kind,
  `events.detail`, `runs.moved`, migration).
- **Affected code:** `src/services/scanner.py` (reconciliation pass), `src/models/db.py`
  (event-kind constraint, `events.detail`, `runs.moved`), `alembic/` (new revision),
  `src/control_panel/` (events feed + dashboard counts), `src/notify/` (skip `moved`).
- **Data migration:** additive; existing rows untouched. `alembic downgrade` reverses the
  constraint/columns. Corpora with no moves behave exactly as today.
- **DESIGN.md:** extends the §5 per-run *classify* step and the §5 `events.kind` enum; consistent
  with §3 (corpora are read-only — reconciliation rewrites only the index, never the bytes) and §6
  (one proof per content state; a move is not a new content state).
