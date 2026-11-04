# Design: oidc-sso-login

## Context

Cairn is one FastAPI process (one uvicorn worker, `cairn serve`) with a SQLite index, read-only
watched mounts and a writable `.ots` proof store. The panel resolves its owning user through
`current_user`, which today returns the first `users` row (the implicit single user). Every
query and collection lookup is already scoped by that user, so a login wall changes *who the
user is*, not how scoping works.

`src/config.py` knows two modes. `single` is built. `multi` is a placeholder: it requires
`CAIRN_SECRET_KEY`, skips the implicit-user bootstrap, and then has nothing to log in with.

## Goals

1. A panel that refuses every request without a session minted from a verified ID token.
2. Integrity-critical actions bound to that verified identity.
3. `single` mode byte-for-byte unchanged; the CLI unaffected.
4. Works with any standards-compliant OIDC provider that signs ID tokens asymmetrically, with no
   provider-specific code path (the audience profile in D3 accepts multi-audience tokens only when
   `azp` names this client).

## Decisions

### D1. A third mode, `oidc`, not `multi` + OIDC

`CAIRN_AUTH_MODE` gains `oidc`. In `oidc` mode the implicit-user bootstrap runs exactly as in
`single` (one owner, every collection unchanged), and the login wall sits in front of it.

Rejected: *`multi` with an OIDC switch.* `multi` means admin-created users with per-user mounted
bases and (per DESIGN.md) password login. None of that is built, and none of it is needed to put
one operator behind SSO. Hanging OIDC on `multi` would either ship a half-built `multi` (no
bootstrap → no owner → the panel cannot render) or drag user administration and mount jailing
into this change. A separate value keeps the contract honest: `oidc` = one owner, federated login.
When `multi` is built, OIDC becomes one of its login methods and `oidc` can be folded in or kept
as the single-owner shorthand; the `users` columns added here serve both.

`/healthz` reports `"mode": "oidc"` in this mode; nothing else in its body changes.

### D2. App-native OIDC, not a front-door forward-auth swap

The alternative was to keep the panel open and swap one forward-auth gate for another (an
OIDC-speaking auth proxy in front of the panel). It was rejected because:

- **The app verifies tokens itself.** Signature, issuer, audience, nonce and expiry are checked
  in the process that acts on the request. A forward-auth gate leaves the app trusting whatever
  reaches its port; a request that bypasses the proxy bypasses the gate.
- **Accept and Acknowledge-all are bound to a verified identity.** These re-baseline or silence
  the very alarms Cairn exists to raise. With app-native login, `acknowledged_by` and the action
  log name a user whose session came from a verified token, re-checked against the `users` row
  on every request. With forward-auth, the best available attribution is a proxy-supplied header
  the app cannot verify, and today it is not even that (the implicit user).
- **Revocation is local.** `cairn users unlink` or `revoke-sessions` ends access on the next
  request, without touching the proxy or the provider.

What it does **not** change, stated plainly:

- **A party that controls TLS termination can still hijack sessions.** The reverse proxy sees the
  session cookie and every request; whoever controls it can replay or forge panel traffic. That
  is true of any web app behind a proxy and is not something an app-level login can fix.
- **The evidence is unaffected either way.** Watched folders are mounted read-only and the
  durable guarantee is the file bytes plus their OpenTimestamps proofs. Someone who gets into the
  panel, by any route, can re-baseline and silence alerts, but cannot modify a watched file,
  forge a proof, or move a Bitcoin attestation. Login protects the alarm, not the record.

### D3. Protocol profile (carried over from the reference implementation)

- Authorization code flow, confidential client, PKCE `S256`, `state` (32 bytes), `nonce`
  (32 bytes), `response_mode` default (query).
- Discovery from `{issuer}/.well-known/openid-configuration`, lazily on the first login and
  cached for 1 h; the document's `issuer` must equal the configured issuer exactly.
  `authorization_endpoint`, `token_endpoint`, `jwks_uri` must be `https`, without userinfo or
  fragment. Same-origin with the issuer is **not** required (some providers serve JWKS from a
  different host); the reference's Keycloak-only same-origin rule is dropped for provider
  neutrality.
- **RFC 9207:** if discovery advertises `authorization_response_iss_parameter_supported: true`,
  the callback's `iss` parameter is required; whenever it is present it must equal the issuer.
- **Signature algorithms:** allowlist {RS256, PS256, ES256} intersected with
  `id_token_signing_alg_values_supported` (when advertised). `none` and every `HS*` are refused
  regardless of what the provider advertises. An empty intersection refuses login with a
  configuration reason.
- **JWKS:** fetched with discovery; on an unknown `kid` refetched at most once per 60 s.
- **ID token claims:** `iss` equals the issuer; `aud` is either the client ID as a string, or a
  non-empty array of strings that contains the client ID, where an array with more than one
  element is accepted **only** when `azp` is present and equals the client ID (OIDC Core 3.1.3.7:
  the authorized party identifies the client when several audiences are present); `azp`, whenever
  present, equals the client ID; `exp` and `iat` integers, `exp` in
  the future and `iat` not in the future, with a fixed 30 s leeway (the reference used zero; a
  small leeway avoids refusing logins on ordinary clock drift); `nonce` equals the transaction's;
  `sub` a non-empty string of at most 255 characters.
- **Token endpoint authentication:** `client_secret_basic` (default) or `client_secret_post`
  (`CAIRN_OIDC_TOKEN_AUTH_METHOD`). If discovery lists
  `token_endpoint_auth_methods_supported`, the configured method must be in it.
- **Scopes:** `openid` by default, plus `CAIRN_OIDC_EXTRA_SCOPES` (for providers that need, e.g.,
  a `groups` scope). `offline_access` is refused at config load. The userinfo endpoint is never
  called; only the ID token is used.
- **Bounded transport:** no redirects, `trust_env=False` (no environment proxies), 10 s timeout,
  response bodies capped at 128 KiB before parsing.
- **Libraries:** Authlib + joserfc, the same pair as the reference, pinned exactly. Both enter
  the image through the existing Trivy HIGH/CRITICAL gate (`make trivy`, the CI image job); a
  finding there blocks the slice, it is not waived in this change.

### D4. Transactions and rate limits

The login transaction (`state` → `nonce`, PKCE verifier, `next` path, expiry) lives in an
in-process dict, keyed by `state`, bound to the initiating browser by also storing `state` in its
session. TTL 5 min, at most 1024 live transactions, expired entries purged before admission, a
full store refuses new starts with `Retry-After` and never evicts a live entry, consumption is
atomic and single-use. This relies on `cairn serve` running one worker, which it does; the
constraint is documented next to the store.

Rate limits: a process-global start budget (60/min) is the real bound. Per-address start and
callback limits (10/min each) are keyed on `request.client.host`, which is only as trustworthy as
the forwarded-header trust in D8 allows; they are a nuisance limit, not a control.

### D5. Identity linking: `(issuer, subject)` on `users`, CLI only

Migration `0013` adds to `users`: `oidc_issuer` (nullable, ≤ 512), `oidc_subject` (nullable,
≤ 255), `oidc_linked_at` (nullable), `session_epoch` (integer, not null, default 0). A UNIQUE
**index** `uq_users_oidc_identity` on (`oidc_issuer`, `oidc_subject`); named CHECK constraints
for the two lengths (`ck_users_oidc_issuer_len`, `ck_users_oidc_subject_len`, both written as
`col IS NULL OR length(col) <= N`, because SQLite ignores `VARCHAR(N)`) and for all-or-none
(`ck_users_oidc_all_or_none`). Reversible. No new table, no foreign key.

**The migration never rebuilds `users`, in either direction.** This is the decision that matters
most in this change. Alembic's batch mode on SQLite copies a table into a new one, drops the old
one and renames; `alembic/env.py` turns `PRAGMA foreign_keys=ON` on for every migration
connection, and `collections.user_id` is `ON DELETE CASCADE` (and `files`, `runs`, `events`
cascade from `collections`). Dropping the old `users` table under enforced foreign keys can
delete every collection and its whole integrity history. So:

- **Upgrade** issues four plain `ALTER TABLE users ADD COLUMN …` statements (via `op.execute`,
  never `batch_alter_table`): each OIDC column carries its CHECK in its own column definition
  (`CONSTRAINT <name> CHECK (…)`), the all-or-none CHECK rides on `oidc_linked_at` (the last OIDC
  column added, so every column it names already exists), `session_epoch` is
  `INTEGER NOT NULL DEFAULT 0`; then `CREATE UNIQUE INDEX`. SQLite permits a column-level CHECK
  that names other columns and a NOT NULL column with a constant default in `ADD COLUMN`; no row
  is copied.
- **Downgrade** drops the index first, then `oidc_linked_at` (whose definition carries the
  cross-column CHECK), then `oidc_subject`, `oidc_issuer`, `session_epoch`, each with a plain
  `ALTER TABLE users DROP COLUMN`. SQLite allows dropping a column whose own definition carries a
  CHECK; it refuses a column that is still indexed or named by another column's CHECK, which is
  why the order is fixed. Both directions were exercised against the stdlib SQLite (3.45) while
  writing this design, including a cascade-protected child row that survived.
- `ALTER TABLE … DROP COLUMN` needs SQLite ≥ 3.35. The migration checks
  `sqlite_version()` first and **refuses** (raises with the version in the message) below that,
  rather than falling back to a rebuild. The image's interpreter ships a far newer SQLite.
- The `User` model declares the same three CHECKs and the unique index, so a schema made by
  `create_all` in tests enforces exactly what a migrated one does.

Rejected: *a batch rebuild with `PRAGMA foreign_keys=OFF` outside the transaction, then
`foreign_key_check`.* It works when done exactly right, but it depends on a pragma that is a
silent no-op inside a transaction and on Alembic's connection lifecycle in `env.py`; a mistake
loses data instead of failing. Rejected: *dropping the CHECKs and validating only in the
service.* It would avoid nothing (the CHECKs do not force a rebuild) and would let a direct
write break the invariants.

Required regardless of the mechanism: a test on a populated fixture database at `0012` (two
users, collections owned by each, files, runs, events including acknowledged ones) that records
the row count and the full ID set of `users`, `collections`, `files`, `runs` and `events` plus
every `collections.user_id` and `events.acknowledged_by`, upgrades to `0013`, asserts all of it
unchanged and `PRAGMA foreign_key_check` empty, downgrades to `0012` and asserts the same again.
A SQL trace (`sqlite3` trace callback) asserts that neither direction issues `CREATE TABLE`,
`DROP TABLE` or `ALTER TABLE … RENAME` for `users`.

The identity service validates the same bounds before writing (issuer: `https`, no userinfo,
query or fragment, ≤ 512 characters; subject: non-empty, ≤ 255 characters, no control
characters), so the CLI fails with a one-line message instead of a constraint error; the CHECKs
are the backstop for any other writer. Boundary tests cover 512/513 and 255/256 at both the
schema and the CLI layer.

The pair is the only thing ever matched. Email, username, `preferred_username` and every other
claim are never used for admission (they are mutable at most providers).

CLI (`cairn users …`), the only management surface in v1 and the break-glass path:

- `list` — username, admin, active, linked issuer, subject, linked-at.
- `link --subject SUB [--issuer URL] [--user NAME] [--replace]` — issuer defaults to the
  configured `CAIRN_OIDC_ISSUER` and is validated as above whether given or defaulted; user defaults to the implicit owner and is required when more
  than one user exists; relinking an already-linked user needs `--replace`; bumps
  `session_epoch`. Refuses a subject that is empty, longer than 255 characters, or contains
  control characters, and a pair already bound to another user.
- `unlink [--user NAME]` — clears the triple and bumps `session_epoch`.
- `revoke-sessions [--user NAME]` — bumps `session_epoch` only.

How the operator learns their subject: the refusal page shows the signed-in person their own
issuer and subject (D7), or the provider's admin console does (for Keycloak, the user's ID).

v1 binds the operator to the existing implicit user, so every collection keeps its owner.
Additional linked users are future work and would arrive with `multi`. Alert recipients stay
plain email addresses.

### D6. Sessions and the login wall

- **Default deny.** A middleware (not a per-route dependency) refuses every request in `oidc`
  mode unless the path is on the public allowlist: `/healthz`, `/static/*`, `/login`,
  `/auth/oidc/start`, `/auth/oidc/callback`, `/auth/refused`, `/auth/logout`, `/mode/toggle`.
  A test enumerates
  `app.routes` and asserts that every other route refuses without a session, so a new route is
  protected by default.
- **Refusal shape:** a browser GET gets `303 → /login?next=<safe local path>`; an htmx request
  gets `401` with `HX-Redirect: /login`; `/api*` paths get a JSON `401`. `next` is accepted only
  as a local absolute path (starts with a single `/`, no scheme, no `//`, no backslash).
- **Session content:** `uid`, `epoch`, `auth="oidc"`, `login_at`, and the CSRF nonce. No subject,
  no token, no claims (the only exception is the one-shot refusal value of an anonymous session,
  D11). On every request the wall loads the user and requires: user exists, is
  active, is still linked, `session_epoch` equals the session's `epoch`, and `now - login_at` is
  under `CAIRN_OIDC_SESSION_MAX_AGE` (default 43200 s). Any failure clears the session and
  refuses. `current_user` returns the session's user in `oidc` mode and keeps today's behaviour
  in `single`.
- **Login mints a fresh session:** the session is cleared before the new values are written, so
  a pre-login session (and its CSRF nonce) cannot carry over.
- **Cookie:** in `oidc` mode `SessionMiddleware` runs with `https_only=True`, `same_site="lax"`
  (the callback is a top-level GET, which Lax allows) and `max_age` = the OIDC session lifetime.
  In `single` mode its arguments are exactly today's.
- **Logout:** `POST /auth/logout` (CSRF-checked) clears the local session. The provider session
  is left alone (no RP-initiated logout in v1).
- **Login start is a POST** from the login page with a CSRF token, so a third-party page cannot
  start a flow in the victim's browser.
- **`/mode/toggle` is public, so it must not be an open redirect.** Today it redirects to the raw
  `Referer`, which makes `GET /mode/toggle` with `Referer: https://evil.example/` a redirect off
  site. In `oidc` mode the handler takes only the path and query of the `Referer`, applies the
  same local-path rule as `next`, and falls back to `/`; a cross-origin `Referer` therefore lands
  on a local path, never on another origin. In `single` mode the handler is unchanged (slice 0
  golden); fixing it there too is a separate decision, recorded under Future decisions.

### D7. Refusals reveal nothing about local users

Every refusal after a valid token (no linked user, inactive user, group missing) ends on the
same page and status (403, `GET /auth/refused`, see D11 for why it is a redirect): "This identity
is not authorized for this Cairn instance." The page
also shows the signed-in person their own issuer and subject, so they can send them to the
operator. It never shows a local username, whether one exists, or which check failed. Protocol
failures (bad state, bad signature, provider error) render a generic "Sign-in failed, try again"
page. Logs carry a closed reason code (`state`, `iss_param`, `token_exchange`, `signature`,
`claims`, `nonce`, `unlinked`, `inactive`, `group`, `capacity`, `rate`, `discovery`, `config`)
and never the query string, code, tokens, state, nonce, verifier, client secret, subject or raw
claims. The success log names the local username only.

### D8. Redirect URI and the forwarded-header dependency

The redirect URI is `{CAIRN_PUBLIC_URL}/auth/oidc/callback`, where `CAIRN_PUBLIC_URL` is the
env/YAML value. It is never derived from the request (`Host`, `X-Forwarded-*`) and never from
the panel-editable `app_settings` override: the latter is a convenience for alert links, and the
redirect URI must match what is registered at the provider.

Both entrypoints, `cairn serve` and the image's `uvicorn` CMD, start uvicorn with
`proxy_headers=True, forwarded_allow_ips="*"`, so any peer that
can reach the container port can set `X-Forwarded-For`/`-Proto`. This change does not alter
that, and does not depend on it for anything security-relevant except the per-address rate-limit
key (D4). SECURITY.md and DEPLOYMENT.md will state that the container port must be reachable only
by the reverse proxy.

### D9. Configuration and fail-loud validation

New settings (env `CAIRN_…`, YAML overlay as usual): `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET` (secret: env or referenced file only), `OIDC_TOKEN_AUTH_METHOD`,
`OIDC_EXTRA_SCOPES`, `OIDC_REQUIRED_GROUP`, `OIDC_GROUPS_CLAIM` (default `groups`),
`OIDC_SESSION_MAX_AGE` (default 43200).

In `oidc` mode startup fails with a message naming the setting when: issuer, client ID or client
secret is missing; the issuer is not `https` or carries userinfo, query or fragment;
`CAIRN_PUBLIC_URL` (env/YAML) is unset, invalid or not `https` (an `http` URL is valid for
alert links in every mode, but in `oidc` mode the transaction cookie is `Secure` and the
callback would carry the code in cleartext, so the rule is `oidc`-only and the existing
outbound-link grammar is unchanged); `CAIRN_SECRET_KEY` is missing or shorter than
32 characters; `offline_access` is requested; the session lifetime is not positive. This departs
from the `public_url` fail-soft rule on purpose and follows the existing `multi` + secret-key
precedent: the operator chose a mode whose whole point is the wall, and a wall that silently
cannot let anyone in is found at deploy time this way rather than at the next alarm. Provider
reachability is **not** checked at startup: an IdP outage must never stop scanning; it fails
logins closed instead.

`CAIRN_SECRET_KEY` has no dev fallback in `oidc` mode, for both the session middleware and the
CSRF serializer. Outside `oidc` mode the OIDC settings are ignored entirely.

### D10. Required group (optional)

When `CAIRN_OIDC_REQUIRED_GROUP` is set, the ID token's groups claim must be an array of at most
256 strings of at most 512 characters each, and contain the configured value by exact,
case-sensitive equality. A missing, scalar or malformed claim refuses (D7). Unset = no group
check.

### D11. Callback secrets never reach a log or a lingering URL

The authorization response arrives as `GET /auth/oidc/callback?code=…&state=…&iss=…`. Route-level
redaction (D7) cannot stop uvicorn's own access logger, which writes the request line, query
included, before and independently of the route. The image runs `uvicorn` directly and
`cairn serve` calls `uvicorn.run`, so the fix has to live in the application, not in one launcher:

- In `oidc` mode, application startup (`src/main.py`, importing `src/auth/access_log.py`)
  attaches a logging filter to the `uvicorn.access` logger that rewrites the request target of
  any `/auth/oidc/callback` record to the bare path. A record whose arguments are not the shape
  the filter understands is **dropped**, not passed through. The filter is not installed in
  `single` mode.
- The callback never renders a page at its own URL. Success is `303` to the stored `next`;
  a protocol failure is `303 /login?error=failed`; a D7 refusal is `303 /auth/refused`. The
  refusal page needs the person's issuer and subject: the callback puts them in the session as a
  one-shot `refused` value (anonymous session only, 5-minute expiry, removed when read and by any
  login), which is the only time a subject is written to a cookie; an authenticated session still
  never holds one (D6).
- Every callback response and the refusal page carry `Referrer-Policy: no-referrer` and
  `Cache-Control: no-store`.
- Test with a real server, not the in-process client: start uvicorn in a subprocess through
  each entrypoint (`cairn serve`, and the image's CMD argv), call the callback with recognisable
  `code`, `state` and `iss` values (success, failure and refusal paths), and assert that neither
  those values nor the raw query string appear anywhere in the captured stdout/stderr.
- Proxy access logs are the operator's: DEPLOYMENT.md, SECURITY.md and `Caddyfile.example` show
  how to drop the callback query there.

### D12. Public-repository hygiene

The repository is public. Everything this change adds uses placeholders only
(`https://idp.example.com/realms/example`, `https://cairn.example.com`, client ID `cairn`), and
the release gate scans the **whole tracked tree**, not the diff: current docs, archived
OpenSpec changes, tests, code comments and the design mock data. Categories: personal names (other
than the copyright and author lines), real host paths, real collection sizes and file names,
monitor or ticket identifiers from a private deployment, private hostnames and repositories,
private addresses, and non-`example` email addresses. The list of personal names and hostnames to
search for is supplied by the operator at gate time and is **never committed** (a committed deny
list would itself publish the data); only the generic category patterns may live in the repo.
Existing hits are redacted in a dedicated commit within this change (task D.9), separate from the
feature commits so it can be reviewed on its own.

## Threat model (summary)

| Threat | Control |
|---|---|
| Request reaches the container without the proxy | Login wall in the app (D6) |
| Forged or replayed callback | single-use state bound to the browser, PKCE, nonce, RFC 9207 `iss` (D3, D4) |
| Token from another client / mix-up | `aud`/`azp` checks, exact issuer, `iss` parameter (D3) |
| Algorithm confusion | fixed asymmetric allowlist, never `none`/`HS*` (D3) |
| Hostile or huge provider response | bounded transport, no redirects, no env proxies (D3) |
| Valid IdP user who is not the operator | no JIT; `(iss, sub)` link required; optional group (D5, D10) |
| Stolen session after access is removed | per-request re-validation, `session_epoch`, absolute lifetime (D6) |
| Open redirect via `next` | local-path-only rule (D6) |
| Open redirect via `/mode/toggle` `Referer` | local-path-only rule in `oidc` mode (D6) |
| Authorization code or state in access logs / history | access-log filter, clean redirects, no-referrer (D11) |
| Cascade deletion during the migration | in-place `ADD`/`DROP COLUMN`, no rebuild, preservation test (D5) |
| Login CSRF / session fixation | POST start with CSRF, fresh session on login (D6) |
| Credential leakage in logs | closed reason codes, redaction (D7) |
| Host-header poisoning of the redirect URI | redirect from configuration only (D8) |
| Party controlling TLS termination | **not mitigated** — accepted (D2) |

## Rollout

1. Deploy with `CAIRN_AUTH_MODE=oidc` **while the existing front-door auth gate stays in place**.
   Both gates apply; `/healthz` stays public on its own route. The callback path passes through
   the front-door gate because the operator already holds its cookie.
2. In the pod: `cairn users link --subject <sub>`. Sign in with SSO; confirm Accept/Ack-all are
   attributed and that an unlinked account is refused.
3. Remove the front-door auth middleware from the panel route (keep the public `/healthz` route).
   Confirm an anonymous request gets the login page and `/healthz` still answers.

**Rollback:** set `CAIRN_AUTH_MODE=single` and restore the front-door gate. The migration is
additive and its columns are ignored outside `oidc`; downgrading it is not required.

**Break-glass** (provider down): every integrity operation (`scan`, `accept`, `verify`, `export`,
`stamp`, `upgrade`) works from the CLI in the pod; or roll back as above.

## Accepted limitations

- A party controlling TLS termination can hijack sessions (D2).
- Per-address rate limits are keyed on a forwarded address any peer that reaches the port can
  set; the global budget is the real bound (D4, D8).
- The transaction store is in-process: a restart drops in-flight logins (the user clicks again),
  and more than one worker is unsupported (D4).
- Unlink and revoke-sessions do not end the provider's own session; the next "Sign in with SSO"
  may be silent if the identity is relinked.
- No step-up re-authentication before Accept; a session up to 12 h old can re-baseline.
- One identity per user and, in v1, one user.
- `oidc` mode with invalid static configuration does not start, and therefore does not scan
  (D9). Chosen deliberately; an unreachable provider never has this effect.

## Future decisions (not in this change)

- Whether to apply the `/mode/toggle` local-path rule in `single` mode as well (it is an open
  redirect there today; left untouched here because `single` must stay byte-identical).
- Whether `/verify` (proof verification for a single file) should be reachable without login.
- Additional users and how they relate to `multi` and per-user mounts.
- Step-up re-authentication (`max_age`/`prompt=login`) for Accept.
- RP-initiated logout.

## Review history

- **Codex spec review r1: FAIL** — 1 BLOCKER, 6 MAJOR, 2 MINOR, all folded:
  migration without a `users` rebuild plus a populated-fixture preservation test (D5);
  `/mode/toggle` local-path rule in `oidc` mode (D6); `https`-only `CAIRN_PUBLIC_URL` in `oidc`
  mode (D9); uvicorn access-log filter, clean callback redirects, no-referrer and a real-server
  log test (D11); slice order B after A and C, `_base_context` wiring assigned to B (tasks);
  CLI help golden limited to pre-existing commands with the `users` entry asserted additively
  (tasks 0.2, A.4); whole-tree public-repo scan and a redaction commit (D12, tasks D.8–D.9);
  length CHECKs and issuer validation with boundary tests (D5); multi-audience tokens accepted
  only with `azp` equal to the client (D3).
