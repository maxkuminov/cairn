## Why

The notary is Cairn's distinctive half: anchoring each file's SHA-256 to Bitcoin via
OpenTimestamps so the owner holds a portable "this file existed, unaltered, by date X" proof.
The scanner already detects what is new/changed; this change stamps those files, completes the
proofs after Bitcoin confirms, verifies them, and exports portable bundles — all while keeping
the watched corpus mounts read-only.

References: DESIGN.md §3 (OTS decisions), §5 (proofs.py, ots.py), §6 (OTS handling — stamp →
incomplete → daily upgrade → complete; verify needs file + `.ots` + block source). De-risked
2026-05-31: `ots stamp` writes `FILE.ots` beside the input (no `-o` flag), so we stamp through a
symlink in the writable proof store; `ots verify -d DIGEST TIMESTAMP` verifies by hash without
the original; `ots upgrade FILE.ots` upgrades in place.

## What Changes

- **OTS CLI wrapper** (`src/services/ots.py`): thin subprocess wrappers around the maintained
  `ots` CLI — `stamp_via_symlink(real_path, out_ots_path, calendars)`,
  `upgrade(ots_path) -> complete: bool`, `verify(ots_path, digest) -> VerifyResult`,
  `info(ots_path) -> ProofInfo` (offline parse of attestations → `none|incomplete|complete`,
  calendars, Bitcoin block height/hash). All decode the CLI's text output; pending proofs
  (exit 1, "Pending confirmation") are a normal state, not an error.
- **Proof store + lifecycle** (`src/services/proofs.py`): proof paths laid out as
  `<proof_store>/<corpus_id>/<relpath>.ots` on the writable volume; `stamp_pending(session,
  corpus)` stamps files the scanner queued (`ots_state='pending'`) → `incomplete`;
  `upgrade_incomplete(session)` upgrades `incomplete` proofs → `complete` once Bitcoin confirms;
  `export_bundle(file, dest)` writes the file bytes + its `.ots` for third-party verification;
  `stale_incomplete(session, days)` lists proofs stuck incomplete past the alarm threshold.
- **Scanner integration** (modifies `integrity-scanning`): in a `perfile` corpus the scanner
  marks newly `added` and content-`modified` files `ots_state='pending'` (a queue marker) and,
  at the end of a scan, runs `stamp_pending` so first-seen/changed files get stamped. `none`
  (tripwire) corpora are never stamped. The watched files are never written to.
- **CLI**: implement `cairn verify <file>` (re-hash from the read-only store + `ots verify -d`
  against the stored proof; print verdict, block, existed-by date), `cairn export <file> [--out
  DIR]` (write the portable bundle), and `cairn upgrade` (run the upgrade pass over all
  incomplete proofs; report how many completed and how many are stale).

### Out of scope (deferred)

- Scheduling the daily upgrade pass and staggered stamping — `add-scheduler` (this change exposes
  `upgrade_incomplete`/`stamp_pending`; the scheduler calls them on a cadence).
- The web Verify page and per-file OTS badges — `add-web-panel`.
- Alerting on stale-incomplete proofs — `add-notifiers` (this change provides the query).
- Self-hosted Bitcoin-node verify backend wiring beyond reading the config value (block-explorer
  default is the shipped path; node RPC is a later refinement).

## Capabilities

### New Capabilities

- `ots-notarization`: stamp a file's hash into a parallel proof store without touching the
  read-only corpus, upgrade incomplete proofs after Bitcoin confirms, verify a proof by digest,
  export a portable bundle, and flag proofs stuck incomplete past a threshold.

### Modified Capabilities

- `integrity-scanning`: `perfile` corpora queue new/changed files for stamping and stamp them at
  the end of a scan; `none` corpora remain tripwire-only.

## Impact

- **Code**: `src/services/ots.py` (new), `src/services/proofs.py` (new), edits to
  `src/services/scanner.py` (queue + stamp hook), `src/cli.py` (`verify`/`export`/`upgrade`).
- **Database**: writes `files.ots_path`, `files.ots_state`, `files.ots_stamped_at`, and the
  `runs.stamped` counter (the scanner's end-of-scan stamp pass). `runs.upgraded` is recorded by
  the scheduler's daily upgrade pass (`add-scheduler`); `cairn upgrade` reports counts directly.
  No schema change.
- **Filesystem**: writes only under the configured proof store (writable); never under a corpus
  root. Uses a `<proof_store>/.staging/` dir for the transient stamp symlink.
- **Dependencies**: the `ots` CLI (already installed in the image and pinned via
  `opentimestamps-client`).
- **Tests**: `tests/test_ots.py` — proof-state parsing from canned `ots info` output, the
  symlink-stamp output path, scanner queueing, and `export` — with the `ots` subprocess mocked so
  the suite needs no network; plus a documented, network-gated live-stamp smoke.
