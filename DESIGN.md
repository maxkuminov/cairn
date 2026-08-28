# Cairn — Design

> **Status:** the build spec. Authored 2026-05-31; revised 2026-05-31 (web-panel pivot,
> language decision, name). **Phase 0 and Phase 1 have shipped** — the sections below have been
> synced back to what was actually built.
>
> **Document status — last synced 2026-08-28.** DESIGN.md holds the *why*: motivation, locked
> decisions, architecture shape, and the records of decisions already taken. It is deliberately
> **not** the behavioural contract. The living specs are in **[`openspec/specs/`](./openspec/specs/)**
> — one directory per capability, and the only place requirement/scenario text belongs. The
> shipped-change history is `openspec/changes/archive/`. **[`CLAUDE.md`](./CLAUDE.md)** carries the
> operational notes (commands, deploy flow, a note block per shipped change). Where this document
> and a spec disagree, **the spec wins**.

**Cairn** is a self-hosted **file-integrity monitor + OpenTimestamps notary** with a web
panel. It continuously detects deletion / modification / silent corruption across
configured file sets ("collections"), and (optionally, per collection) anchors each file's hash to
the Bitcoin blockchain via OpenTimestamps so you hold a trustless "this file existed,
unaltered, by date X" proof. Config-driven, multi-user, pluggable alerts, no external
service dependency.

> *Why "Cairn":* a cairn is a stack of stones left as a durable, human-made marker —
> proof that something was here, that endures for ages (provenance / OTS). And on a trail,
> if a cairn has been knocked over or moved, you notice (integrity monitoring). The name
> carries both halves of the product.

---

## 1. Motivation & origin

Started as a fix for one concrete fear: Max's irreplaceable family photos
(`/srv/media/photos`, ~1.4 TiB / 186k files) suffering **silent logical
loss** — a file deleted, overwritten, or corrupted *above* the block layer — that goes
unnoticed until it ages off the off-site backup (~90–180 days) and is gone forever.

What already protects that data (so Cairn does NOT reinvent it):
- **Bit-rot on disk** → BTRFS RAID1 + weekly `btrfs scrub` (detects *and self-heals* from
  the good mirror).
- **Off-site** → nightly `restic` to a cloud backend, ~90–180d retention.

The gap is **logical-change detection** (scrub can't tell you a file *vanished*) and
**provenance** (proving when/what). A personal bash tripwire was already built for the
photos (see §8); Cairn generalizes it into a reusable, multi-user product.

Two real-world drivers that shaped scope:
- **EXIF dates lie.** Max hit bad EXIF timestamps doing memoir research. For *new* photos
  (auto-uploaded from phones via Nextcloud), an OTS proof gives a **trustworthy "existed-by"
  date independent of EXIF**. (It cannot retro-date the old archive — stamping a 2015 file
  today only proves "existed by 2026".)
- **Document provenance.** Tax/legal documents where a *portable, third-party-verifiable*
  proof of existence-by-date has genuine value.

---

## 2. Positioning (why this is a niche, not a me-too)

The two halves exist separately; nobody cleanly joins them behind one self-hosted panel:
- **Integrity monitors** (AIDE, Tripwire, Samhain, `bitrot`, `cshatag`) — detect change/rot,
  **no notarization, CLI-only**.
- **Notarization** (OpenTimestamps client) — stamp/upgrade/verify, **no monitoring, no
  collections, no alerting, no UI**.

**Cairn = continuous integrity monitoring + OpenTimestamps notarization + pluggable alerts +
a multi-user web panel**, aimed at two audiences: **family-archive owners** (trustworthy
photo dates that beat EXIF) and **small-business document provenance** (tax/legal proofs you
can hand off). The OTS layer is the hook: *"integrity monitoring that also gives you
blockchain-anchored proof your files existed, unaltered, by a date — with a page anyone can
use to verify it."*

---

## 3. Locked decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Product name | **Cairn** | Durable marker (provenance) you'd notice if disturbed (integrity); carries both halves. PyPI `cairn` is a dead-ish minor tool, so the *distribution* name needed a suffix — **resolved: `cairn-integrity`** (§12); the brand is clean |
| Shape | **Web-panel-first**, with a headless CLI underneath | Multi-user requires a UI; users self-serve their monitored paths, see status, and verify proofs. The CLI still drives cron/headless ops |
| Language/runtime | **Python 3.12** | (1) The OTS tooling that is *actually maintained* is Python — `opentimestamps-client` shipped **v0.7.2 on 2024-12-31**, vs the JS `opentimestamps` npm lib stuck at **v0.4.9 (2021-01-29)**, CI-tested only against EOL Node 6/7. Betting an integrity tool's core on abandoned crypto code is the wrong risk. (2) Direct reuse of Max's hardened Obsidian-MCP FastAPI codebase. (3) One stack to operate on the home server. (Node/TS evaluated and rejected; see §11.) |
| Web stack | **FastAPI + uvicorn; Jinja2 + htmx + Tailwind panel** | Mirrors the Obsidian-MCP server — proven, server-rendered, minimal JS |
| Datastore | **SQLite** (single file, WAL mode) | No service dependency (a safety tool must run even if a DB server is down); trivial self-host install; rides in existing snapshots/backups; "the DB is just an index — the guarantee is bytes + proofs". The scanner is the single writer; panel reads/writes are light. Postgres rejected: its concurrency/multi-app wins don't justify the install burden for a single-writer tool |
| Multi-user | **Dual-mode (single / multi), like Obsidian MCP** | `single` = no login wall, one implicit user. `multi` = login + admin role, each user scoped to their own collections, can't see anyone else's |
| Filesystem access | **Read-only mounts, per-user jailed roots** | Cairn *physically cannot* modify or delete what it watches (the integrity tool can't become the threat). Each user's collection roots must live under an admin-provisioned, read-only mounted base |
| OTS proof storage | **Parallel store** (separate writable volume) + `export` | Keeps watched trees clean & read-only; `cairn export <file>` bundles file + `.ots` for handoff |
| OTS granularity | **Per-file** where proofs may be presented externally; manifest/none where only personal | Per-file = standalone portable proof, zero inventory disclosure. Calendar Merkle-aggregation makes per-file cheap (not N transactions); the *lifecycle* (incomplete→complete, N `.ots`) is what mandates the DB |
| OTS integration | **Primarily wrap the maintained `ots` CLI** (subprocess); pin `opentimestamps[-client]`; keep the library import path available | The CLI is the most actively maintained surface; subprocessing decouples us from library API churn. Smoke-test on 3.12 first |
| OTS cadence | **Stamp on first-seen, re-stamp on content change, daily `upgrade` pass** | Each distinct content state anchored; upgrades complete after Bitcoin confirms (~hours/day) |
| Verify backend | **Block-explorer lookup by default (configurable to a Bitcoin node)** | Self-hosters get working verify out of the box ("trust the lookup"); node owners get fully trustless. Browser-side verify deferred (§11). **Shipped as a dispatcher — see §6** |
| Codebase | **New standalone Python project** (a new project dir → rename to `cairn`) | Supersedes the bash photo script, which keeps running until parity then migrates (the manifest import shipped; §8) |
| Dev model | **Build private, open-source when stable** | Protect Max's data now; public release is battle-tested |
| Obsidian | **Separate track, NOT in Cairn** | High churn → OTS is noise. Use private GitHub repo + hourly git commit/push instead (see §9) |
| Core vs personal | **Clean separation** | Max's CallMeBot/s-nail/Kuma/restic/paths are *config & optional plugins*, never hardcoded |

> **Status of the locked decisions (2026-08-28).** Every row still holds as written, with three
> annotations: the **verify backend** is built, as a dispatcher (§6); the **distribution name**
> question is settled (`cairn-integrity`, §12); and the *"the app owns its own auth → no external
> OAuth proxy"* consequence of the multi-user row holds **only in `multi` mode, which has not been
> built yet** — see the flag in §4. One granularity in the OTS row was never built: `ots_mode`
> shipped as `none | perfile` only, with no `manifest` mode (the manifest is an *import* format,
> §8, not a stamping mode).

---

## 4. Deployment modes & multi-user model

**Mode selected by env (`CAIRN_AUTH_MODE=single|multi`).**

- **single-user** — no login; one implicit user owns every collection. The simple default for a
  personal install.
- **multi-user** — login required; an **admin** creates users and assigns each a mounted
  base. Every collection belongs to a user; queries are scoped by `user_id`; a user never sees
  another user's collections, files, events, or proofs.

**Filesystem model (the key security surface).** Cairn runs in a container. Watched folders
are mounted **read-only**; the SQLite DB + OTS proof store live on a **separate read-write**
volume. A user can only create collections whose root falls under their admin-provisioned base
mount — the panel rejects paths outside it (no traversal, no cross-user disclosure). Because
the app has its own multi-user login, **no external OAuth proxy is required** (unlike the
Obsidian-MCP panel, which sat behind Traefik `chain-oauth`).

> **Shipped state (2026-08-28).** Only **single-user mode is built.** There is no login route and
> `src/auth/` is still an empty package. The *scoping* half of the multi-user model is in place and
> exercised on every request — the panel resolves an owning `User` (`current_user`, today the
> implicit single row) and scopes every query and every collection lookup by it — but the login
> wall, user administration and admin-provisioned per-user mounted bases are Phase 2 (§10). The
> Settings "Users & mounts" tab renders only for an admin in `multi` mode, so it is unreachable
> today.
>
> ⚠️ **Reality contradicts the decision above.** "No external OAuth proxy is required" is a
> consequence of multi mode. In `single` mode the panel has **no login wall at all**, so the
> homelab deployment fronts it with Traefik `chain-oauth@file` (Google OAuth), keeping `/healthz`
> public on a higher-priority router for the Uptime-Kuma poll (see `docker-compose.example.yml`
> and CLAUDE.md). The shipped `Caddyfile.example` still documents the proxy-straight-through path
> the locked decision describes. The middleware comes off when multi-user login lands.

### Example multi-user instance (illustrative config, NOT hardcoded)

| User | Collection | Root (mounted ro) | Mode | OTS | Alerts |
|---|---|---|---|---|---|
| **alice** | Photos | `/srv/media/photos` (incl. phone-upload subdirs `Phone Uploads`, `Camera`, `Unsorted`) | WORM | **per-file on newly-added files only** (archive stays a hash-tripwire) | Alice: Signal + email |
| **bob** | Tax practice files | `/srv/documents/tax` (incl. `TAX CLIENTS`) | WORM-ish | per-file | bob@example.com (cc admin? — open Q) |
| **carol** | Game ROM collection | (Carol's mounted base) | WORM | **none** (tripwire only) | Carol + cc admin (alice) by default |

- A ROM set doesn't change, so any modify/delete is real signal and OTS adds no provenance
  value — tripwire-only WORM is the right, cheap policy.
- Exclude generated/cache files and any editor/notes vault (e.g. under `/srv/documents`) from any
  document collections.

---

## 5. Architecture

*As built (2026-08-28).*

```
cairn/
  src/
    main.py             # FastAPI app, lifespan (orphan-run reaper + scheduler), mounts the panel
    config.py           # pydantic-settings (CAIRN_* env: auth mode, paths, calendars, verify backend…)
    database.py         # async SQLAlchemy engine/session (aiosqlite + WAL/foreign_keys pragmas)
    csrf.py             # CSRF issue/verify for the panel's mutating routes
    models/db.py        # ORM: users, collections, files, runs, events, app_settings
    services/
      scanner.py        # walk → diff → hash changed → classify → reconcile → stamp tail → alert
      collections.py    # collection CRUD + root validation; the run claim/lease, keepalive and
                        #   fence; file queries (search/filter/sort/paginate) + one-level tree browse
      ots.py            # `ots` CLI wrapper; proof placement/preservation; the per-collection
                        #   proof-store lock; the verify dispatcher (explorer | node)
      proofs.py         # the proof lifecycle over that store: batched stamping, adoption, upgrade,
                        #   stamp-backfill runs, export bundles, stale-incomplete listing
      scheduler.py      # health/freshness, orphan reaper, cheapest-first scan pass, daily upgrade
      manifest.py       # photo-tripwire `manifest.tsv` import (§8)
      app_settings.py   # DB-backed overlay of UI-editable settings over the env defaults (DB wins)
      panel_url.py      # the one normalize/build path for outbound links into the panel
    notify/             # pluggable notifiers + dispatch
      base.py  dispatch.py  smtp.py  signal_callmebot.py  webhook.py  ntfy.py  kuma_push.py
    control_panel/      # routes.py (all panel + htmx endpoints) + Jinja2 templates + static assets
    api/                # reserved for a REST surface — empty package, nothing mounted
    auth/               # reserved for session auth — empty package (Phase 2, §4)
    witness/            # reserved for the restic witness — empty package (deferred, §10)
    cli.py              # argparse entrypoint (command list below)
  alembic/versions/     # 0001 … 0011
  Dockerfile  docker-compose.yml  docker-compose.example.yml  Caddyfile.example  Makefile
  config.example.yaml  pyproject.toml  README.md  DEPLOYMENT.md  SECURITY.md  CLAUDE.md
  LICENSE (Apache-2.0)  tests/  docs/  openspec/
```

The panel's endpoints live in `control_panel/routes.py`, not in `api/` — the panel is
server-rendered htmx, so there was never a separate JSON API to mount. `api/`, `auth/` and
`witness/` are reserved namespaces, empty today.

### SQLite schema (as built; `src/models/db.py`, migrations `0001`–`0011`)
- `users(id, username, password_hash, is_admin, is_active, created_at, last_login_at)`
  — single-user mode uses one implicit row.
- `collections(id, user_id, name, root, mode[worm|churn], hash_cadence_seconds,
  verify_cadence_seconds, ots_mode[none|perfile], auto_baseline_new, exclude_globs_json,
  alert_json, created_at, last_full_scan_at)`
  — `root` must resolve under the owning user's mounted base. `verify_cadence_seconds` drives the
  deep re-hash pass (`0` = off) and `last_full_scan_at` records the last one; `auto_baseline_new`
  lets that deep pass graduate intact `new` files to `ok`.
- `files(id, collection_id, relpath, size, mtime, sha256, first_seen, last_checked, last_changed,
  status[ok|new|modified|missing], ots_path, ots_state[none|pending|incomplete|complete],
  ots_stamped_at, ots_digest)`
  — `ots_digest` is **provenance, not a second copy of the file's hash**: the digest the proof
  Cairn placed at `ots_path` commits to. It is written and cleared with `ots_path`/`ots_state`, and
  never filled from an uncorroborated read of an `.ots` on disk (§6).
- `runs(id, collection_id, kind[scan|stamp|upgrade], started, finished, heartbeat_at, processed,
  total, added, modified, missing, moved, stamped, upgraded, deep,
  result[ok|error|partial|running|interrupted])`
  — audit trail, dead-man's-switch source, **live progress**, and the **single-operation claim**: a
  partial unique index (`uq_runs_one_running_per_collection`) makes "at most one in-progress run
  per collection" atomic across processes, `heartbeat_at` is the claim's liveness, and only
  `kind='scan'` runs count toward freshness. `interrupted` is the reaper's terminal state (a
  restart killed the run), kept distinct from a genuine `error`.
- `events(id, collection_id, file_id, kind[added|modified|missing|restored|moved|restored_changed],
  detail, detected_at, acknowledged_at, acknowledged_by)` — nag-until-accept lifecycle + the panel's
  feed. **Only the alarming kinds nag**: `missing`, WORM `modified`, and `restored_changed`. The
  informational kinds (`added`, `restored`, `moved`) are written *already acknowledged*
  (`acknowledged_by` NULL = a system ack), so routine activity shows in the feed without inflating
  "needs action". `detail` carries context — `"old → new"` for a move, both digests for a
  `restored_changed`.
- `app_settings(key, value, updated_at)` — UI-editable global config (the SMTP server, the public
  panel address) overlaid over the env defaults at read time, **DB wins**; an empty table is pure
  env fallback.

### Per-run flow (one collection)
One sentence of *shape* per step; the behavioural contract is
`openspec/specs/integrity-scanning/`.

1. **Claim** — atomically claim the collection's single in-progress slot (a `running` `runs` row
   under the partial unique index) and commit it up front, so the scheduler, a manual panel op and
   the CLI can never run two writers over one collection.
2. **Keepalive** — a timer refreshes the claim's `heartbeat_at` from its own session for as long as
   the body runs, so a scan that spends longer than the abandonment interval inside *one* unit of
   work (hashing a multi-terabyte file) does not starve its own claim.
3. **Walk & diff** — walk the root honouring the exclude globs, skip any relpath SQLite cannot
   store as TEXT (reported, not tracked), and diff against `files` by relpath, **fast-pathing on
   size + mtime** and hashing only what looks changed.
4. **Deep verify** — on a deep pass, re-hash *every* tracked file instead of fast-pathing; that is
   the only way silent bit-rot (bytes change, size + mtime do not) is ever seen.
5. **Classify** — added / modified / unchanged, writing `events` and per-batch progress; WORM and
   churn differ in what nags, not in what is detected.
6. **Missing sweep** — anything tracked and not seen this pass becomes `missing`.
7. **Rename reconciliation** — a candidate-`missing` row whose `(sha256, size)` matches exactly one
   newly-`added` row (strict 1:1, zero-byte excluded) is the same file relocated: reconciled in
   place, with one informational `moved` event instead of a false alarm plus a wasted re-stamp.
8. **Restore comparison** — a reappeared file's fresh digest is compared against the recorded one
   **before** that record is overwritten: same bytes → `restored` (informational, no re-stamp);
   different bytes → `modified` plus an alarming `restored_changed` event carrying both digests.
   Either way the reappearance acknowledges its own still-open `missing` events, so no alert is
   left open that nothing can clear.
9. **Auto-baseline** — on a deep pass, in a collection that opted in, pre-existing intact `new`
   files graduate to `ok`.
10. **Fence, then commit** — before each commit the run re-confirms against the datastore that it
    still holds the claim; if it has been reclaimed it stops where it stands and writes nothing
    further (work already committed stands).
11. **Stamp tail** — in a `perfile` collection, stamp what this scan queued `pending`, in batches
    (§6), re-confirming the claim before each batch.
12. **Finalize** — write the `runs` row to a terminal state. A scan **always** reaches one: the
    failure path rolls back and still finalizes, with a last-ditch `UPDATE` fallback, so no
    in-process failure can leave a collection wedged `running` and unscannable.
13. **Alert** — one **batched, best-effort** alert per collection for the alarming events newly
    detected this run, dispatched *after* the commit, isolated per channel, and structurally unable
    to fail the scan.

### Scheduler
Background task in the FastAPI lifespan (the Obsidian-MCP indexer pattern), but **per-collection
cadence**, staggered — you cannot full-rescan 186k files every 5 min. E.g. documents every
15 min, photos nightly. Scan-all on startup, then each tick scans the due collections
**sequentially, cheapest-estimated-cost first** (tracked bytes, then file count), so a multi-hour
deep pass over the largest collection cannot starve the fleet behind it; at most **one deep pass
per tick**. A separate **daily `ots upgrade` job** completes pending proofs once Bitcoin confirms,
recorded as its own `kind='upgrade'` run. A collection with an operation already in flight is
skipped. A startup **reaper** marks any run left `running` by a killed process `interrupted`, and a
claim whose holder has stopped reporting liveness can also be reclaimed **on the claim path itself**,
without a restart — so a deployment with no web process still recovers.

**The dead-man's switch is a poll, not a push:** `/healthz` reports per-collection scan freshness —
200 `ok`, 503 `degraded` (a collection is stale), 503 `error` (datastore down) — for an external
monitor such as Uptime Kuma to poll. (The push-style Kuma notifier is scaffolded but not the
mechanism.) `CAIRN_SCHEDULER_ENABLED=0` gives a cron-only deployment driven by `cairn scan --once`.

### Web panel (pages, as built — the living spec is `openspec/specs/web-panel/`)
- **dashboard** — per-collection status cards (resting status pill, live operation badge, last
  scan, counts, proof coverage stated as a **ratio** rather than an unearned "all confirmed"), the
  recent `events` feed with single and bulk acknowledge, and the health pill
- **collections** — the collection list
- **collection detail** — a **Tree ⇄ List** file browser (tree default; one directory level per
  request, so the full set is never materialized — the list view is searched, filtered, sorted and
  paginated server-side), per-file status and proof state with the notarization date, and the
  "Scan now" / "Stamp all" / re-baseline actions
- **collection review** (`/collection/{id}/review`) — the "what happened to my files and what do I
  do now" page: every missing + modified file with what happened, last seen, and whether a proof of
  prior existence is held; per-file and collection-scoped acknowledge/accept; "Copy paths"; and a
  **tool-neutral "How to recover" panel** — Cairn never restores files itself
- **add / edit collection** — root (validated live, within the allowed base), mode, OTS mode,
  exclude globs, scan + deep-verify cadences, auto-baseline, alert routing
- **verify** — **no upload**: search the tracked files that hold proofs, and Cairn re-hashes the
  file it already watches and checks the stored `.ots` against the configured block source. The
  result card names *which* outcome occurred — anchored, not yet anchored, digest disagreement,
  proof mismatch, backend unreachable — rather than one generic failure, and the bundle can be
  downloaded. (Browser-side verify, where the file never leaves the user's machine, is still §11 /
  Phase 4.)
- **learn** — a plain-language explainer of what a hash and a Bitcoin timestamp do, and do not,
  prove
- **settings** — tabbed: **Notifications** (panel address, SMTP server config + "send test email",
  the scaffolded channels shown as planned), **Verification** (block source, calendar servers —
  env-only values presented as description, never as a dead control), and **Users & mounts**
  (admin, `multi` mode only — not yet built)
- **login / register** — Phase 2, not built (§4)

### CLI (headless / cron / ops)
`cairn init | serve | add-collection | scan [--collection X] [--once] | accept [--collection X] |
verify <relpath> | export <relpath> | upgrade | stamp [--collection X] [--all] | bench |
import-manifest --collection X --file PATH [--rehash]`. `--once` for cron; `serve` runs the panel;
`stamp` backfills an unstamped baseline off the scan path; `bench` measures local SHA-256
throughput and estimates a deep pass. **`status` is the one subcommand still planned** — it exists
on the parser and exits non-zero. After the corpus→collection rename, `add-corpus` and `--corpus`
remain as aliases. Flag defaults and behaviour live in CLAUDE.md, not here.

### Config (env + optional YAML) — captures everything that would otherwise be hardcoded
`CAIRN_*` env via pydantic-settings, with an optional YAML file: auth mode · datastore path ·
proof-store path · OTS calendars · stamp batch size · verify backend (explorer URL or node RPC) ·
incomplete-proof alarm days · scheduler enable/intervals · health freshness floor · public panel
URL · SMTP server · per-collection policy + alert routing (on the collection row, not in env).
A small set is additionally **editable from the panel** and stored in `app_settings`, which is
overlaid over the env values at read time with the DB winning (currently the SMTP server and the
public panel address; the SMTP password living in the DB is a deliberate homelab departure from
"secrets via env only" — §12). The living list is `openspec/specs/configuration/` plus
`.env.example` / `config.example.yaml`.

---

## 6. OpenTimestamps handling
*Shipped. The full behavioural contract — every placement, adoption, deferral and verify outcome —
is `openspec/specs/ots-notarization/`; this section is the shape and the rationale.*

- Stamp → **incomplete** proof immediately (calendar-signed); run `upgrade` after the Bitcoin
  tx confirms (~hours; batched) to bake the **complete** Bitcoin path. The incomplete state is
  the only fragile window — a proof still incomplete past `CAIRN_INCOMPLETE_PROOF_ALARM_DAYS`
  (default 7) is listed as **stale-incomplete** so it can be surfaced and re-stamped; `cairn
  upgrade` warns on them.
- Proofs live in the parallel store at `<proof_store>/<collection_id>/<relpath>.ots` (the watched
  tree stays read-only — a file is stamped through a symlink in `<proof_store>/.staging`), and
  `cairn export` / the panel bundle the file with its `.ots` on demand.
- **Stamping is batched:** one `ots stamp <f1>…<fN>` per batch → one calendar round-trip, still N
  independent per-file `.ots`; a member that yields no proof falls back to a single-file stamp, and
  no per-file failure ever fails the batch or the scan. Per-file stays cheap at the chain level —
  the calendar Merkle-aggregates many submissions into one tx.
- **Automatic stamping is scoped to what a scan added or changed.** A pre-existing unstamped
  baseline is left alone and backfilled deliberately (`cairn stamp --all`, or "Stamp all").
- **Verification is a dispatcher.** Default `explorer`: parse the `.ots` with the `opentimestamps`
  library and confirm each Bitcoin attestation's commitment against the real block's merkle root,
  fetched from an esplora-compatible explorer (`CAIRN_EXPLORER_URL`, default blockstream.info).
  `node` keeps the CLI path (`ots verify -d --bitcoin-node …`). *This was forced by reality, not
  preference:* the maintained `ots` CLI can **only** verify against a Bitcoin Core node, so before
  the dispatcher every complete proof failed on a host without `bitcoind`. The explorer default
  trusts the explorer's canonical block at a height — the acknowledged, less-trustless default;
  point at a node for full trustlessness. A verify result reports **which** outcome occurred
  (anchored · not yet anchored · digest disagreement · proof mismatch · backend unreachable), and a
  digest disagreement is reported neutrally — it establishes that two digests differ, not which
  artifact changed.
- **A proof is the one artifact that cannot be recomputed, so placement never destroys one.**
  Before placing, the output path is inspected: an existing proof for the same digest whose
  attestation is *confirmed* keeps its place (a new proof is never anchored — replacing it would be
  a strict downgrade); an existing proof for different bytes, an unreadable one, or one the
  calendars never anchored is **preserved into a superseded archive** (keyed by the preserved
  proof's own digest, never by the watched file's name) and the new proof is placed; and if the
  backend cannot be reached, the placement is **deferred** — nothing is asserted, nothing is
  destroyed, and the next pass settles it. A file already carrying a confirmed proof for its own
  bytes is **adopted** rather than re-submitted, but only on a read + digest match + a
  chain-confirmed attestation *at that moment* — recorded provenance never substitutes for the
  chain. `files.ots_digest` records the digest of the proof Cairn itself placed, which is why it is
  provenance and not a second hash. All of this runs under the collection's single-operation claim
  **and** an advisory lock on that collection's proof subtree, re-confirmed before each unit, so
  two writers can never both find the canonical path free. A path the filesystem refuses
  permanently (`ENAMETOOLONG`) leaves the file unstamped and reported, never re-queued forever.
- **Blocking OTS work stays off the event loop** — every `ots` subprocess and proof IO reachable
  from the async app is `asyncio.to_thread`-ed, sequentially (one subprocess at a time), so a large
  pass cannot freeze the panel.
- **De-risked 2026-05-31:** `ots` v0.7.2 stamps on Python 3.12 (host venv and image);
  `opentimestamps` / `opentimestamps-client` are pinned.

---

## 7. What we reuse from the Obsidian-MCP server (the sibling FastAPI app)
Cairn is the same *shape* of app, so we lift patterns (and where clean, code):
- **Stack:** FastAPI / uvicorn / SQLAlchemy async / Alembic / Jinja2 + htmx + Tailwind.
- **Auth:** session login, password hashing (`pwdlib[argon2]` — *not* passlib, which is
  unmaintained and whose bcrypt backend is broken against bcrypt>=5), `User` model with
  `is_admin`/`is_active`, per-user scoping (their `vault_path` → our per-user collections). Drop the
  API-key + OAuth2/PKCE layers (MCP-specific; Cairn v1 needs only the panel session auth).
  *Status:* the `User` model and the per-user scoping are in; the session-login lift has not
  happened yet (§4, Phase 2).
- **Indexer → scanner:** the "run on startup, then on a cadence, hash-based change detection"
  loop is exactly our scan/diff/hash; we change the payload from "embed notes" to
  "classify + stamp".
- **Atomic writes** (`write_file` tmp + `os.replace`), CSRF, rate limiting, timing-safe compares.
- **Ops:** Docker + reverse proxy + Makefile deploy + the "host paths live outside the public
  tree" discipline + OpenSpec. *Status:* both paths ship — `Caddyfile.example` is the simple
  self-host path (proxy straight through, the app owns its auth), while `docker-compose.example.yml`
  carries the Traefik labels the homelab actually runs, including the `chain-oauth@file` middleware
  that stands in for the login wall single mode does not have and the higher-priority public router
  that keeps `/healthz` pollable (§4).
- **Drop the Postgres-isms** (JSONB/ARRAY/TSVECTOR/pgvector) — Cairn's models are plain columns
  + JSON blobs, SQLite-friendly.

---

## 8. Migration from the already-built photo tripwire (record, 2026-05-31)
*Record of the pre-Cairn state and the parity requirement it imposed.* The import half **shipped**
(`cairn import-manifest`, `src/services/manifest.py`, `openspec/specs/manifest-import/`); nothing in
this repo records the bash script having been retired, so treat the paragraphs below as the
starting state, not as a description of the host today.

A working bash tripwire protected the photos and had to keep running until the
Python engine reached parity:
- `/srv/scripts/photo-integrity.sh` (+ `photo-integrity.env`, chmod 600)
- State: `/srv/integrity/{manifest.tsv, pending-deletions.tsv, photo-integrity.log}`
- Cron (example): daily `check` 05:00, monthly `verify-backup` (restic) 1st 14:00
- Uptime Kuma push monitor **#85** "Photo Integrity Tripwire" (dead-man's-switch)
- Modes: `check` (delete/edit detection, Signal+email, nag-until-`accept`), `verify-backup`
  (restic `--force` + `diff` independent witness), `accept` (re-baseline)

**Migration (shipped):** `cairn import-manifest --collection X --file PATH [--rehash]` imports the
existing `manifest.tsv` rows into the `files` table (no re-hash unless asked; tolerant parsing,
idempotent re-import). Everything imported is "pre-existing — don't stamp": it lands `ots_state =
none`, which is exactly why automatic stamping is scoped to what a scan adds or changes (§6), and
why backfilling a baseline is a deliberate `cairn stamp --all`. Retirement of the bash script is
the operator step that follows parity; it is not recorded here as done.

---

## 9. Obsidian (separate track — not part of Cairn)
- Vault is a symlink → `/srv/documents/Obsidian` (so it *is* in restic), 1.4 GB.
  Largest file 49 MB (< GitHub's 100 MB limit). No `.git` today; `/srv` is not
  btrfs-snapshotted → weak fine-grained version history for the source-of-truth.
- **Plan:** private GitHub repo (Max has a paid account) + **hourly `git commit`/push** cron.
- **`.gitignore` is essential** — the 1.4 GB is mostly generated junk: 2462 `.ajson`
  (Smart Connections embeddings), 1252 `.pyc`, fonts, big PDFs. Ignore `.ajson`, `.pyc`,
  `.obsidian/workspace*`, plugin caches; big PDFs via gitignore or Git-LFS. Version the
  markdown + real attachments only.

---

## 10. Phasing (status 2026-08-28)
- **Phase 0 — complete.** OTS tooling smoke-tested and pinned on Python 3.12 (2026-05-31).
- **Phase 1 — complete**, built change-by-change through OpenSpec. Evidence: the archived changes
  in `openspec/changes/archive/` — `add-foundation`, `add-scanner`, `add-ots-notary`,
  `add-scheduler`, `add-web-panel`, `add-notifiers`, `add-manifest-import`, then `add-deep-verify`,
  `decouple-ots-stamping`, `improve-file-browser`, `order-scans-by-size`,
  `streamline-event-acknowledgement`, `add-rename-detection`, `add-folder-tree-and-scan-progress`,
  `offload-ots-subprocess`, `reconcile-interrupted-runs`, `tolerate-unencodable-paths`,
  `add-issue-review-and-recovery`, `rename-corpus-to-collection`, `auto-baseline-new-files`,
  `add-alert-deep-links`, `tolerate-unstampable-proof-paths`, `fix-ux-audit-sprint1`,
  `guard-proof-and-restore-integrity`. The engine, the notary, the scheduler and the single-user
  panel are live; `cairn status` is the one CLI verb still unbuilt.
- **Phase 2 — not started.** Multi-user mode is still a config flag: the ownership column, the
  `User` model and per-user scoping are in and exercised, but there is no login wall, no user
  administration and no admin-provisioned mounted bases (§4). The restic **witness plugin is
  deferred** — `src/witness/` is an empty package, and the review page's recovery guidance is
  deliberately backup-tool-agnostic instructions so the repo can go public without it.
- **Phase 3 — partial.** Packaging (`pyproject.toml`, `cairn-integrity`), Docker + compose,
  Caddyfile/Traefik examples, the Makefile deploy, README/DEPLOYMENT/SECURITY, an Apache-2.0
  LICENSE and a real test suite all exist; **CI does not** (no `.github/`), and the repo is still
  private pending the open-source decision.
- **Phase 4 — not started.** Browser-side client verify (vendor + audit the JS OTS lib for the
  verify page only) is untouched; the shipped verify page is server-side but requires **no upload**,
  since Cairn re-hashes a file it already watches.

---

## 11. Language decision — Python over Node (record, 2026-05-31)
Node/TypeScript was seriously evaluated (one language end-to-end; the appeal of *browser-side*
OTS verification where the user's file never leaves their machine). Rejected because:
- The JS OTS library (`opentimestamps` npm) is **effectively abandoned** — latest **0.4.9 on
  2021-01-29**, CI-tested only against **Node 6/7** (EOL ~2019), no native TypeScript types,
  ~587 downloads/wk. The Python `opentimestamps-client` shipped **0.7.2 on 2024-12-31** and
  provides the maintained `ots` CLI. For a data-integrity tool, unmaintained crypto code with
  no upstream is a disqualifier.
- The browser-verify win was built *on that abandoned lib*. We keep the option for **Phase 4**
  by vendoring + auditing the JS lib **only** for an isolated client-side verify page — the
  benefit without betting the backend on dead code.
- Python also lets us reuse Max's hardened Obsidian-MCP codebase and run one stack on the
  home server.

---

## 12. Open questions (status 2026-08-28)
1. **License** — ✅ **decided: Apache-2.0** (patent grant). Recorded by the shipped `LICENSE`.
2. **pip distribution name** — ✅ **decided: `cairn-integrity`** (`pyproject.toml`); the brand
   stays "Cairn".
3. **Alert cc policy** — **open.** Routing is per-collection (`collections.alert_json`), so a cc is
   expressible today, but no policy is recorded. Related constraint now known: a review deep link
   is only actionable by the collection's **owner**, so in `multi` mode a cc'd non-owner lands on
   "not found" (accepted limitation, `openspec/specs/alerting/`).
4. **Document collections finalization** — **open.** Exact include/exclude globs per collection are
   an operator decision held in the live instance, not in this repo.
5. **Verify backend default** — ✅ **decided: block-explorer** (`CAIRN_VERIFY_BACKEND=explorer`,
   blockstream.info), configurable to a node. Forced as much as chosen — the `ots` CLI can only
   verify against a node (§6). Recorded in `openspec/specs/ots-notarization/` and CLAUDE.md.
6. **Calendar servers** — ✅ **decided for now: the public OTS calendars** ship as the default
   (`a`/`b.pool.opentimestamps.org`, `a.pool.eternitywall.com`, `ots.btc.catallaxy.com`), operator-
   overridable. Self-hosting an aggregator was not pursued.
7. **Witness plugin generality** — **open, and deferred**: no witness plugin exists at all
   (`src/witness/` is empty), so the restic-vs-general question has not had to be answered.
8. **Secrets** — **partly decided.** Env (or a secret file) remains the rule; the deliberate
   exception is the **SMTP password, which lives in the DB** (`app_settings`) so the server can be
   configured from the panel — a homelab trade recorded in CLAUDE.md. Keyring / age-sops were not
   pursued. The scaffolded Signal CallMeBot key should move to env before that channel is enabled.

---

## 13. Reference material
- Sibling codebase to mine for patterns/code: **the sibling FastAPI app** (FastAPI multi-user
  self-hosted panel).
- OTS tooling: `opentimestamps-client` (PyPI, the `ots` CLI), `python-opentimestamps` (lib).
