## Context

DESIGN locks "primarily wrap the maintained `ots` CLI (subprocess); subprocessing decouples us
from library API churn." The CLI surface (de-risked 2026-05-31):

- `ots stamp [-c URL ...] [--timeout T] [-m M] FILE` → writes `FILE.ots` beside FILE (no output
  flag). Immediately produces an *incomplete* proof carrying calendar PendingAttestations.
- `ots upgrade FILE.ots` → contacts calendars; if Bitcoin has confirmed, rewrites `FILE.ots`
  complete (old → `FILE.ots.bak`). Still-pending → exit 1, "Pending confirmation", file unchanged.
- `ots verify (-f FILE | -d DIGEST) TIMESTAMP` → exit 0 + "Success! Bitcoin block N attests …"
  when complete; exit 1 + "Pending confirmation …" when incomplete; mismatch → failure.
- `ots info FILE.ots` → offline dump; presence of `BitcoinBlockHeaderAttestation` ⇒ complete,
  else `PendingAttestation(<calendar>)` ⇒ incomplete.

## Decisions

### D1 — Stamp through a symlink in the writable store (read-only corpora untouched)
`ots stamp` writes next to its input, and corpus mounts are read-only. So: create a transient
symlink `<proof_store>/.staging/<uuid>` → the real corpus file, run `ots stamp` on the symlink
(it reads the real bytes, writes `<uuid>.ots` in `.staging`), then atomically move the `.ots` to
`<proof_store>/<corpus_id>/<relpath>.ots` and remove the symlink. Confirmed working: the stamped
digest equals the file's real SHA-256, and nothing is written under the corpus root.

### D2 — State machine maps the CLI to `ots_state`
`none` (not stamped) → `incomplete` (after stamp: calendar attestation, no Bitcoin yet) →
`complete` (after upgrade: Bitcoin attestation present). `pending` is the **queue marker** the
scanner sets to mean "needs stamping" before the `.ots` exists. `info()` classifies an existing
`.ots` offline (no network) by attestation type; `verify()`/`upgrade()` hit the network.

### D3 — Stamp is a separate step from hashing
The scanner already SHA-256s changed files; it does not stamp inline. Instead it sets
`ots_state='pending'` on new/changed files in `perfile` corpora, then calls `stamp_pending` once
per scan. This decouples cheap local hashing from slower calendar round-trips, lets a failed
stamp be retried next pass (stays `pending`), and lets the scheduler later run stamping/upgrading
independently. Re-stamp on content change: a modified file is re-queued `pending` and stamped
afresh (each distinct content state gets its own proof); the prior `.ots` is overwritten.

### D4 — Verify by digest, not by shipping the file
The panel/CLI verify re-hashes the file from the read-only store (or uses the stored `sha256`)
and runs `ots verify -d <digest> <proof>.ots`. Nothing leaves the host. The block-explorer
backend is the CLI default; `verify_backend=node` is recorded in config for a later refinement.
A `VerifyResult` captures: verified bool, state, Bitcoin block height + hash, the "existed by"
UTC date, calendars, and the raw message.

### D5 — Export bundle
`export_bundle(file, dest_dir)` copies the file bytes and its `.ots` together (named
`<basename>` and `<basename>.ots`) so a third party can `ots verify` independently. This is the
only place file bytes are copied, and only on explicit request.

### D6 — Robust subprocess handling
All `ots` calls go through one `_run_ots(args, timeout)` helper: captures stdout/stderr, applies
a timeout, and never raises on the "pending" non-zero exit (that is a valid state). Genuine
failures (calendar unreachable on stamp, malformed `.ots`) raise `OtsError` with the captured
output. Calendars come from `settings.ots_calendars` passed as repeated `-c` flags. Tests
monkeypatch `_run_ots` to return canned output, so the suite needs no network.

## Risks / Trade-offs

- **Double read on stamp**: stamping re-reads the file to hash it (the scanner already hashed it).
  Acceptable — stamping happens only on first-seen/change, not every scan, and (for the photo
  archive) only on newly-added files.
- **Calendar availability**: a stamp needs ≥M calendars to reply within the timeout. On failure
  the file stays `pending` and is retried next scan; surfaced via the run's `stamped` count.
- **Incomplete window**: a proof is fragile only while `incomplete`. `stale_incomplete(days)`
  surfaces proofs the calendars never confirmed so they can be alarmed/re-stamped.
- **Clock/`.bak` files**: `ots upgrade` leaves a `FILE.ots.bak`; we remove the `.bak` after a
  successful upgrade to keep the store clean.
