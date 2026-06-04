## Context

Cairn is a single-writer integrity tool: the scanner is the only writer to SQLite; the panel
reads (and writes light user/ack state). DESIGN.md locks SQLite (WAL) over Postgres precisely so
a safety tool runs with no DB service. The sibling `obsidian_mcp` provides the FastAPI/auth/
session/lifespan patterns to lift, but it is Postgres-based — the datastore layer is the main
adaptation.

## Decisions

### D1 — SQLite pragmas applied per-connection via an engine event listener
`aiosqlite` opens a fresh connection per pool checkout; pragmas are connection-scoped. Use
SQLAlchemy's `event.listens_for(engine.sync_engine, "connect")` to issue, on every connection:
`PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`,
`PRAGMA synchronous=NORMAL`. WAL keeps panel reads concurrent with the scanner's writes;
`foreign_keys=ON` is required because SQLite defaults it OFF (so the `corpora.user_id` /
`files.corpus_id` / `events.*` FKs actually enforce).

### D2 — `NullPool`-free default, single connection discipline
SQLite + WAL tolerates one writer + many readers. Use the default pool but keep writes
serialized in application code (the scanner). No Postgres pool args (`pool_size`,
`max_overflow`, `pool_pre_ping`) — they don't apply.

### D3 — Schema = plain columns + JSON-as-TEXT
`exclude_globs_json` and `alert_json` are stored as TEXT containing JSON (no `JSON`/`JSONB`
type). Enums (`mode`, `ots_mode`, `status`, `ots_state`, `kind`, `result`) are stored as TEXT
with a `CHECK` constraint or app-level validation. Timestamps are timezone-aware UTC stored as
ISO-8601 TEXT (SQLite has no native datetime) via SQLAlchemy `DateTime`. `ots_mode` is
`none|perfile` only (manifest removed per the design handoff).

### D4 — Root jailing is enforced in app logic, not the DB
`corpora.root` must resolve under the owning user's mounted base. The DB stores the resolved
absolute path; validation (realpath + `is_relative_to(base)`, reject traversal/symlink escape)
lives in the corpus-create path (later change) and is re-validated server-side. This change only
defines the column + the base-path config seam.

### D5 — Dual-mode auth groundwork now, login later
`CAIRN_AUTH_MODE=single` ensures one implicit user (id=1, `username="local"`, `is_admin=1`) at
startup and skips the login wall. `multi` mode's login/admin UI is a later change, but every
`corpora` row carries `user_id` from the first migration so Phase 2 is additive (no schema
rewrite, no data backfill).

### D6 — Async Alembic
Mirror `obsidian_mcp/alembic/env.py`'s async pattern, swapping the URL to `sqlite+aiosqlite`.
Migrations are the source of truth for schema; `Base.metadata.create_all` is used only by the
in-process test smoke if needed. On container start, `CAIRN_AUTO_MIGRATE=1` runs
`alembic upgrade head` before uvicorn binds.

### D7 — Deploy layout mirrors obsidian_mcp, adapted for SQLite + read-only corpora
- `Makefile` includes a gitignored `Makefile.local` for host paths; defaults `DEPLOY_DIR=.`,
  `DATA_DIR=./data`. Max's host sets `DEPLOY_DIR=/srv/cairn`.
- `make deploy` = `build → push → db-backup → compose up -d --force-recreate → prune`.
- `db-backup` copies the SQLite file via `sqlite3 <db> ".backup <dest>"` (consistent online
  backup) instead of `pg_dump`; no `db-init` step (Alembic handles schema).
- `docker-compose.yml`: corpus host paths mounted **`:ro`**; `data/` (DB) and `proofs/` (`.ots`
  store) mounted read-write on a separate volume. Container runs as non-root, `cap_drop: ALL`,
  `no-new-privileges`. Frontable by Caddy (shipped example) or an existing Traefik like Max's.
- `Dockerfile` installs the `ots` CLI (`opentimestamps-client`) so the notary service can
  subprocess it.

### D8 — `/healthz` is poll-model and stubbed-then-grown
Per the design handoff, external monitors **poll** `/healthz` (the push-heartbeat model was
dropped). This change returns `{"status":"ok","scans":{...}}` where the scans block is a stub
(no runs yet); the scheduler change fills in real freshness (oldest successful run age vs.
each corpus cadence) and flips status to `degraded` when stale.

## Risks / Trade-offs

- **WAL on networked filesystems** can misbehave. The DB volume is a local writable volume
  (not the read-only corpus mount), so this is fine — documented in `.env.example`.
- **Single-writer assumption**: if a future feature needs concurrent writers, revisit. For now
  the scanner serializes writes; panel mutations (ack/accept) are small and brief.
- **CLI stubs returning non-zero** could surprise cron if wired early — documented; only `init`/
  `serve` are wired this phase.
