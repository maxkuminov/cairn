# Cairn — Design

> **Status:** design spec. No implementation yet — the build happens in a separate
> session. A fresh session can pick the project up from this document alone.
> Authored 2026-05-31; revised 2026-05-31 (web-panel pivot, language decision, name).

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
photos (see §10); Cairn generalizes it into a reusable, multi-user product.

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
| Product name | **Cairn** | Durable marker (provenance) you'd notice if disturbed (integrity); carries both halves. PyPI `cairn` is a dead-ish minor tool, so the *distribution* name may need a suffix (open Q) — the brand is clean |
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
| Verify backend | **Block-explorer lookup by default (configurable to a Bitcoin node)** | Self-hosters get working verify out of the box ("trust the lookup"); node owners get fully trustless. Browser-side verify deferred (§11) |
| Codebase | **New standalone Python project** (a new project dir → rename to `cairn`) | Supersedes the bash photo script, which keeps running until parity then migrates |
| Dev model | **Build private, open-source when stable** | Protect Max's data now; public release is battle-tested |
| Obsidian | **Separate track, NOT in Cairn** | High churn → OTS is noise. Use private GitHub repo + hourly git commit/push instead (see §9) |
| Core vs personal | **Clean separation** | Max's CallMeBot/s-nail/Kuma/restic/paths are *config & optional plugins*, never hardcoded |

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

```
cairn/
  src/
    main.py             # FastAPI app, lifespan (start scheduler), mount panel + api
    config.py           # pydantic-settings (env-driven; CAIRN_AUTH_MODE, paths, calendars…)
    database.py         # async SQLAlchemy engine/session (aiosqlite + WAL pragma)
    models/db.py        # ORM: users, collections, files, runs, events
    auth/               # session auth, password hashing, login/register routes (from obsidian_mcp)
    services/
      scanner.py        # walk → diff vs `files` → hash changed → classify (added/modified/missing)
      ots.py            # stamp / upgrade / verify (wraps the `ots` CLI)
      scheduler.py      # per-collection scan cadence (staggered) + daily OTS `upgrade` pass + heartbeat
      proofs.py         # parallel .ots store, export bundles
    notify/             # pluggable notifiers
      smtp.py  signal_callmebot.py  webhook.py  ntfy.py  kuma_push.py
    witness/            # optional external-witness checks
      restic.py         # `restic backup --force` + `diff` vs backup (optional witness)
    api/routes.py       # REST/htmx endpoints for the panel
    control_panel/      # jinja2 templates + static assets
    cli.py              # click/argparse entrypoint (init/scan/accept/verify/export/status/upgrade/serve)
  alembic/              # migrations
  Dockerfile  docker-compose.yml  Caddyfile.example  Makefile
  config.example.yaml  pyproject.toml  README.md  CLAUDE.md  LICENSE  tests/  .github/workflows/
```

### SQLite schema (sketch)
- `users(id, username, password_hash, is_admin, is_active, created_at, last_login_at)`
  — single-user mode uses one implicit row.
- `collections(id, user_id, name, root, mode[worm|churn], hash_cadence_seconds,
  ots_mode[none|manifest|perfile], exclude_globs_json, alert_json, created_at)`
  — `root` must resolve under the owning user's mounted base.
- `files(id, collection_id, relpath, size, mtime, sha256, first_seen, last_checked, last_changed,
  status[ok|new|modified|missing], ots_path, ots_state[none|pending|incomplete|complete],
  ots_stamped_at)`
- `runs(id, collection_id, started, finished, added, modified, missing, stamped, upgraded, result)`
  — audit trail / dead-man's-switch source.
- `events(id, collection_id, file_id, kind[added|modified|missing|restored], detected_at,
  acknowledged_at, acknowledged_by)` — nag-until-accept lifecycle + the panel's alert feed.

### Per-run flow (one collection)
walk root → diff vs `files` by relpath → fast-path on size+mtime, SHA-256 only the changed →
classify (added / modified / **missing→alert**) → write `events` → per policy: stamp
new/changed, `ots upgrade` any incomplete → route alerts per user/collection → emit heartbeat →
write `runs` row.

### Scheduler
Background task in the FastAPI lifespan (the Obsidian-MCP indexer pattern), but **per-collection
cadence**, staggered — you cannot full-rescan 186k files every 5 min. E.g. documents every
15 min, photos nightly. A separate **daily `ots upgrade` job** completes pending proofs once
Bitcoin confirms. Heartbeat ping after each run (dead-man's-switch).

### Web panel (pages)
- **login / register** (multi-user only)
- **dashboard** — per-collection status cards (last scan, ok/modified/missing counts, OTS
  pending/complete), recent `events` feed, heartbeat status
- **collection detail** — file list with status; **accept / re-baseline** action; per-file OTS state
- **add / edit collection** — pick root (within allowed base), mode, OTS mode, exclude globs, alerts
- **verify** — drop a file + `.ots` → verify against the configured block source (server-side
  in v1; browser-side later, §11)
- **settings** — per-user notification channels; **admin:** user management + mounted bases

### CLI (headless / cron / ops)
`cairn init | scan [--collection X] | accept [--collection X] | verify <file> | export <file> |
status | upgrade | add-collection | serve`. `--once` for cron; `serve` runs the panel.

### Config (env + YAML) — captures everything currently hardcoded
auth mode · datastore path · proof-store path · OTS calendars · verify backend (explorer URL
or node RPC) · notifier credentials (from env/secret file, NOT in repo) · per-user/collection
policy + alert routing · optional witness (restic) config · optional Kuma heartbeat URL.

---

## 6. OpenTimestamps handling (key facts for the implementer)
- Stamp → **incomplete** proof immediately (calendar-signed); run `upgrade` after the Bitcoin
  tx confirms (~hours; batched) to bake the **complete** Bitcoin path. The incomplete state is
  the only fragile window — alarm if a proof stays incomplete past N days (calendar never
  confirmed).
- Store **file + upgraded `.ots`** together in the parallel store; exportable on demand.
- Verification needs the file + `.ots` + a Bitcoin block source. **Default: a block-explorer
  lookup** (e.g. blockstream.info — "trust the lookup"); **configurable to your own Bitcoin
  node** for fully trustless verification.
- Per-file is cheap at the chain level (the calendar Merkle-aggregates many submissions into
  one tx); the client can stamp many files per call.
- **De-risk early:** before building `ots.py`, smoke-test `ots stamp/upgrade/verify` on Python
  3.12 and pin `opentimestamps` / `opentimestamps-client`.

---

## 7. What we reuse from the Obsidian-MCP server (the sibling FastAPI app)
Cairn is the same *shape* of app, so we lift patterns (and where clean, code):
- **Stack:** FastAPI / uvicorn / SQLAlchemy async / Alembic / Jinja2 + htmx + Tailwind.
- **Auth:** session login, password hashing (`passlib[bcrypt]`), `User` model with
  `is_admin`/`is_active`, per-user scoping (their `vault_path` → our per-user collections). Drop the
  API-key + OAuth2/PKCE layers (MCP-specific; Cairn v1 needs only the panel session auth).
- **Indexer → scanner:** the "run on startup, then on a cadence, hash-based change detection"
  loop is exactly our scan/diff/hash; we change the payload from "embed notes" to
  "classify + stamp".
- **Atomic writes** (`write_file` tmp + `os.replace`), CSRF, rate limiting, timing-safe compares.
- **Ops:** Docker + reverse proxy (we use **Caddy** — simpler self-host, app handles its own
  auth) + Makefile deploy + the "host paths live outside the public tree" discipline + OpenSpec.
- **Drop the Postgres-isms** (JSONB/ARRAY/TSVECTOR/pgvector) — Cairn's models are plain columns
  + JSON blobs, SQLite-friendly.

---

## 8. Migration from the already-built photo tripwire (do not regress)
A working bash tripwire protects the photos **right now** and must keep running until the
Python engine reaches parity:
- `/srv/scripts/photo-integrity.sh` (+ `photo-integrity.env`, chmod 600)
- State: `/srv/integrity/{manifest.tsv, pending-deletions.tsv, photo-integrity.log}`
- Cron (example): daily `check` 05:00, monthly `verify-backup` (restic) 1st 14:00
- Uptime Kuma push monitor **#85** "Photo Integrity Tripwire" (dead-man's-switch)
- Modes: `check` (delete/edit detection, Signal+email, nag-until-`accept`), `verify-backup`
  (restic `--force` + `diff` independent witness), `accept` (re-baseline)

**Migration:** Cairn imports the existing `manifest.tsv` rows into the `files` table under
the Photos collection (no re-hash). Everything imported is "pre-existing — don't stamp"; only
files first-seen *after* import get per-file OTS. The import is a first-class, tested step
(the whole "stamp new photos only" rule hinges on it). Then the bash script is retired.

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

## 10. Suggested phasing (for the build session)
- **Phase 0 (in flight):** finish photo baseline hash + one-time trust-check; Obsidian→GitHub
  git auto-commit; **smoke-test the OTS Python tooling on 3.12.** *Independent of this spec.*
- **Phase 1 (MVP):** Python engine core — config, SQLite (WAL), scanner (scan/diff/hash),
  per-file OTS (stamp/upgrade), SMTP+Signal+Kuma notifiers, and the **single-user web panel**
  (dashboard, collection detail, add/edit collection, verify page). Parity with the photo tripwire
  (import `manifest.tsv`).
- **Phase 2:** multi-user mode (login, admin role, per-user scoping, read-only mounts) +
  per-user alert routing → onboard Bob (tax) and Carol (ROMs). restic "witness" plugin.
- **Phase 3:** packaging (pyproject, pipx, Docker, Caddy, Makefile), README, tests, CI,
  LICENSE → open-source.
- **Phase 4:** browser-side client verify (vendor + audit the JS OTS lib for the verify page
  only); optional richer panel features.

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

## 12. Open questions (decide during the build session)
1. **License** — MIT vs Apache-2.0 (Apache-2.0 gives a patent grant; lean Apache-2.0).
2. **pip distribution name** — `cairn` is taken by a dead-ish minor tool; pick a suffix
   (`cairn-integrity`?) or coined variant. Brand stays "Cairn".
3. **Alert cc policy** — Bob → bob@ only, or cc the admin for oversight? Carol → cc
   the admin by default (set); confirm.
4. **Document collections finalization** — exact include/exclude globs; confirm scopes; ensure the
   Obsidian vault is excluded.
5. **Verify backend default** — block-explorer (blockstream.info) acceptable as the shipped
   default, or require a node?
6. **Calendar servers** — default public OTS calendars, or also self-host an aggregator?
7. **Witness plugin generality** — restic-specific now; generalize later (borg, rclone)?
8. **Secrets** — env file vs OS keyring vs age/sops-encrypted config.

---

## 13. Reference material
- Sibling codebase to mine for patterns/code: **the sibling FastAPI app** (FastAPI multi-user
  self-hosted panel).
- OTS tooling: `opentimestamps-client` (PyPI, the `ots` CLI), `python-opentimestamps` (lib).
