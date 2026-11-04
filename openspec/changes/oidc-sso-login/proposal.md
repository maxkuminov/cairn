# Proposal: oidc-sso-login

Coordination issue: #61

## Why

Cairn's panel has no login wall. `single` mode (the only mode that is built) trusts every request
that reaches the port, and `multi` mode exists only as a config value: there is no login route,
`src/auth/` is an empty package, and `ensure_implicit_user` skips bootstrap in `multi`, so the
panel cannot serve at all in that mode. DESIGN.md §4 already says so plainly: "no external OAuth
proxy is required" holds only once in-app login exists, and until then a deployment has to put a
forward-auth gate in front of the panel.

That gate is a weaker control than it looks, for a tool whose product is noticing that files
changed:

- **Anyone who reaches the container port directly owns the panel.** The gate lives in the proxy.
  A request that does not pass through the proxy (a neighbour on the same network, a misrouted
  rule, a second router that forgot the middleware) gets the full panel with no identity at all.
- **The integrity-critical actions are not bound to an identity.** *Accept* (re-baselining a
  collection, which silences the alarm for every missing or modified file in scope) and
  *Acknowledge all* are attributed to the implicit user because that is the only user there is.
  The app never sees who pressed the button; at most it sees a proxy header it cannot verify.

Many self-hosters already run an OpenID Connect provider (Keycloak, Authentik, Kanidm, Zitadel,
Dex, a cloud IdP). Letting Cairn verify an ID token itself closes both gaps without adding a
password store, and gives the operator one place to revoke access.

## What Changes

- **New auth mode `CAIRN_AUTH_MODE=oidc`.** Every panel route requires a session. `/healthz`
  stays unauthenticated; `/static/*`, the login page, the OIDC start, callback and refusal
  endpoints, logout and the theme toggle are the only other public routes, by an explicit allowlist (default deny, so a future route is protected
  without anyone remembering to protect it). The CLI keeps working without auth, as it does today.
  `single` mode is unchanged byte for byte; `multi` is left reserved for the future password /
  per-user-mount model (design D1).
- **Federated login only, no password login.** The login page shows one button, **"Sign in with
  SSO"** (the label is defined in one place). Authorization code flow with PKCE S256, `state`,
  `nonce`, RFC 9207 `iss` checking, strict ID-token validation (RS256/PS256/ES256 intersected
  with what the provider advertises, never `none` or `HS*`; `iss`, `aud`, `azp`, `exp`, `iat`),
  a bounded discovery/JWKS transport, a single-use in-process transaction store, rate limits and
  redacted callback logs. Mirrors a reference implementation already reviewed for the same stack.
- **No JIT provisioning.** A verified identity is admitted only if it is linked to an existing
  local user by its pinned `(issuer, subject)` pair. Anyone else is refused with a page that
  reveals nothing about local users (it shows the person their own issuer and subject, so they
  can hand them to the operator).
- **Linking lives in the database (Alembic migration `0013`)**: nullable `oidc_issuer`,
  `oidc_subject`, `oidc_linked_at` plus a `session_epoch` counter on `users`, with a unique pair,
  length CHECKs and an all-or-none CHECK, added in place with no table rebuild (a rebuild of
  `users` under enforced foreign keys could cascade-delete every collection; design D5). It is managed only by a pod-local CLI — `cairn users list`,
  `cairn users link --subject …`, `cairn users unlink`, `cairn users revoke-sessions` — which is
  also the break-glass path. v1 binds the operator to the existing implicit user, so ownership of
  every collection is unchanged. Additional users are future work; alert recipients stay email
  addresses, not users.
- **Sessions.** OIDC sessions have their own absolute lifetime (`CAIRN_OIDC_SESSION_MAX_AGE`,
  default 12 h), a `Secure` cookie, a fresh session on login (no fixation), and are re-validated
  against the `users` row on every request, so `unlink` and `revoke-sessions` take effect on the
  next click. Provider tokens are discarded after the callback; nothing from the provider is
  stored except the pinned pair already linked by the operator.
- **Optional required-group check** (`CAIRN_OIDC_REQUIRED_GROUP`, claim name configurable).
- **Redirect URI from configuration only.** It is built from the env/YAML `CAIRN_PUBLIC_URL`,
  never from forwarded headers and never from the panel-editable override. `CAIRN_SECRET_KEY`
  is mandatory in `oidc` mode (no dev fallback for the session or the CSRF signer).
- **Attribution.** Accept, Acknowledge and Acknowledge-all keep writing `acknowledged_by = user`,
  which in `oidc` mode is now the verified session user, and log the acting username and auth
  method.
- **Docs** (README, DEPLOYMENT.md, SECURITY.md, CLAUDE.md, DESIGN.md §4, `config.example.yaml`,
  `.env.example`, `docker-compose.example.yml`, `Caddyfile.example`): a provider-setup section
  (generic, plus a Keycloak example with placeholders), the rollout procedure, and the network
  isolation the app depends on (`cairn serve` trusts forwarded headers from any peer).
- **New dependencies:** Authlib and joserfc, pinned exactly, through the existing Trivy
  HIGH/CRITICAL image gate.

## Non-goals

- Password login, local registration, user administration in the panel, per-user mounted bases
  (the rest of `multi`).
- More than one human user, or mapping alert recipients to users.
- Provider-side (RP-initiated) logout, refresh tokens, `offline_access`, step-up re-authentication.
- A public `/verify` (it stays behind the login wall for now; a later decision).
- Any change to how files are watched, hashed or stamped.

## Capabilities

- **New:** `oidc-login`.
- **Modified:** `configuration` (auth mode selector gains `oidc`), `datastore` (implicit-user
  bootstrap runs in `oidc` too; `users` carries the optional pinned identity).

## Impact

- Code: `src/config.py`, `src/main.py`, `src/database.py`, `src/csrf.py`, `src/cli.py`,
  `src/models/db.py`, `src/auth/` (new modules), `src/control_panel/routes.py`,
  `src/control_panel/templates/` (`login.html`, `base.html`, new refusal page), `alembic/versions/`.
- Deployments in `single` mode: none. The migration is additive and ignored outside `oidc`.
- Deployments that switch to `oidc`: need a confidential client at their provider, a fixed
  `CAIRN_PUBLIC_URL`, a real `CAIRN_SECRET_KEY`, one `cairn users link` call, and then may remove
  their front-door auth gate (design D9).
