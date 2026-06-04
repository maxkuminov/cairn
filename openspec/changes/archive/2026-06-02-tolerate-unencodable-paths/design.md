# Design — tolerate non-UTF-8 filenames and never wedge a scan run

## Context
`os.walk` on Linux returns `str` paths decoded with the filesystem encoding (UTF-8) using the
`surrogateescape` error handler, so a byte that is not valid UTF-8 becomes a lone surrogate
(`\udcXX`) in the string. Every *filesystem* call (`Path.stat`, `open`, the SHA-256 stream)
re-encodes with `surrogateescape` and works fine. The *database* is the only thing that breaks:
Python's `sqlite3` binds `str` via plain UTF-8, and a lone surrogate is not encodable
(`UnicodeEncodeError: surrogates not allowed`), so the row write fails. SQLite TEXT is defined as
valid UTF-8/UTF-16 — there is no "store arbitrary bytes as TEXT" path.

## Decision 1 — skip, count, and log un-storable paths (not reversible-encode, yet)
At the top of the walk loop, gate each relpath on `relpath.encode("utf-8")` succeeding. If it does
not, skip the file: `summary.errors += 1` (so the run finishes `partial`), accumulate a capped
sample of the raw `os.fsencode(relpath)` bytes, and emit one batched `WARNING`. No `files` row is
created.

Why skip rather than store a reversible encoding now:
- **Correctness of the no-op case.** A lossy store (`errors="replace"` → `1�.jpg`) would make
  the real file `1\udce0.jpg` unmatchable on the next scan: the stored path never reappears on disk
  (→ perpetual `missing`) while the real file is perpetually re-`added`. That manufactures the exact
  churn and false alarms we are trying to remove. Lossy storage is strictly worse than skipping.
- **A faithful reversible encoding is a real design change.** To actually track the file we must
  persist a form that is valid UTF-8 *and* round-trips to the original bytes for re-`open` — e.g.
  percent-escaping only the surrogate bytes (and the literal `%`). That touches the `relpath`
  contract everywhere it is consumed: the tree-browse `LIKE` prefix queries, `root / relpath`
  filesystem access, OTS stamp/verify/export, event `detail`, and template rendering. It deserves
  its own change with its own tests, not a rider on an incident fix.
- **Scope is one file in 188k.** The urgent harm is the *wedge* (a whole corpus un-monitored), not
  the one untracked photo. Skipping removes the wedge immediately; the skipped count is surfaced
  (run `partial` + log), so the residual gap is explicit, not silent.

Follow-up (out of scope, noted for the backlog): a reversible relpath encoding (or a corpus-level
"normalize names" tool) to bring such files under monitoring.

## Decision 2 — a scan always reaches a terminal run state
The wedge is not unique to bad filenames: *any* exception escaping the scan body after the run row
is committed `running` can leave it stuck, because the broken session makes the finalizing commit
fail too. So independently of Decision 1, harden finalize:

- The scan body's `except Exception` now logs and `await session.rollback()`s, clearing the
  pending-rollback state so the run row (already committed `running` up front) can be moved to a
  terminal state.
- The finalizing `await session.commit()` is wrapped: on failure it rolls back and issues a direct
  `UPDATE runs SET result='error', finished=now WHERE id=:id` in a clean transaction. A scan can no
  longer leave a corpus blocked until the next restart.

This complements — does not replace — `scheduler.reap_orphaned_runs` (which only fires at startup,
for runs orphaned by a hard process kill). With both, an in-process failure self-heals immediately
and a crash heals on the next start.

## Alternatives considered
- **Prune at `iter_relpaths`.** Filtering bad names inside the generator hides them from the
  classifier but also from the error/skip accounting; keeping the gate in `scan_corpus` lets the
  same place that owns the run summary own the skip count and the log.
- **`errors="replace"` on store.** Rejected — manufactures `missing`/`added` churn (above).
- **Bytes/BLOB relpath column.** A schema change that ripples through every relpath consumer;
  deferred to the reversible-encoding follow-up.

## Risks
- A corpus with such a file now reports `partial` forever (until the follow-up tracks it or the name
  is fixed). That is accurate — the scan genuinely could not cover every file — and `partial` keeps
  the dead-man's switch fresh (`compute_health` treats `ok`/`partial` alike), so it does not mask
  real staleness. The batched log makes the cause discoverable.
