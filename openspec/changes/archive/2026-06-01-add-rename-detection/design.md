# Design — content-addressed move/rename detection

## Context
The scanner (DESIGN.md §5) walks a corpus, diffs against `files` by `relpath`, hashes only what
size/mtime says changed, then sweeps for rows whose path vanished and marks them `missing`. Move
correlation is feasible because the added file's SHA-256 is already in hand; the only missing piece
is correlating it with a same-run `missing` row.

## Decisions

### Where it runs
A new reconciliation pass runs **after** the missing-sweep (`scanner.py:236-245`) and **before**
alert routing / stamp pass / run finalize. The missing side is only known after the full walk, and
the added side is produced during the walk — so correlation must wait until both sets exist. Both
sets are bounded by churn since the last scan, not corpus size, so the pass is cheap.

### Matching key & ambiguity
Match on `(sha256, size)`. Reconcile **only** when a `missing` file's key matches **exactly one**
`added` file *and* that key is shared by no other `missing` or `added` row in the run (strict 1:1).
Rationale:
- Duplicate content is common (identical photos; every empty file shares one hash). With N>1
  candidates the move target is ambiguous, and guessing risks attaching a file's proof/history to
  the wrong path — worse than a false `missing`.
- Zero-byte files are excluded explicitly (the 1:1 guard already covers the usual case).

Ambiguous and multi-match cases fall back to plain `missing` + `added` (today's behavior) and are
logged at INFO so the operator can see why a reorganization still alarmed.

### What the surviving row keeps
Reconcile by mutating the **existing (missing) row**: delete the just-created `added` row first (it
occupies the new path; `UNIQUE(corpus_id, relpath)` would otherwise collide), then repoint the
missing row's `relpath` to the new path, set `status='ok'`, refresh `last_checked`. The surviving
row **preserves** `first_seen`, `sha256`, `ots_path`, `ots_state`, `ots_stamped_at`. Net: identity,
proof, and notarization history follow the file to its new path.

### Events & counters
The would-be `missing` + `added` pair is replaced by one `moved` event (informational, like
`restored`), referencing the surviving `file_id` with the old → new path stored in a new nullable
`events.detail`. The run's `missing`/`added` counts exclude reconciled moves and a new `runs.moved`
count is incremented (audit trail + dashboard). `events.detail` is the minimal schema add that keeps
the move record honest without widening the events table further.

### OpenTimestamps
Because the surviving row keeps `ots_path`/`ots_state` and is left `ok` (not `pending`/`added`), the
existing proof over the unchanged content stays valid and the file is **not** re-queued by the stamp
pass. This is the main efficiency win and matches DESIGN.md §6 — content did not change, so no new
proof is owed. The `.ots` file is not renamed; `ots_path` already points at the proof.

### Read-only invariant
Reconciliation rewrites only the SQLite index (relpath, status, event rows). It never touches corpus
bytes (DESIGN.md §3 — corpora mounted read-only) nor the proof store files.

## Risks / trade-offs
- **Mis-reconciliation** would attach a file's proof/history to the wrong path. Mitigated by exact
  content match + size guard + strict 1:1 uniqueness; ambiguous cases never reconcile.
- **Move + content edit in the same window:** the edited file hashes differently, so it won't match
  the missing original → correct fallback to `missing` + `added` (it genuinely is a new content
  state owing its own proof).
- **Old path had an unacknowledged status** (e.g. previously `modified`): reconcile sets the
  surviving row `ok`, effectively resolving the stale nag for the old path. Documented behavior.
- **Performance:** one in-memory pass over `missing ∪ added`; no extra disk hashing. Negligible.
