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
> 2. **Something must authenticate the panel.** In single-user mode (`CAIRN_AUTH_MODE=single`,
>    the default) Cairn has **no in-app login wall** — anyone who can reach it has full access, so
>    front it with a reverse proxy that enforces authentication. Alternatively run
>    `CAIRN_AUTH_MODE=oidc`, where Cairn enforces its own single sign-on login against any
>    standards-compliant OpenID Connect provider (see [Authentication](#authentication)). Either
>    way, `/healthz` stays public so an uptime monitor can poll it, and the container port must be
>    reachable **only** by the reverse proxy.

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

In `oidc` mode the auth middleware on the main router becomes optional: keep it during the
[rollout](#rollout-alongside-your-existing-gate-then-remove-it), then delete that one
`middlewares=` label once in-app login is verified. The public `/healthz` router does not change.

### Caddy (see `Caddyfile.example`)

Simplest path — Caddy terminates TLS and proxies to the container. For single-user installs,
`Caddyfile.example` includes commented blocks for gating the front door with **HTTP basic auth**
or an **IP allowlist**. Uncomment one. In `oidc` mode you proxy straight through once the in-app
login is verified (see the [rollout](#rollout-alongside-your-existing-gate-then-remove-it)),
since Cairn enforces login itself; the example also shows a log filter that keeps the OIDC
callback's query string out of Caddy's access log.

### The container port must be reachable only by the proxy

Both entrypoints (`cairn serve` and the image's `uvicorn` command) trust `X-Forwarded-For` /
`X-Forwarded-Proto` from **any** peer (`--proxy-headers --forwarded-allow-ips '*'`). Keep port
8000 on a private network shared with the reverse proxy only (the Caddy example uses `expose`,
never `ports`). Cairn uses forwarded headers for nothing security-relevant except the per-address
login rate limit, and never to build the OIDC redirect URI — but a peer that reaches the port
directly can spoof its address, bypass any proxy-side gate and, in `single` mode, owns the panel.

## Configuration

All settings come from environment variables (prefix `CAIRN_`); see `.env.example` for the full
annotated list. The ones that matter most for a deployment:

| Variable | Purpose |
|---|---|
| `CAIRN_AUTH_MODE` | `single` (no login wall — needs an authenticating proxy), `oidc` (in-app single sign-on, see [Authentication](#authentication)) or `multi` (Phase 2 placeholder). |
| `CAIRN_SECRET_KEY` | Session-cookie and CSRF signing key. In `oidc` mode required and at least 32 characters, with no built-in fallback. |
| `CAIRN_HOSTNAME` | Public hostname your proxy routes to. |
| `CAIRN_PUBLIC_URL` | Optional in `single` mode; **required and `https`** in `oidc` mode (the OIDC redirect URI is built from it). The panel's full public base URL (`https://cairn.example.com`) — the address a **human** reaches through the proxy, not the container's bind address. Alert emails use it to link straight to the affected collection's review page. Unset = alerts carry no link. |
| `CAIRN_DATABASE_URL` | SQLite path on the writable volume (default `sqlite+aiosqlite:////app/data/cairn.db`). |
| `CAIRN_PROOF_STORE_PATH` | `.ots` proof store on the writable volume. |
| `CAIRN_VERIFY_BACKEND` | `explorer` (works out of the box) or `node` (your own Bitcoin node, fully trustless). |
| `CAIRN_SCHEDULER_ENABLED` | `1` for the in-process scan loop; `0` for cron-only (see below). |
| `CAIRN_AUTO_MIGRATE` | `1` runs migrations on startup; `0` to manage them with `make migrate`. |
| `CAIRN_OIDC_*` | Single sign-on settings, read in `oidc` mode only — see [OIDC settings](#oidc-settings). |

SMTP alert settings can be set via `CAIRN_SMTP_*` env vars **or** in the panel (Settings →
Notifications); panel values are stored in the DB and override the env defaults. The same is true
of `CAIRN_PUBLIC_URL`, which appears there as **Panel address** (admin-only) and also drives the
health-monitoring URL that page shows.

A malformed `CAIRN_PUBLIC_URL` is deliberately **not** fatal: it is logged, treated as unset, and
startup, scanning, and alerting carry on without the link. A cosmetic setting must never cost you a
scan. (In `oidc` mode the opposite holds: `CAIRN_PUBLIC_URL` is the redirect URI's base, so a
missing or invalid value stops startup — see [OIDC settings](#oidc-settings).) Saving one in the panel *is* refused with an inline error — there a human is present to fix
it. Note the review page is owner-scoped, so in `multi` mode the link is actionable by the
collection's owner; other recipients reach a "not found" page.

## Authentication

### The three modes

| `CAIRN_AUTH_MODE` | Who can use the panel | What you must provide |
|---|---|---|
| `single` (default) | Anyone who reaches it. One implicit user owns every collection. | An authenticating reverse proxy in front of everything except `/healthz`. |
| `oidc` | Only the operator, after signing in with an OpenID Connect provider. Same single implicit owner as `single`; the login wall sits in front of it. | A confidential client at your provider, the `CAIRN_OIDC_*` settings, an `https` `CAIRN_PUBLIC_URL`, a real `CAIRN_SECRET_KEY`, and one `cairn users link`. |
| `multi` | Placeholder for Phase 2 (admin-managed users, per-user mounts). Not functional. | — |

In `oidc` mode every request needs a session minted from a verified ID token, except an explicit
public allowlist: `/healthz`, `/static/*`, `/login`, `/auth/oidc/start`, `/auth/oidc/callback`,
`/auth/refused`, `/auth/logout` and `/mode/toggle` (the theme switch). Anything else — including
any route added later — redirects a browser to `/login`, returns `401` with `HX-Redirect` to an
htmx request, and a JSON `401` under `/api`. The login page has one **Sign in with SSO** button;
there are no local passwords.

Admission is by **pinned identity**, never by email or username: the provider's issuer plus the
user's subject (`sub`) must be linked to the Cairn user with `cairn users link`. Nobody is created
on first login. An identity that is not linked (or is missing a required group) lands on a page
saying "This identity is not authorized for this Cairn instance", which shows the person their own
issuer and subject so they can send them to the operator. It never reveals a local username.

Sessions last at most **12 hours** from sign-in (`CAIRN_OIDC_SESSION_MAX_AGE`, default `43200`
seconds), with no sliding renewal, and are re-checked on every request against the user row (still
active, still linked, session not revoked). Sign out (the control next to your username) ends the
Cairn session only; your session at the provider is left alone.

Accept, Acknowledge and Acknowledge-all are recorded against the signed-in user, and the server log
names the acting username and `auth=oidc`.

### OIDC settings

Set these in `.env` (or the YAML overlay; secrets in the environment only). They are read **only**
when `CAIRN_AUTH_MODE=oidc`, by the server and by the CLI alike.

| Variable | Required | Purpose |
|---|---|---|
| `CAIRN_OIDC_ISSUER` | yes | The provider's issuer URL, exactly as its discovery document states it, e.g. `https://idp.example.com/realms/example`. `https` only; no userinfo, query or fragment. Discovery is fetched from `{issuer}/.well-known/openid-configuration`. |
| `CAIRN_OIDC_CLIENT_ID` | yes | The client ID registered at the provider, e.g. `cairn`. |
| `CAIRN_OIDC_CLIENT_SECRET` | yes | The client secret. **Secret — set via the environment, never commit it.** |
| `CAIRN_OIDC_TOKEN_AUTH_METHOD` | no | `client_secret_basic` (default) or `client_secret_post`. Must be one the provider advertises, if it advertises any. |
| `CAIRN_OIDC_EXTRA_SCOPES` | no | Scopes requested in addition to `openid` (space-separated), e.g. `groups` for a provider that only issues the groups claim on request. `offline_access` is refused: Cairn never uses refresh tokens. |
| `CAIRN_OIDC_REQUIRED_GROUP` | no | If set, the ID token's groups claim must be an array containing this exact, case-sensitive value. Unset = no group check. |
| `CAIRN_OIDC_GROUPS_CLAIM` | no | Name of the groups claim in the ID token (default `groups`). |
| `CAIRN_OIDC_SESSION_MAX_AGE` | no | Absolute session lifetime in seconds (default `43200` = 12 h). Must be a positive integer. |

Two existing settings become mandatory in `oidc` mode:

- **`CAIRN_PUBLIC_URL`** must be set in the environment or the YAML overlay and must be an
  **`https`** URL. The redirect URI is `{CAIRN_PUBLIC_URL}/auth/oidc/callback`, built from this
  value only — never from the `Host` or `X-Forwarded-*` headers, and never from the **Panel
  address** stored in the panel (that override still drives alert links, but cannot change the
  redirect URI registered at your provider). An `http` value remains valid in `single` mode.
- **`CAIRN_SECRET_KEY`** must be set and at least 32 characters. It signs the session cookie and
  the CSRF tokens; there is no built-in fallback in `oidc` mode.

**Incomplete configuration refuses to start.** In `oidc` mode `cairn serve` exits with an error
naming the offending setting (never echoing its value or any secret) when the issuer, client ID or
client secret is missing, the issuer is not a clean `https` URL, `CAIRN_PUBLIC_URL` is unset,
invalid or not `https`, `CAIRN_SECRET_KEY` is missing or short, `offline_access` is requested, the
token auth method is unknown, or the session lifetime is not positive. This is deliberate: a login
wall that silently cannot let anyone in should fail at deploy time. Note what it means: an `oidc`
deployment with a broken configuration **does not scan** until it is fixed. The provider is **not**
contacted at startup — an unreachable provider never stops Cairn starting or scanning; it only makes
sign-in fail until it is back.

### Provider setup

Cairn works with any standards-compliant OpenID Connect provider that signs ID tokens with an
asymmetric algorithm. There is no provider-specific code path.

#### Any OIDC provider

Register a client with:

- **Client type:** confidential (it has a client secret).
- **Flow:** authorization code only. Cairn uses PKCE (`S256`), `state` and `nonce`; it does not
  need implicit, hybrid, device or client-credentials grants.
- **Redirect URI:** exactly `https://cairn.example.com/auth/oidc/callback` (your
  `CAIRN_PUBLIC_URL` plus `/auth/oidc/callback`). No wildcards.
- **ID-token signing algorithm:** `RS256`, `PS256` or `ES256`. Cairn refuses `none` and every
  `HS*` algorithm regardless of what the provider advertises.
- **Token endpoint authentication:** client secret via HTTP basic (default) or in the POST body
  (set `CAIRN_OIDC_TOKEN_AUTH_METHOD=client_secret_post`).
- **Groups (optional):** if you want `CAIRN_OIDC_REQUIRED_GROUP`, make the provider put a groups
  claim (an array of strings) **in the ID token**. Cairn never calls the userinfo endpoint.

The ID token's audience must be the client ID; a token with several audiences is accepted only when
its `azp` claim names this client. If the provider advertises RFC 9207
(`authorization_response_iss_parameter_supported`), the `iss` callback parameter is required and
checked.

#### Example: Keycloak

In the realm (here `example`, so the issuer is `https://idp.example.com/realms/example`):

1. **Clients → Create client**, type *OpenID Connect*, client ID `cairn`.
2. **Capability config:** *Client authentication* **on**; authentication flow *Standard flow*
   only (untick *Direct access grants*, *Implicit flow*, *Service accounts*, *OAuth 2.0 Device
   Authorization Grant*).
3. **Login settings:** *Valid redirect URIs* = `https://cairn.example.com/auth/oidc/callback`;
   *Web origins* can stay empty.
4. **Credentials** tab: copy the *Client secret* into `CAIRN_OIDC_CLIENT_SECRET`.
5. *(Optional, for a required group)* **Client scopes → `cairn-dedicated` → Add mapper → By
   configuration → Group Membership**: token claim name `groups`, *Add to ID token* **on**.
   Choose *Full group path* as you prefer — on gives values such as `/cairn-operators`, off gives
   `cairn-operators`; set `CAIRN_OIDC_REQUIRED_GROUP` to match exactly.
6. The subject Cairn pins is the user's Keycloak **ID** (Users → the user → *ID*, a UUID). You can
   also just try to sign in: the refusal page shows your issuer and subject.

The defaults Keycloak advertises (`RS256`, `client_secret_basic`, RFC 9207 `iss`) match Cairn's
defaults; no other settings are needed.

### Linking the operator: `cairn users`

The CLI is the only management surface and works without a server or a login, so it is also the
break-glass path. Run it where Cairn's database is, e.g. `docker compose exec cairn cairn users …`.

| Command | What it does |
|---|---|
| `cairn users list` | Username, admin, active, linked issuer, subject and linked-at for each user. |
| `cairn users link --subject SUB [--issuer URL] [--user NAME] [--replace]` | Pins `(issuer, SUB)` to the user. In `oidc` mode the issuer defaults to `CAIRN_OIDC_ISSUER`; outside `oidc` mode `--issuer` is required. The user defaults to the only user (`--user` is required if there are several). Relinking an already-linked user needs `--replace`. A pair already linked to another user is refused. |
| `cairn users unlink [--user NAME]` | Removes the link. Every session of that user is refused on its next request. |
| `cairn users revoke-sessions [--user NAME]` | Ends every session of that user on its next request, keeping the link. |

`link`, `unlink` and `revoke-sessions` take effect immediately on the running server; no restart.
None of them ends the user's session at the provider, so a later "Sign in with SSO" may complete
without a password prompt — the link, not the provider session, decides admission.

### Rollout: alongside your existing gate, then remove it

1. **Deploy with `CAIRN_AUTH_MODE=oidc` while your current front-door auth stays in place.** Both
   gates apply. `/healthz` stays public on its own route, unchanged. The callback passes through
   the front-door gate because you already hold its cookie.
2. **Link yourself:** `docker compose exec cairn cairn users link --subject <your-subject>`.
3. **Verify:** sign in with SSO; Accept or Acknowledge something and check the server log names
   your username with `auth=oidc`; sign in with a different, unlinked account and confirm the
   "not authorized" page; check `curl https://cairn.example.com/healthz` reports `"mode": "oidc"`.
4. **Remove the front-door auth middleware from the panel route** — and only that one; keep the
   public `/healthz` route. Confirm an anonymous browser now lands on Cairn's login page and
   `/healthz` still answers without credentials.

**Rollback:** set `CAIRN_AUTH_MODE=single`, restart, and restore the front-door gate *before*
exposing the panel again. The migration that adds the identity columns is additive and ignored
outside `oidc` mode; there is nothing to downgrade.

**Break-glass (provider down):** the panel cannot be signed into, but scanning, alerts and
`/healthz` keep working. Every integrity operation is available from the CLI in the container
(`cairn scan`, `accept`, `verify`, `export`, `stamp`, `upgrade`). If you need the panel itself,
roll back as above.

### Keep the callback out of access logs

The provider sends the browser to `/auth/oidc/callback?code=…&state=…&iss=…`. Cairn's own access
log records that path without its query string in `oidc` mode, and the callback always redirects
straight to a clean URL with `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. **Your
reverse proxy's access log is yours to configure.** The authorization code is single-use and bound to
PKCE, but it still does not belong in a log: drop the query string for that path. `Caddyfile.example`
shows a Caddy log filter; with Traefik, drop the request path from the access log
(`accessLog.fields.names.RequestPath: drop`) or keep the access log off for that entrypoint.

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
monitor can reach it without credentials. In `oidc` mode `/healthz` is on Cairn's public allowlist
too, and its body reports `"mode": "oidc"`.
