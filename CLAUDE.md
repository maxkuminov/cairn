# Cairn

Self-hosted **file-integrity monitor + OpenTimestamps notary** with a multi-user web panel.
Watches configured file sets ("collections") for deletion / modification / corruption and anchors
file hashes to Bitcoin via OpenTimestamps for trustless "existed-by-date" proofs.

> **Phase 1 in progress (built via OpenSpec).** Shipped & archived changes:
> `add-foundation` (config/DB/models/migrations/app/CLI + Docker/`make deploy`), `add-scanner`
> (walk→diff→hash→classify + accept), `add-ots-notary` (stamp/upgrade/verify/export), `add-scheduler`
> (per-collection cadence + daily upgrade + `/healthz` freshness), `add-web-panel` (the Slate panel).
> See `openspec/specs/` for the live capabilities and `openspec/changes/archive/` for history.
> [`DESIGN.md`](./DESIGN.md) is the build spec; this file is working notes. Sibling reference
> codebase mined for patterns/code: **the sibling FastAPI app** (same shape of app).

## Stack (planned)
- Python 3.12 / FastAPI / uvicorn
- SQLAlchemy async + **SQLite** (single file, WAL mode) — `aiosqlite`. No DB service to run.
- Alembic migrations
- Jinja2 + htmx + Tailwind control panel (server-rendered, minimal JS)
- OpenTimestamps via the maintained **`ots` CLI** (subprocess); `opentimestamps[-client]` pinned
- pydantic-settings for config

## Project layout (planned)
- `src/main.py` — FastAPI app, lifespan (starts the scan scheduler), mounts panel + api
- `src/config.py` — pydantic-settings (`CAIRN_AUTH_MODE`, paths, calendars, verify backend…)
- `src/database.py` — async SQLAlchemy engine/session; sets SQLite `WAL` + `foreign_keys` pragmas
- `src/models/db.py` — ORM: `users`, `collections`, `files`, `runs`, `events`
- `src/auth/` — session login, password hashing, login/register routes (lift from obsidian_mcp)
- `src/services/scanner.py` — walk → diff → hash changed → classify (added/modified/missing)
- `src/services/ots.py` — stamp / upgrade / verify (wraps the `ots` CLI)
- `src/services/scheduler.py` — per-collection scan cadence (staggered) + daily OTS upgrade + heartbeat
- `src/services/proofs.py` — parallel `.ots` store + `export` bundles
- `src/notify/` — smtp, signal_callmebot, webhook, ntfy, kuma_push
- `src/witness/restic.py` — optional independent-witness check (`restic backup --force` + `diff`)
- `src/api/routes.py` — REST/htmx endpoints for the panel
- `src/control_panel/` — Jinja2 templates + static assets
- `src/cli.py` — `init / scan / accept / verify / export / status / upgrade / add-collection / serve`
- `alembic/` — migrations

## Key decisions (see DESIGN.md §3 for full rationale)
- **Python, not Node** — the maintained OTS tooling is Python (`opentimestamps-client` v0.7.2,
  2024-12-31); the JS lib is abandoned (v0.4.9, 2021, CI on EOL Node 6/7). Plus reuse of the
  obsidian_mcp codebase and one stack on the host.
- **SQLite, not Postgres** — a safety tool must run without a DB service; trivial self-host
  install; the DB is just an index (the guarantee is bytes + `.ots` proofs). Scanner is the
  single writer; WAL mode keeps panel reads concurrent.
- **Web-panel-first, dual-mode** — `CAIRN_AUTH_MODE=single|multi`. Multi-user = login + admin,
  each user scoped to their own collections.
- **Watched folders mounted read-only**; DB + proof store on a separate read-write volume. Cairn
  cannot modify/delete what it watches. Each user's collection roots are jailed under an
  admin-provisioned base mount.
- **OTS per-file** where proofs may be shown externally; stamp on first-seen, re-stamp on change,
  daily `upgrade` pass. Verify defaults to a block-explorer lookup, configurable to a Bitcoin node.
- **App owns its own auth** → no external OAuth proxy needed (unlike the obsidian_mcp panel).

## What to reuse from the sibling FastAPI app
- Auth/session/password code and the `User`/admin/per-user-scoping pattern (drop the API-key &
  OAuth2/PKCE layers — MCP-specific).
- The indexer's "run on startup, then on a cadence, hash-based change detection" loop → becomes
  our scanner. Atomic `write_file`, CSRF, rate-limiting, timing-safe compares.
- Docker + reverse-proxy + Makefile-deploy + "host paths live outside the public tree" discipline
  + OpenSpec workflow.
- **Do not** copy the Postgres types (JSONB/ARRAY/TSVECTOR/pgvector) — Cairn uses plain columns +
  JSON blobs, SQLite-friendly.

## Commands (status tracked as built)
- `cairn init` — **implemented** (add-foundation): create `data/`+`proofs/` dirs, migrate the DB to head (WAL).
- `cairn serve` — **implemented** (add-foundation): run the web panel (uvicorn `src.main:app`).
- `cairn add-collection --name --root [--mode] [--ots-mode] [--cadence] [--verify-cadence] [--exclude ...]` — **implemented** (add-scanner; `--verify-cadence` from add-deep-verify).
- `cairn scan [--collection X] [--once]` — **implemented** (add-scanner): walk→diff→hash→classify, write events+run.
- `cairn accept [--collection X]` — **implemented** (add-scanner): re-baseline (new/modified→ok, drop missing, ack events).
- `cairn verify <relpath> [--collection X]` — **implemented** (add-ots-notary): re-hash + `ots verify -d` the stored proof.
- `cairn export <relpath> [--collection X] [--out DIR]` — **implemented** (add-ots-notary): portable file + `.ots` bundle.
- `cairn upgrade` — **implemented** (add-ots-notary): upgrade incomplete proofs; warn on stale-incomplete.
- `cairn stamp [--collection X] [--all]` — **implemented** (decouple-ots-stamping): stamp the already-`pending`
  set (decoupled from a scan); `--all` first queues every unstamped non-missing file (`ots_state=none`)
  and backfills it. Batched (one `ots stamp` call per `CAIRN_OTS_STAMP_BATCH_SIZE` files); never
  re-stamps `incomplete`/`complete`. `perfile` collections only.
- `cairn bench [--path DIR] [--bytes N] [--estimate]` — **implemented** (add-deep-verify): measure local
  SHA-256 throughput (in-memory probe or real files under `--path`); `--estimate` prints per-collection
  deep-scan ETA (total size ÷ throughput). Read-only.
- `cairn import-manifest --collection X --file PATH [--rehash]` — **implemented** (add-manifest-import):
  import the photo-tripwire `manifest.tsv` as a pre-existing, unstamped baseline (parity, DESIGN §8).
- `cairn status` — _planned_.

> OTS notary (add-ots-notary): per-file stamps land in the writable proof store
> `<proof_store>/<collection_id>/<relpath>.ots` (collection mounts stay read-only — stamped via a symlink
> in `<proof_store>/.staging`). `perfile` collections stamp new/changed files at end of scan; `none`
> = tripwire only. `ots` binary resolved next to `sys.executable` then PATH (`CAIRN_OTS_BIN` overrides).
> Stamping is **batched** (decouple-ots-stamping): `proofs.stamp_pending` chunks `pending` rows into
> `CAIRN_OTS_STAMP_BATCH_SIZE` (default 256) groups, each stamped in one `ots stamp <f1>…<fN>` call —
> one calendar round-trip, still N independent per-file `.ots`. A member that yields no proof falls
> back to a single-file stamp (per-file failure isolation; a stamp never fails a scan). Auto-stamp
> covers only files that scan added/changed; the pre-existing `none` baseline is left alone. Backfill
> it on demand with `cairn stamp --collection X --all` or the collection-view "Stamp all" button (perfile only).

> Scheduler (add-scheduler): `cairn serve` runs a background loop that scans each collection on its
> staggered `hash_cadence_seconds` (scan-all on startup) + a daily OTS upgrade pass. `/healthz` now
> reports per-collection scan freshness: 200 `ok` (reachable + fresh), 503 `degraded` (a collection is
> stale — dead-man's switch), 503 `error` (datastore down). Disable the in-process loop with
> `CAIRN_SCHEDULER_ENABLED=0` for cron-only (`cairn scan --once`) deployments.

> Deep verify (add-deep-verify): the normal scan fast-paths on size+mtime, so silent bit-rot (bytes
> change, size+mtime don't) goes unseen. `scan_collection(..., deep=True)` re-hashes every tracked file
> to catch it; classification is unchanged (intact files stay `ok` and are never re-stamped). Per
> collection `verify_cadence_seconds` (default weekly `604800`, `0` = off, on the collection row + form
> "Deep verify" select); `last_full_scan_at` is the wall-clock of the last deep pass. The scheduler
> runs a deep pass when owed, replacing that tick's quick pass, capped to one deep pass per tick so a
> long re-hash can't starve the fleet. `runs.deep` marks deep runs. Estimate cost with `cairn bench`.

> Alerting (add-notifiers): `src/notify/` — SMTP active; webhook/ntfy/signal_callmebot/kuma_push
> scaffolded. A scan dispatches one batched best-effort alert per collection when it NEWLY detects a
> `missing` file (any mode) or a WORM `modified` file (churn re-baselines + `added` don't alert);
> routing is per-collection `alert_json` (`{"email":{"enabled":true,"to":[...]}}`). Dispatch is
> post-commit and can never fail a scan. The **SMTP server** config (host/port/TLS/user/password/from)
> is editable from the panel (Settings → Notifications, admin-only) and persisted in the new
> `app_settings` key-value table; `src/services/app_settings.py` overlays those rows over the env
> `CAIRN_SMTP_*` defaults (**DB wins**, empty table = pure env fallback) via
> `effective_settings(session, get_settings())` at the scanner's dispatch site — no restart, no cache
> bust. A "Send test email" button (`POST /settings/smtp/test`) verifies the config. The SMTP
> password lives in the DB (homelab choice; a departure from "secrets via env only"). Follow-up:
> source the scaffolded Signal CallMeBot key from env (not `alert_json`) before enabling that channel.
> **Alert deep links (add-alert-deep-links):** an alert now carries `Alert.url` — a one-click link to
> `{public_url}/collection/{id}/review`, so the reader lands on the missing/modified list with the
> acknowledge/accept/copy-paths controls in reach instead of "review it in the panel" with no address.
> The address is the new optional **`public_url`** setting (`CAIRN_PUBLIC_URL`, or **Settings →
> Notifications → Panel address**, admin-only, stored in `app_settings` with the usual DB-wins-over-env
> overlay; saving empty *deletes* the row so the env value reappears). The grammar and the single link
> builder live in `src/services/panel_url.py` (`normalize_public_url` / `panel_link`) — never
> string-concatenate a URL at a call site; the value is normalized to pure ASCII (IDNA host,
> percent-encoded path, RFC-1123 host syntax) so it is safe in an HTML `href` and in ntfy's `click`
> field. Unset ⇒ `url=None` and every channel emits byte-identical
> output to before. **Validation is fail-soft at load, fail-loud on save:** a bad `CAIRN_PUBLIC_URL`
> is logged and treated as unset (raising in `get_settings()` would stop startup, scanning and every
> alert over a cosmetic setting), while the panel refuses it inline; a stored override is
> re-validated on *every* overlay read, since `model_copy(update=...)` skips validators. At the
> scanner's dispatch site the settings lookup + link build sit in their **own nested `try/except`**
> inside the existing best-effort block — any failure yields a link-free alert that is still
> constructed and still dispatched (env settings as the transport fallback): a convenience must never
> sit on the path deciding whether the operator is told. Settings' health-monitoring URL is derived
> from the same setting, falling back to a visibly-labelled example. **Accepted limitation:** the
> review page is owner-scoped (`_get_owned_collection` 404s for a non-owner, deliberately not
> disclosing existence) and alert recipients are arbitrary addresses, not Cairn users — so in `multi`
> mode a non-owner recipient reaches "not found". Hinted in the collection alert-routing form;
> mapping recipients to users is Phase-2 auth work, and weakening the scoping is the wrong trade.
> **Post-audit hardening** (adversarial review of the implementation found four live
> false-negative paths, all in the same shape — a detected incident committed, then the notification
> silently swallowed by `dispatch`'s blanket `except`): (1) the settings-overlay load and the link
> build now sit in **two separate** guards, because a deployment that configures SMTP *from the
> panel* keeps host/user/password in `app_settings` — collapsing to env settings on a link failure
> left `smtp_host=None` and the live email channel sent nothing. A successful overlay now survives a
> link failure. (2) **ntfy now publishes a JSON body** (`{topic,title,message,priority,tags,click}`)
> instead of HTTP headers: httpx ASCII-encodes header values, so an ordinary collection name like
> `Café` raised `UnicodeEncodeError` — not an `httpx.HTTPError`, so it escaped `send()` and the
> notification was never sent. No user-influenced value rides in a header any more, so the encode
> failure *and* the newline-injection vector are gone by construction, not by filter. (3) mail
> headers (`Subject`/`From`/`To`) are control-character-sanitized — a CR/LF in a collection name or
> a pasted recipient made `EmailMessage` raise and killed the mail on the active channel; non-ASCII
> is left alone (RFC 2047 handles it). (4) `normalize_public_url` validates host syntax (RFC-1123
> labels or an IP literal), so `https://exa%mple.com`, `https://-` and `https://.` no longer pass as
> valid and become dead links.
> Deploy auth caveat: DESIGN says "app owns its own auth, no OAuth proxy" — that only holds in
> `multi` mode (Phase 2). In **single mode the panel has no login wall**, so the homelab deploy
> fronts it with Traefik `chain-oauth@file` (Google OAuth), with `/healthz` kept public on a
> higher-priority router for the Uptime-Kuma poll. Drop the middleware once multi-user login ships.

> Event acknowledgement (streamline-event-acknowledgement): only the alarming kinds nag —
> `missing` (both modes) + worm `modified`. The informational kinds `added`/`restored` are written
> **already acknowledged** by the scanner (`acknowledged_at` set, `acknowledged_by` NULL = system
> ack), so a routine new+stamped file appears in the dashboard feed without inflating "N need
> action". The dashboard has a bulk **"Acknowledge all"** control (`POST /events/ack-all`, CSRF,
> scoped to the current user's collections) that clears every open event and refreshes the feed + "need
> action" pill + sidebar badge in place; it is **ack-only** (no file re-baseline — that stays
> `accept`). The feed render is factored into `_event_feed()` (reused by the dashboard + the
> ack-all route); the pill + button live in `partials/_events_controls.html` so single-ack and
> ack-all OOB swaps keep them in sync. Migration `0004` backfills existing `added`/`restored` acks.

> Rename detection (add-rename-detection): a moved/renamed file used to read as two unrelated
> changes (old path `missing` → false alarm, new path `added` → a wasted re-stamp + split history).
> The scanner now runs a content-addressed reconciliation pass (`_reconcile_moves`) after the
> missing-sweep, before alerts/stamp/finalize: a candidate-`missing` row whose `(sha256, size)`
> matches **exactly one** newly-`added` row — a key shared by no other missing/added row in the run
> (strict 1:1; zero-byte files excluded) — is the same file relocated. It's reconciled in place
> (delete the added row to free its path, repoint the surviving row's `relpath`, set `ok`) so
> `first_seen`/`sha256`/`ots_*` follow the file; one informational **`moved`** event (born
> acknowledged, `events.detail` = "old → new") replaces the missing+added pair, and `runs.moved`
> counts it (surfaced in the dashboard "Last activity" tile + event feed). A move never alarms and
> is never re-stamped (surviving row stays `ok`, not `pending`; the `pending` added row is deleted
> before the stamp pass). Ambiguous/multi-match cases fall back to plain `missing`+`added` (logged
> at INFO). Migration `0005` adds the `moved` event kind (SQLite batch rebuild of the CHECK),
> `events.detail`, and `runs.moved`. Out of scope: copies, cross-collection moves, fuzzy matching,
> retroactive repair of pairs from past scans.

> Folder tree + typed progress runs (add-folder-tree-and-scan-progress): a `runs` row is now a
> **typed, progress-bearing** record — `runs.kind` (`scan`|`stamp`|`upgrade`, default `scan`, CHECK),
> `runs.processed`, `runs.total` (nullable; migration `0006`, batch rebuild + backfill `kind='scan'`).
> A scan sets `total` = the last completed `kind='scan'` run's `processed` (estimate; first scan →
> NULL → indeterminate; never `count(*)`), writes `processed` per `_drain`, and **commits the
> `running` run up front** so the concurrency guard + badge see it immediately. Freshness
> (`compute_health` + `_collection_view` "last scan") now keys on `kind='scan'` **only**, so the daily
> upgrade pass records a real `kind='upgrade'` run (`proofs`-counted, progress threaded via a
> callback) instead of the old "amend the latest scan run" workaround — and a `stamp`/`upgrade` run
> can't refresh the dead-man's switch. The on-demand stamp-all is `proofs.run_stamp_backfill` (a
> `kind='stamp'` run, `total` = pending count). **"Scan now" and "Stamp all" are async**: routes
> launch `_run_operation(collection_id, op)` in its own session via `asyncio.create_task` (module-level
> `_BG_TASKS` ref so it isn't GC'd) and return the live badge fragment immediately. **One op per
> collection**: `collections.active_run()` is the single guard — routes refuse a second op, the scheduler
> skips an in-flight collection (scan + upgrade passes). A startup **reaper** (`scheduler.reap_orphaned_runs`,
> called in the lifespan) marks any leftover `running` run `error` so a crash never freezes a badge.
> Panel: collection detail has a **Tree ⇄ List** toggle (tree default); the tree is one directory level
> per request from `relpath` in SQL (`collections.browse_tree` for grouped subfolders + counts/issue
> roll-up; `query_files(prefix=…)` for immediate files, anchored `LIKE` escaped, paginated) — never
> materializes the full set. The live badge (`partials/op_status.html`, polls `GET
> /collection/{id}/op-status` every 4s while running; idle → resting pill, no poll, and an `HX-Refresh`
> on the running→done transition) shows on the dashboard card + collection status pill.

> Non-UTF-8 filenames + terminal runs (tolerate-unencodable-paths): a single file with a non-UTF-8
> name froze a whole collection. `os.walk` surfaces such a name as a lone surrogate (`\udcXX`); the FS
> ops accept it but SQLite can't bind it as TEXT, so the batch commit in `_drain()` raised
> `UnicodeEncodeError`, the broken session failed the finalizing commit too, and the run stayed
> `running` — `collections.active_run()` then refused every later scan (Photos was wedged on `…/1à.jpg`;
> `/healthz` showed a dead-man's-switch **false** `degraded`). Two fixes in `scanner.py`, **no schema
> change**: (1) `_db_storable(relpath)` gates each path on round-tripping through UTF-8 at the **top
> of the walk loop** — a non-storable name is skipped before any row is created (`summary.errors += 1`
> → run `partial`, one batched `WARNING` with the raw `os.fsencode` bytes); no row means no
> `missing`/`added` churn across scans, and the file is **reported-and-skipped, not tracked/stamped**
> (faithful reversible-relpath encoding is the deferred follow-up). (2) A scan now **always reaches a
> terminal run state**: the scan-body `except` `rollback()`s the session before finalizing, and the
> finalizing commit has a last-ditch `UPDATE runs SET result='error', finished=… WHERE id=run_id`
> fallback — so no in-process failure can leave a collection perpetually `running` (complements the
> startup reaper, which only fires on restart). The currently-wedged Photos run clears via that reaper
> on deploy/restart; the next Photos scan finishes `partial`. A collection with such a file reports
> `partial` forever (accurate; `compute_health` treats `ok`/`partial` alike, so the switch stays fresh).

> OTS off the event loop (offload-ots-subprocess): the `ots` CLI is invoked via synchronous
> `subprocess.run` (`ots._run_ots`), and the async callers ran it **directly on the single asyncio
> event loop** — so a large pass froze the panel (the daily upgrade over 28,632 `incomplete` proofs
> pegged a core and made the dashboard take ~20s; `/healthz` flapped `degraded`). Fix mirrors the
> scanner's `asyncio.to_thread(sha256_file)`: every blocking OTS/IO call reachable from the loop is
> now `await asyncio.to_thread(...)`-ed — `proofs.stamp_pending` (batched + per-file fallback),
> `proofs.upgrade_incomplete` (`ots.upgrade`), and the panel `/verify` (re-hash + `ots.verify`) and
> `/export` (`export_bundle` copy) routes. Calls stay **sequential** (one `ots` subprocess at a time
> → shared `.staging` dir + calendar rate unchanged); only the blocking thread moves off the loop.
> **No schema change.** Two tests assert the work runs on a non-main thread. **Known follow-up (not
> fixed here):** the scheduler still `await`s `run_due_scans` then `run_daily_upgrade` **inline** per
> tick, so a multi-hour upgrade still postpones the next scan tick → a collection can briefly read
> `stale` (transient false `degraded`). Decoupling those passes is structural (own task / cap per
> tick), left as its own change. CLI `verify`/`scan`/`import-manifest` are one-shot processes (no
> shared loop) and are intentionally left synchronous.

> Verify via block explorer (inline fix, no openspec): the design says "verify defaults to a
> block-explorer lookup, configurable to a Bitcoin node", but `ots.verify()` only ever ran
> `ots verify -d`, and the maintained `ots` CLI (v0.7.2) can ONLY verify against a Bitcoin Core
> node — so on the homelab host (no `bitcoind`) **every complete proof failed** with "Could not
> connect to Bitcoin node". `ots.verify()` is now a dispatcher: `backend="explorer"` (default)
> parses the `.ots` with the `opentimestamps` library and confirms each `BitcoinBlockHeaderAttestation`'s
> commitment equals the real block's merkle root, fetched from an esplora-compatible explorer
> (`CAIRN_EXPLORER_URL`, default `blockstream.info`: `/api/block-height/<n>` → `/api/block/<hash>`,
> merkle root reversed to internal byte order); the earliest matching block time is "existed by".
> A merkle mismatch or a changed file digest reads **not-verified** (never a false positive); an
> unreachable explorer is not-verified with the network error. `backend="node"` keeps the old
> `ots verify -d` path and now forwards `--bitcoin-node <node_rpc_url>`. Both the panel `/verify`
> route and `cairn verify` pass the configured backend/explorer/node. Verification trusts the
> explorer's canonical block at a height (the acknowledged, less-trustless default; point at a node
> for full trustlessness). Also: file-browser rows reflow on mobile (`≤768px`) from the fixed
> 5-column grid into a stacked card so the **filename owns the first full-width line** (it was being
> crushed to ~0 width in the tree view, which has no horizontal scroll) — CSS-only, no template change.

> New files are informational, not "attention" (inline fix, no openspec): a collection whose only
> non-`ok` files were `new` (status) was wedged reading "Attention" with **no way out** — a scan's
> fast-path **preserves** status (never promotes `new`→`ok`; `scanner.py`), stamping only sets
> `ots_state`, and the only re-baseline action (`accept`) was gated on `modified+missing`, so its
> "Accept changes" button was hidden for a new-only collection. So a freshly-added, fully-stamped collection
> (e.g. Bob Tax Services: 4672 files all `status=new`, `ots_state=complete`) showed "Attention"
> forever and **"Scan now" could never clear it** (works as designed — a scan is detection, not
> baselining; neither quick nor deep promotes `new`→`ok`). This contradicted
> streamline-event-acknowledgement (which already made the `added` event informational/born-acked),
> so two display-layer fixes align with it: (1) `_collection_status` no longer raises "attention" for
> `new` (only WORM `modified` → "attention"; `missing` → "alert"), so a new-only collection reads "All
> clear"; (2) the collection-detail re-baseline button now shows when there are `new` OR modified/missing
> files, labelled **"Baseline new files"** for the new-only case (one click `new`→`ok`, populating
> the "Verified OK" tile — optional, no longer required for healthy). `new` files were always still
> change-monitored (the scanner classifies by size/mtime/sha regardless of status) and notarized;
> only the status pill + button affordance changed. **No schema change.** Regression test in
> `tests/test_panel.py`.

> Un-writable proof paths (tolerate-unstampable-proof-paths): a proof filename that the store's
> filesystem refuses (ext4 caps one path component at 255 **bytes**; multi-byte UTF-8 names + the
> `.ots` suffix can exceed it — the 2026-07-07 Skate crash-loop) is **skipped per-file, never
> batch-fatal**: the file is warned, counted, and dropped to `ots_state='none'` (`ots_path` and
> `ots_stamped_at` cleared) so a normal scan never re-queues it; `stamp --all` may retry it cheaply.
> Governing rule (module docstring, `src/services/ots.py`): **only a failure on the final proof
> output path may be classified permanent (`OtsPathError`)** — every staging-side failure
> (mkdir/symlink, any errno) is a transient `OtsError` that leaves files `pending` for the next
> pass, cleanup only touches paths actually created and is `suppress(OSError)`-ed, and the
> `_NAME_MAX_BYTES` pre-check measures only components Cairn creates below the proof-store root
> (`store_root` threaded from `stamp_pending`), never the store root's own components — so an
> over-limit component in an existing store root can't silently drop a whole collection to `none`.
> A batch-level failure degrades to the per-file fallback and later chunks still run. Hardened over
> three adversarial review rounds (commits 8d77a49, 31fed5b, 83097d3); the known follow-up — a
> re-stamp can overwrite an existing proof for a *different* digest (`_place_proof` has no existence
> check) — is GitHub #15, queued with the restore-digest fix (#21).

> UX-audit sprint 1 (fix-ux-audit-sprint1): the 13 `sprint-1` issues from the Aug-2026 six-auditor
> UX walk (#13,#14,#17-#20,#22,#23,#26,#31-#34 — meta #12) — every false-reassurance string fixable
> without new routes or schema. Verify: `VerifyResult` carries typed outcomes (`digest_mismatch`,
> `proof_mismatch`, `transport_error`+`transport_failures`, `inconclusive`, `unreadable_proof`);
> verdicts branch by reason (mismatch before transport; verified wins if ANY attestation validates);
> blame is attributed only via the recorded baseline (re-stamp window reads amber "proof predates
> this version"; definitive proof-blame deferred to per-proof digests → #15); transport/queued/
> never-stamped/unreadable/missing-file cards are neutral, never red speculation; same ladder in
> `cairn verify` (CLI). Vocabulary: `pending`="Queued to stamp", `incomplete`="Pending confirmation"
> (never summed; stale-incomplete past the upgrade alarm drops the "settles in hours" reassurance);
> "Acknowledge"→"Mark reviewed"; "Verified OK"→"Matching baseline". Coverage claims are ratios over
> `status != 'missing'` within `ots_mode='perfile'` only; "all confirmed" ⇔ `complete_active ==
> stampable > 0`; zero-file collections read muted "No files indexed yet" on every surface.
> **Accept guard (D14):** every accept-family POST carries a `population_fp` fingerprint (files by
> identity+generation incl. `first_seen`, open events by id/kind/detected_at, US/RS/GS-framed,
> sha256) minted from ONE compound UNION-ALL snapshot per GET and recomputed inside a write-locked
> transaction at POST; drift/absence/BUSY ⇒ fail-closed 303 to `review?stale=1` ("Your action was
> NOT applied"). Detail header offers no Accept while issues exist ("Review issues" is primary);
> baseline-new renders only at zero issues AND zero open events; the review-accept's `new`-set
> promotion is the one disclosed fingerprint exception. Scanner: a restore system-acks the file's
> open `missing` events (kind-scoped, inside `_drain`'s commit). Dashboard tile links to review
> (multi → `/collections` until #27's fleet page), sidebar badge counts missing+modified via one
> helper. One shared clipboard helper (secure-context fallback) serves every copy button. **No
> schema change.** Gates: 5-round adversarial spec review, 2×2-round + final implementation review
> (all PASS), openspec-verifier 86/86, live user-representative pass + re-verify (6/6). Follow-ons
> ticketed: #36 inert global search, #37 detail-page new count, #38 scan-all labelling; fleet-wide
> `/review` is #27 (later change flips the multi-collection tile href).

> Integrity guards (guard-proof-and-restore-integrity): the audit's two real-loss paths (#15, #21)
> + the sprint-1 provenance IOU. **A stamp never destroys a proof**: `_place_proof` inspects an
> occupied canonical path and archives the displaced proof content-addressed under
> `<proof_store>/.superseded/<cid>/<dd>/<digest>[.N].ots` (never-discard suffixed family, link-free
> O_EXCL create, full-write loop, fsync file → directory chain to the store root → only then unlink;
> dir-fsync/flock-unsupported filesystems warn once and degrade). Same-digest anchored re-stamps are
> **adopted** only after a live anchor confirmation (recorded provenance never substitutes);
> unestablished verdicts **defer** with no state change. **A restored file is compared before it is
> believed** (#21): the scanner hashes first — different bytes ⇒ `status=modified` in both modes +
> an unacknowledged **`restored_changed`** event (both digests in `events.detail`, rendered fully on
> a wrapping line) + `pending` re-stamp; the obsolete missing alert still closes; alerts fire like a
> WORM modified. **Provenance**: `files.ots_digest` (migration **0011**, also `runs.heartbeat_at`;
> downgrade refuses while restored_changed rows exist) records what each placed proof commits to —
> written only at placement/adoption/corroborated-upgrade-backfill, never from an uncorroborated
> read; the verify blame ladder checks parsed-vs-recorded FIRST (swapped proof detectable), and the
> proof-stale reading claims only the committed-digest fact. **Proof mutation is single-writer by
> construction**: every entry point (scheduler, panel, `cairn stamp/upgrade/scan`) claims the
> collection's run slot; the claim is a full lease — heartbeat on progress + an async keepalive
> (5 min) independent of work, in-band stale reclamation at every gate + the tick (guarded UPDATEs,
> a concurrent heartbeat always wins), lease fencing before every batch commit/stamp tail/
> finalization, and a per-collection flock around placement with post-acquisition re-check. CLI ops
> refuse (never wait) when the slot is held; `cairn scan` exits non-zero only if everything was
> refused. Known deferred: #39 (moved file's ots_path points at the old relpath's proof), #40/#41
> (coverage-bar labelling, unstamped verify card reachability). Gates: 5-round spec review, 2-scope
> adversarial implementation review to PASS-zero, verifier 21/21 requirements, live user-rep pass.

> Scoped accept verbs (split-accept-into-scoped-verbs): the unscoped accept UI (#16, #30, #35 —
> meta #12 R2) is gone from the panel. Four constant-scope routes ride the D14 fingerprint guard:
> `/collection/{id}/accept` (**Baseline N new files** — offered only at zero issues AND zero open
> events, count from the minting snapshot), `/review/adopt-changed` (**Adopt N changed files**,
> `btn--warn-outline` per row), `/review/stop-tracking` (**Stop tracking N missing files**,
> `btn--danger`), and `/file/{id}/accept` (per-row, population-of-one fingerprint). `_narrow`
> derives each form's population from the ONE wide `_read_population` snapshot that also mints its
> fingerprint — **baseline-new is the explicit exception**: it hashes the collection-wide open-event
> set (so an alert on any file refuses it). Every display claim an action makes (counts, confirm
> strings, the ack-swapped row's verb) derives from its own minting snapshot; cross-form and
> cross-row replay refused (12 ordered pairs tested). Service: `accept_collection(scope=...)`
> (None = today's blanket behavior, `cairn accept` unchanged) acks ONLY the scope's own files'
> events; `accept_file` resolves one row; the pre-delete event detach now backfills `events.detail`
> with each file's relpath via one correlated subquery-keyed UPDATE (fills only NULL/empty — never
> clobbers moved/restored_changed; survives >999 rows), so #35's "Missing — —" rows carry their
> path going forward. `review-accept` (route + scope string) is retired — a stale tab 404s, nothing
> forwards. **No schema change.** The old accepted limitation "review accept silently baselines new
> files" is resolved: a new file appearing refuses nothing and is not promoted. Gates: 2-round spec
> review, 3-round adversarial implementation review to PASS-zero, verifier 65/65, live pass 6/6.

- `make init|build|deploy|up|down|logs|shell|db-backup|status|clean|audit` — **implemented** (add-foundation).
  `make deploy` = build → trivy → push → SQLite online backup → `compose up -d --force-recreate`.
  Host paths in gitignored `Makefile.local` (`DEPLOY_DIR=/srv/cairn`).

> **Standard session flow:** finish a unit of work by committing directly to `main`, pushing to
> `origin`, then `make deploy`. `make deploy` does **not** run migrations and the container
> auto-migrates only when `CAIRN_AUTO_MIGRATE` is set — so when a change adds an Alembic revision,
> run `make migrate` (`alembic upgrade head`, idempotent) right after deploy. Verify with
> `make status` / the `/healthz` poll. **This commit → push → `make deploy` → `make migrate` (when a
> revision was added) flow is run automatically at the end of a unit of work — Max has standing
> authorization, so don't ask per change.** Only stage the files belonging to the change (leave
> unrelated dirty files like in-progress proposals or local `docker-compose.yml` tweaks alone).

> OTS dependency de-risked 2026-05-31: `ots` CLI v0.7.2 stamps on Python 3.12 (host venv and the
> `cairn:latest` image). Health is exposed at `/healthz` (poll model — external monitors poll Cairn).

> Renamed "corpus" → "collection" (rename-corpus-to-collection): "corpus/corpora" read as jargon,
> so the domain term is now **collection** everywhere — UI copy, routes (`/collection/...`), CLI
> (`add-collection`, `--collection`), services (`src/services/collections.py`,
> `scan_collection`/`accept_collection`/`list_collections`…), the ORM class `Collection`, and the DB
> table `collections` with FK `collection_id` on `files`/`runs`/`events`. Migration **0009** does an
> in-place SQLite rename (`ALTER TABLE … RENAME TO` + `RENAME COLUMN`, no table rebuild — cheap on the
> ~186k-row `files` table; FK refs auto-repoint on SQLite ≥3.25; the one named partial index becomes
> `uq_runs_one_running_per_collection`). Backward-compat: old `/corpus/...` (and `/corpora`) URLs
> **308-redirect** to the new paths, and `cairn add-corpus` / `--corpus` stay as aliases. The OpenSpec
> capability id `corpus-management` is intentionally **kept** (internal traceability; archived changes
> reference it as history). Constraint names that still embed "corpus" (`uq_files_corpus_relpath`,
> `ck_corpora_*`) are cosmetic labels left as-is to avoid a needless `files` rebuild.

> Issue-review page + recovery guidance (add-issue-review-and-recovery): the dashboard card's issue
> count and the collection-detail "Changed / missing" tile now deep-link to a focused review page
> **`GET /collection/{id}/review`** (`collection_review.html`) — the home for "what happened to my
> files, and what do I do now". It lists every `missing` + `modified` file (missing first, bounded to
> `REVIEW_ROW_LIMIT=500` rows) with what-happened + last-seen + size + a "proof of prior existence
> kept" note for notarized files, a per-file **Acknowledge** (reuses `POST /events/{id}/ack?view=review`
> → swaps the row + OOB-refreshes the collection's `#review-open-pill` and the global
> `#sidebar-alert-badge`), and collection-scoped **Acknowledge all** / **Accept all changes**
> (`/collection/{id}/review/ack-all` + `/review/accept`, both redirect back to review). **Recovery is
> instructions-only and backup-tool-agnostic** (public-repo-safe): "Copy paths" / "Copy full paths"
> buttons (relpaths + root-prefixed, computed client-side from a bounded `REVIEW_COPY_LIMIT=2000` list)
> plus a tool-neutral "How to recover" panel — Cairn never restores files itself. Reuses
> `query_files`/`_event_view`/`humanize_*` + the pill/badge macros; no new query primitives, no schema
> change. **Restic / live "find in backup" is deferred** (Phase-2 follow-up, kept out so the repo can
> go public).

> Auto-baseline new files on the deep pass (auto-baseline-new-files): a per-collection boolean
> **`collections.auto_baseline_new`** (migration **0010**, additive, default `0`/off). When on, a
> **deep** scan (`scan_collection(deep=True)`) — after classification + the missing-sweep, before the
> commit — promotes every file still `status=new` and present this pass to `ok` (`summary.baselined`,
> logged; surfaced in the CLI scan line). Only **pre-existing** `new` rows graduate (`existing` is the
> pre-scan snapshot, so files first discovered this pass are skipped); a `new` row reclassified
> `modified`/`missing` this pass is no longer `new` so is never auto-accepted; **never re-stamps** (a
> `new` file was stamped when first seen). A quick scan never promotes — only the weekly deep pass, so
> additions "settle" for up to a verify cycle before graduating. Off preserves the old manual-baseline
> behavior. Editable in the add/edit-collection form (On/Off select next to Deep verify) and
> `cairn add-collection --auto-baseline`. **Enabled on the Photos collection** (steadily-growing); other
> collections stay off (e.g. tax/legal, where new additions are reviewed by hand). Does not change the
> notary guarantee — `new` vs `ok` is only a baseline/UI distinction.

## Engineering workflow

The standard flow — supervisor main thread, **Opus** subagents, OpenSpec, the
two Codex audit gates, `openspec-verifier`, and the `user-representative`
browser pass — is defined once in `~/.claude/rules/engineering-workflow.md` and
applies here. Read it; don't restate it. This section records only what is
specific to Cairn.

**Local gates**

| Gate | Command |
| --- | --- |
| Dependency audit | `make audit` (pip-audit) |
| Migrations | `make migrate` (alembic) |
| Deploy | `make deploy` (build → Trivy → compose) |

**Codex framing for this product.** Cairn's product *is* a trust claim: it
tells a user that a file existed, unmodified, at a point in time. When briefing
the adversarial gate, say so — the expensive failure is a **false negative**
(a modified or deleted file that scans clean, or a proof that verifies when it
shouldn't). Anything touching hashing, the scan→diff→classify path, OTS
stamp/upgrade/verify, or proof export is a mandatory adversarial-pass trigger:
a wrong answer there silently voids the evidentiary value of every proof.

**`user-representative` pass** applies to the web panel (`cairn serve`). Note
in the brief that this is a self-hosted multi-user tool for a technical
operator, not a consumer app.

**OpenSpec is already the norm here** — Phase 1 was built change-by-change.
Keep it that way.

## Conventions
- Build private; open-source when stable. Keep Max's host-specific paths/secrets out of tracked
  files (config & env, never hardcoded) — see DESIGN.md "core vs personal".
- De-risk the OTS dependency early: smoke-test `ots stamp/upgrade/verify` on Python 3.12 before
  building `src/services/ots.py`.
