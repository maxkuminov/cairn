# Deploying Cairn

Cairn ships as a Docker image fronted by a reverse proxy. This guide covers a full
self-hosted deployment. Two things are easy to get wrong and matter most, so read these first.

> ### ⚠️ Two requirements that aren't optional
>
> 1. **Watched folders must be mounted read-only (`:ro`).** Cairn is an integrity tool; it
>    must not be able to become the threat it watches for. Every folder you monitor is mounted
>    into the container read-only so Cairn physically cannot modify or delete it. The SQLite DB
>    and `.ots` proofs live on a **separate writable** volume — never on the watched mounts.
>
> 2. **You must front Cairn with a reverse proxy that handles authentication.** In single-user
>    mode (the default) Cairn has **no in-app login wall** — anyone who can reach it has full
>    access. Do not expose it to the internet without an authenticating proxy in front. The one
>    exception is `/healthz`, which should stay public so an uptime monitor can poll it.
>    (Multi-user in-app login is Phase 2 and not yet shipped.)

## Prerequisites

- A Linux host with **Docker** and the Docker Compose plugin.
- A reverse proxy: either the **Traefik** setup shown in the example compose, or the bundled
  **Caddy** alternative (simplest — auto-issues a Let's Encrypt cert).
- A writable directory on the host for Cairn's state (DB + proofs). Everything else
  (the SQLite index) is rebuildable; the durable guarantee is your file bytes + their `.ots`.

## Quick start

```bash
git clone <this-repo> cairn && cd cairn

# 1. Config
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # paste into CAIRN_SECRET_KEY
# edit .env: CAIRN_HOSTNAME, DATA_HOST_PATH, PROOFS_HOST_PATH

# 2. Compose (your real compose is gitignored so host paths never get committed)
cp docker-compose.example.yml docker-compose.yml
# edit docker-compose.yml: add a read-only mount per folder you want to watch (see below)

# 3. Bring it up
make deploy      # build, security-scan, back up the DB, recreate the container
make migrate     # run DB migrations (only needed when a release adds one)
```

Then visit your `CAIRN_HOSTNAME`, create a collection pointing at one of your mounts, and run a
scan. Confirm health with `curl https://<host>/healthz` (or `make status`).

> Don't use `make`? It just wraps `docker compose`. The equivalent is
> `docker compose up -d --build`, plus `docker compose exec cairn alembic upgrade head` for
> migrations.

## Mounts in detail

Edit the `volumes:` list in your `docker-compose.yml`:

```yaml
volumes:
  # WATCHED FOLDERS — read-only. One line per folder you want Cairn to monitor.
  # Host paths with spaces must be quoted. The container-side path (after the
  # colon) is what you'll enter as the collection root in the panel.
  - "/path/to/your/photos:/corpora/photos:ro"
  - "/path/to/your/documents:/corpora/documents:ro"

  # WRITABLE STATE — separate volume, never the watched mounts.
  - ${DATA_HOST_PATH:-./data}:/app/data        # SQLite DB (WAL)
  - ${PROOFS_HOST_PATH:-./proofs}:/app/proofs  # .ots proof store
```

When you add a collection in the panel, its **root** is the container-side path
(e.g. `/corpora/photos`), not the host path. In multi-user mode (Phase 2) each user's roots are
jailed under an admin-provisioned base mount.

## Reverse proxy & authentication

Pick whichever proxy you run. Both are pre-wired in the examples.

### Traefik (shown in `docker-compose.example.yml`)

The example uses Traefik labels with two routers:

- A high-priority **public** router for `/healthz` only (no auth — your uptime monitor polls it).
- A main router for everything else, behind an auth middleware (`chain-oauth@file` in the
  example, e.g. a Google-OAuth forward-auth chain). Replace it with whatever auth middleware
  your Traefik provides.

### Caddy (see `Caddyfile.example`)

Simplest path — Caddy terminates TLS and proxies to the container. For single-user installs,
`Caddyfile.example` includes commented blocks for gating the front door with **HTTP basic auth**
or an **IP allowlist**. Uncomment one. (In multi-user mode you can proxy straight through, since
Cairn enforces login itself.)

## Configuration

All settings come from environment variables (prefix `CAIRN_`); see `.env.example` for the full
annotated list. The ones that matter most for a deployment:

| Variable | Purpose |
|---|---|
| `CAIRN_AUTH_MODE` | `single` (no login wall — needs a proxy) or `multi` (Phase 2). |
| `CAIRN_SECRET_KEY` | Session-cookie signing key. The app refuses to start if empty/placeholder. |
| `CAIRN_HOSTNAME` | Public hostname your proxy routes to. |
| `CAIRN_DATABASE_URL` | SQLite path on the writable volume (default `sqlite+aiosqlite:////app/data/cairn.db`). |
| `CAIRN_PROOF_STORE_PATH` | `.ots` proof store on the writable volume. |
| `CAIRN_VERIFY_BACKEND` | `explorer` (works out of the box) or `node` (your own Bitcoin node, fully trustless). |
| `CAIRN_SCHEDULER_ENABLED` | `1` for the in-process scan loop; `0` for cron-only (see below). |
| `CAIRN_AUTO_MIGRATE` | `1` runs migrations on startup; `0` to manage them with `make migrate`. |

SMTP alert settings can be set via `CAIRN_SMTP_*` env vars **or** in the panel (Settings →
Notifications); panel values are stored in the DB and override the env defaults.

## Operations

| Command | What it does |
|---|---|
| `make deploy` | Build → scan → back up DB → recreate the container. |
| `make migrate` | `alembic upgrade head` (run after a deploy that adds a migration). |
| `make logs` / `make status` | Tail logs / show health. |
| `make db-backup` | Online SQLite backup. |

Host-specific values (e.g. a remote `DEPLOY_DIR`) go in a gitignored `Makefile.local` — see the
top of the `Makefile`.

### Cron-only (no in-process scheduler)

Set `CAIRN_SCHEDULER_ENABLED=0` and drive scans from system cron instead:

```cron
*/15 * * * *  docker compose exec -T cairn cairn scan --once
0    3 * * *  docker compose exec -T cairn cairn upgrade
```

`/healthz` freshness still works — it reads the `runs` table regardless of who writes it.

## Health & monitoring

`/healthz` is the dead-man's switch. It returns:

- `200 ok` — reachable and every collection has scanned recently.
- `503 degraded` — a collection has gone stale (missed its expected scan window).
- `503 error` — the datastore is unreachable.

Point an uptime monitor (e.g. Uptime Kuma) at it. Keep it on the **unauthenticated** route so the
monitor can reach it without credentials.
