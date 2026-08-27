# Design notes — tolerate un-writable proof output paths

## Where the write actually fails
The stamp path stages to a fixed-length hash name and renames to the real name:

```
<proof_store>/.staging/<uuid>.ots   →   <proof_store>/<collection_id>/<relpath>.ots
```

The staging name is always safe; only the **final** name is unbounded. `stamp_batch_via_symlink`
already decided per-member success by *filesystem truth* (did `<link>.ots` get produced), which
handles a member that yields **no** proof. But ENAMETOOLONG fires one step later — at
`os.replace(staged_ots, out)` — when the proof *was* produced but its destination name is too long.
That call was unguarded, so the exception escaped the whole batch. The fix guards exactly that move
(`_place_proof`) and adds a cheap up-front byte-length pre-check (`_proof_output_writable`) so an
un-writable member is skipped before a symlink or a calendar round-trip is wasted on it.

## Why skip-and-count instead of a safe alternate name
The DoD offered a richer option: derive a length-capped sidecar name (cap the base component and
append a short content hash, or move to a flat hash-named proof store keyed to the original path) so
the file can still be notarized. We deliberately **defer** it:

- **Conservatism on a live safety tool.** Skip-and-count is the minimal, obviously-correct fix and
  mirrors the existing precedent (`tolerate-unencodable-paths` reports-and-skips un-storable relpaths
  rather than inventing a reversible encoding). The skipped count is surfaced, so the gap is visible.
- **Layout stability.** `verify` / `export` / `upgrade` locate a proof via the stored `ots_path`, so
  a per-file alternate name would work — but it changes the store layout's 1:1
  `relpath ↔ <relpath>.ots` invariant and needs a deterministic, collision-free derivation and its
  own tests. That is a feature, not a crash fix.

If the un-writable population turns out to be non-trivial (real tax/legal files that genuinely need a
proof), the follow-up is a length-capped `proof_path`: keep the human-readable stem truncated to fit
`NAME_MAX - len(".ots") - len(<hash>)` bytes and append a short hash of the full relpath for
uniqueness, recorded in `ots_path` so verify/export are unaffected.

## Permanent vs transient, and why `none`
`stamp_pending` must not re-attempt a path the filesystem will always refuse. The state machine has
no dedicated "cannot stamp" terminal, but `ots_state='none'` (unstamped) already means "no proof",
and a normal scan only queues **added/changed** files — so dropping the skip to `none` takes it out
of the `pending` set without a new column. Only an explicit `stamp --all` re-queues `none` files, and
the byte pre-check makes that retry a cheap no-op (no calendar call). A transient failure stays
`pending`, unchanged, so a temporarily-unreachable calendar still retries next pass.

## Throttle disposition (from the incident)
The host saturation was the **crash-loop**, not cairn's own CPU: cairn scans and stamps strictly
sequentially (one collection → one file → one `ots` subprocess), and the deployment caps it at
`cpus: "2.0"` + `cpu_shares: 256`. `restart: unless-stopped` turned an uncaught stamp exception into
a restart-storm that re-ran scan-all-on-startup over the whole read-only tree, whose reads flooded a
co-located document-extraction pool. Removing the crash removes the loop — that is the cairn-side
throttle. A system-wide CPU/IO cgroup guardrail on the host is tracked separately in the
host-hardening work and is out of this repo's scope.
