## Why

Cairn is pre-implementation: the repo holds only the design (`DESIGN.md`, `docs/design/`).
Before any feature (scanner, OTS notary, scheduler, panel) can land, the project needs a
runnable skeleton: configuration, an async SQLite datastore with the locked schema, the FastAPI
app + lifespan, a CLI entrypoint, and a deployment path. This change establishes that foundation
so every later change is purely additive.

It also stands up `make deploy` (modeled on the sibling `obsidian_mcp` project) now, while the
surface is small, so the deploy pipeline matures alongside the code rather than being bolted on
at the end. Per DESIGN.md §3/§4, watched corpus folders are mounted **read-only** and the DB +
proof store live on a **separate writable volume** — that security invariant is encoded in the
container/compose layout from day one.

References: DESIGN.md §3 (locked decisions), §4 (deployment/multi-user model), §5 (architecture,
SQLite schema), §7 (reuse from obsidian_mcp).

## What Changes

- **Packaging**: `pyproject.toml` + pinned `requirements.txt` (FastAPI, uvicorn, SQLAlchemy
  async, aiosqlite, Alembic, pydantic-settings, passlib[bcrypt], itsdangerous, Jinja2, httpx,
  PyYAML, opentimestamps-client). `cairn` console entrypoint → `src/cli.py`.
- **Configuration** (`src/config.py`): pydantic-settings reading env (`CAIRN_*`). `CAIRN_AUTH_MODE`
  = `single` (default) | `multi`. Datastore path, proof-store path, OTS calendars, verify backend
  (default block-explorer), session secret — all env/file driven, never hardcoded. Optional YAML
  overlay.
- **Datastore** (`src/database.py`, `src/models/db.py`): async SQLAlchemy 2.x engine over
  `sqlite+aiosqlite`, with `journal_mode=WAL` + `foreign_keys=ON` + `busy_timeout` set per
  connection. ORM for the five locked tables: `users`, `corpora`, `files`, `runs`, `events`
  (plain columns + JSON-text blobs; no Postgres types). `get_session()` FastAPI dependency.
- **Migrations**: async Alembic wired to the models; initial migration creating all five tables
  and their indexes/constraints. Runs on startup if `CAIRN_AUTO_MIGRATE=1`.
- **App runtime** (`src/main.py`): FastAPI app, `lifespan` that opens the DB, ensures schema, and
  reserves a hook to start the scheduler (no-op stub for now). `GET /healthz` returns liveness +
  (stubbed) scan-freshness JSON. Jinja2 templates + `/static` mounted (empty placeholder panel).
- **CLI** (`src/cli.py`): argparse/click entrypoint with the full command surface stubbed —
  `init`, `serve`, `scan`, `accept`, `verify`, `export`, `status`, `upgrade`, `add-corpus`.
  `init` and `serve` are functional; the rest print "not yet implemented" and exit non-zero so
  later changes can fill them in without changing the surface.
- **Single-user bootstrap**: in `single` mode, one implicit user row is ensured at startup so
  later corpora always have an owner. `multi`-mode login is deferred to a later change but the
  `user_id` FK exists from the start.
- **Deployment**: `Dockerfile` (python:3.12-slim, non-root, installs the `ots` CLI),
  `docker-compose.yml` (read-only corpus mounts, writable `data/` + `proofs/` volumes, Caddy/
  Traefik-frontable), `Caddyfile.example`, `.env.example`, and a `Makefile` with
  `init/build/push/deploy/up/down/logs/shell/db-backup/status/clean/audit` — `make deploy` =
  build → push → backup (SQLite copy) → `compose up -d --force-recreate`. Host-specific paths
  live in a gitignored `Makefile.local` (`DEPLOY_DIR=/srv/cairn`).

### Out of scope (deferred to later changes)

- The scanner, OTS service, scheduler logic, notifiers, and the full web panel — this change
  only stubs their seams (lifespan hook, CLI commands, empty panel mount).
- Multi-user login/admin/registration UI and per-user scoping enforcement (Phase 2).
- The photo `manifest.tsv` import (its own change).
- TLS/reverse-proxy specifics beyond example configs.

## Capabilities

### New Capabilities

- `configuration`: env/YAML-driven settings with `CAIRN_AUTH_MODE` single|multi, datastore/proof
  paths, OTS calendars, verify backend, and a session secret — no secrets or host paths hardcoded.
- `datastore`: async SQLite (WAL + `foreign_keys` enforced) holding the five locked tables, a
  single-writer model, and an async session dependency + Alembic migrations.
- `app-runtime`: the FastAPI application, its lifespan, the `/healthz` endpoint, and the `cairn`
  CLI command surface.
- `deployment`: containerized deploy with read-only corpus mounts on a separate writable
  data/proof volume, and a `make deploy` pipeline.

### Modified Capabilities

None (first change).

## Impact

- **Code**: new `src/` tree (`main.py`, `config.py`, `database.py`, `models/db.py`, `cli.py`,
  `control_panel/` placeholder), `alembic/` + `alembic.ini`, `pyproject.toml`,
  `requirements.txt`.
- **Database**: creates `data/cairn.db` (gitignored) with the five tables on first run.
- **Ops**: `Dockerfile`, `docker-compose.yml`, `Caddyfile.example`, `.env.example`, `Makefile`,
  gitignored `Makefile.local`.
- **Dependencies**: first dependency set pinned; `ots` CLI installed in the image (de-risked on
  3.12 — CLI v0.7.2 stamps successfully).
- **Tests**: a startup smoke test (`cairn init` then `cairn serve` → `/healthz` 200; pragmas
  assert WAL + FK on).
