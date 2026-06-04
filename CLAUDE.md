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

## Conventions
- Build private; open-source when stable. Keep Max's host-specific paths/secrets out of tracked
  files (config & env, never hardcoded) — see DESIGN.md "core vs personal".
- De-risk the OTS dependency early: smoke-test `ots stamp/upgrade/verify` on Python 3.12 before
  building `src/services/ots.py`.
