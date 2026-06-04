## 1. Packaging & dependencies

- [x] 1.1 Create `pyproject.toml` (project metadata, `cairn` console_script → `src.cli:main`, build via setuptools/hatchling) and a pinned `requirements.txt`: fastapi, uvicorn[standard], sqlalchemy[asyncio], aiosqlite, alembic, pydantic-settings, passlib[bcrypt], itsdangerous, jinja2, python-multipart, httpx, pyyaml, opentimestamps-client. Add `requirements-dev.txt` (pytest, pytest-asyncio, ruff, pip-audit).
- [x] 1.2 Create `src/__init__.py` and the package skeleton dirs (`src/models/`, `src/services/`, `src/notify/`, `src/witness/`, `src/auth/`, `src/api/`, `src/control_panel/`) each with `__init__.py`.

## 2. Configuration (`src/config.py`)

- [x] 2.1 pydantic-settings `Settings` reading `CAIRN_*` env (+ optional `.env`): `auth_mode` (single|multi, default single), `database_url` (default `sqlite+aiosqlite:///./data/cairn.db`), `proof_store_path` (default `./proofs`), `secret_key`, `session_cookie_name`, `session_max_age`, `ots_calendars` (list, default public pools), `verify_backend` (explorer|node, default explorer) + `explorer_url` (blockstream.info) / `node_rpc_url`, `auto_migrate` (bool), `incomplete_proof_alarm_days` (default 7).
- [x] 2.2 Support an optional YAML overlay path (`CAIRN_CONFIG_FILE`) merged under env precedence. Provide `config.example.yaml`. Never read secrets from the repo; document env/secret-file usage.
- [x] 2.3 Provide a cached `get_settings()` accessor. Add a smoke assertion that a missing `secret_key` in `multi` mode raises a clear error.

## 3. Datastore (`src/database.py`, `src/models/db.py`)

- [x] 3.1 `src/models/db.py`: SQLAlchemy 2.x `DeclarativeBase` + ORM for `users`, `corpora`, `files`, `runs`, `events` per DESIGN.md §5 / `openspec/config.yaml`. Use TEXT for JSON blobs (`exclude_globs_json`, `alert_json`) and for enums with CHECK constraints. `ots_mode` ∈ {none, perfile}. UTC `DateTime`. FKs: `corpora.user_id`→users, `files.corpus_id`→corpora (CASCADE), `events.corpus_id`/`events.file_id`. Unique `(corpus_id, relpath)` on files. Indexes on `files.status`, `events.acknowledged_at`, `runs.corpus_id`.
- [x] 3.2 `src/database.py`: `create_async_engine(settings.database_url)`; register a `connect` event listener on `engine.sync_engine` issuing `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`. `async_sessionmaker` + `get_session()` async-generator dependency. A `ping()`/`init_db()` helper.
- [x] 3.3 Single-user bootstrap helper `ensure_implicit_user(session)` that, in `single` mode, inserts the implicit `users` row (id=1, username from `CAIRN_SINGLE_USER` or "local", is_admin=1, is_active=1) if absent.

## 4. Migrations (async Alembic)

- [x] 4.1 `alembic.ini` + `alembic/env.py` adapted from `obsidian_mcp` for `sqlite+aiosqlite` async (run_migrations_online via async engine; `render_as_batch=True` for SQLite ALTER support). Point `target_metadata` at the models' `Base.metadata`.
- [x] 4.2 Initial migration `0001_initial` creating all five tables, FKs, CHECK constraints, unique + secondary indexes. `alembic upgrade head` succeeds on a fresh file; `alembic downgrade base` drops cleanly.

## 5. App runtime (`src/main.py`)

- [x] 5.1 FastAPI app with `lifespan`: on startup open the engine, run `alembic upgrade head` when `auto_migrate`, call `ensure_implicit_user`, and call a `start_scheduler()` no-op hook (placeholder for the scheduler change); cancel/close on shutdown. Mirror the obsidian_mcp lifespan structure.
- [x] 5.2 `GET /healthz` → JSON `{status, mode, version, scans: {stub}}`, 200 when DB reachable, 503 when not. (Real scan-freshness lands with the scheduler.)
- [x] 5.3 Mount Jinja2 templates (`src/control_panel/templates/`) + `/static` (`src/control_panel/static/`) with a placeholder `index.html` ("Cairn — panel coming soon") so the app serves something. Add CSRF/session middleware seams (config from settings) without enforcing login yet.

## 6. CLI (`src/cli.py`)

- [x] 6.1 Entrypoint `main()` with subcommands `init`, `serve`, `scan`, `accept`, `verify`, `export`, `status`, `upgrade`, `add-corpus`. `init` creates `data/` + `proofs/` dirs and runs migrations; `serve` runs uvicorn on configured host/port. The remaining subcommands print "not yet implemented (see roadmap)" and exit 2.
- [x] 6.2 `cairn --version` and `cairn --help` work. Wire the console_script from §1.1.

## 7. Deployment scaffold

- [x] 7.1 `Dockerfile`: `python:3.12-slim`, `apt` curl + (build deps for opentimestamps if needed), non-root `appuser` (uid 1000), install `requirements.txt`, install the `ots` CLI, copy `alembic*`/`src/`, `EXPOSE 8000`, CMD uvicorn `src.main:app`. Verify `ots --version` in the image.
- [x] 7.2 `docker-compose.yml`: `cairn` service from `${REGISTRY}/cairn:latest`, `restart unless-stopped`, `cap_drop: ALL`, `no-new-privileges`, resource limits, `env_file: .env`. Volumes: example corpus mounts as **`:ro`**, plus `${DATA_HOST_PATH}:/app/data` and `${PROOFS_HOST_PATH}:/app/proofs` read-write, timezone mounts. Healthcheck hits `/healthz`. Comment the read-only-corpus / writable-data invariant (DESIGN §4).
- [x] 7.3 `Caddyfile.example` (TLS reverse proxy to `cairn:8000`, app owns its own auth) and `.env.example` (CAIRN_* keys incl. `CAIRN_HOSTNAME`, generated `SECRET_KEY` guidance, host paths). Add `config.example.yaml`.
- [x] 7.4 `Makefile` modeled on `obsidian_mcp/Makefile`: `-include Makefile.local`; vars `IMAGE_NAME=cairn`, `REGISTRY?=localhost:5000`, `DEPLOY_DIR?=.`, `DATA_DIR?=./data`, `COMPOSE` macro. Targets: `help init build build-cached push image deploy up down restart logs shell db-backup status clean audit`. `db-backup` = `sqlite3 $(DATA_DIR)/cairn.db ".backup ..."` + gzip. `deploy: image` then backup + `compose up -d --force-recreate` + prune.
- [x] 7.5 `Makefile.local` (gitignored) with `DEPLOY_DIR := /srv/cairn` and `DATA_DIR := /srv/cairn` for Max's host. Confirm `.gitignore` already ignores `Makefile.local` (it does) and add if missing.

## 8. Verification

- [x] 8.1 Smoke test (`tests/test_foundation_smoke.py` or a runnable script): fresh venv → `pip install -r requirements.txt` → `cairn init` → assert `data/cairn.db` exists; open it and assert `PRAGMA journal_mode` = wal and `PRAGMA foreign_keys` = 1 and the five tables exist. Start `cairn serve` and assert `GET /healthz` returns 200 with `mode=single`.
- [x] 8.2 `docker build` succeeds and `ots --version` runs in the image (CI-friendly; can be a documented manual step if Docker is unavailable in the build env).
- [x] 8.3 `openspec validate add-foundation --strict` passes. Spawn the `openspec-verifier` agent against the diff + spec deltas; resolve any drift.
- [x] 8.4 Update `CLAUDE.md` "Commands" section: mark `init`/`serve` as implemented, the rest stubbed. Archive the change.
