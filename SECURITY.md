# Security Policy

Cairn is a file-integrity and notarization tool, so its own integrity matters. If you
find a vulnerability, please report it privately rather than opening a public issue.

## Reporting a vulnerability

- **Preferred:** open a [GitHub private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
  on this repository ("Security" → "Report a vulnerability").
- Please include a description, affected version/commit, and reproduction steps.

This is a hobby project maintained on a best-effort basis — there is no SLA, but
security reports are taken seriously and triaged when received.

## Scope and design notes

A few invariants worth knowing when assessing impact:

- **Watched folders are mounted read-only** (`:ro`). Cairn cannot modify or delete the
  bytes it monitors; the durable guarantee is the file bytes plus their `.ots` proofs,
  not the SQLite index (which is rebuildable).
- **Single-user mode has no in-app login wall.** Do not expose a single-user deployment
  directly to the internet — front it with a reverse proxy that enforces authentication
  (the example compose uses Traefik with a forward-auth middleware). Keep `/healthz` as the
  only unauthenticated route if you need an external monitor to poll it. Multi-user
  (password) login is Phase 2 and not yet shipped.
- **OIDC mode (`CAIRN_AUTH_MODE=oidc`) puts a login wall in the app itself.** Every route
  except a short public allowlist (`/healthz`, static assets, the login/callback/refusal/
  logout routes and the theme toggle) refuses a request without a session minted from an ID
  token Cairn verified itself: authorization code flow with PKCE, single-use `state` bound to
  the browser, `nonce`, an asymmetric-only algorithm allowlist (never `none` or `HS*`), exact
  issuer, `aud`/`azp` checks and the RFC 9207 `iss` parameter. Only an identity pinned by
  issuer + subject with `cairn users link` is admitted (no provisioning on first login, never
  matched by email). Sessions carry no tokens or claims, last at most 12 hours, and are
  re-validated on every request, so `cairn users unlink` / `revoke-sessions` end access on the
  next request. Accept and Acknowledge are attributed to the verified user. Setup and
  rollout: [`DEPLOYMENT.md`](./DEPLOYMENT.md#authentication).
- **The container port must be reachable only by the reverse proxy.** `cairn serve` and the
  image trust `X-Forwarded-*` headers from any peer. In `oidc` mode the redirect URI comes
  from configuration only and forwarded headers feed only a nuisance per-address rate limit,
  but a peer that reaches the port directly bypasses every proxy-side control and, in `single`
  mode, owns the panel.
- **Whoever controls TLS termination can hijack sessions.** The reverse proxy sees every
  session cookie and request; an app-level login cannot protect against it. This is accepted.
- **Panel compromise does not touch the evidence.** Watched folders are mounted read-only and
  the durable guarantee is the file bytes plus their OpenTimestamps proofs. Someone who gets
  into the panel by any route can re-baseline or silence alerts, but cannot modify a watched
  file, forge a proof or move a Bitcoin attestation. Login protects the alarm, not the record.
- **Keep the OIDC callback out of your proxy's access log.** Cairn strips the query string of
  `/auth/oidc/callback` from its own access log and never renders a page at that URL, but the
  reverse proxy logs requests independently. Drop the callback's query there
  (`Caddyfile.example` shows how).
- **Known limits of `oidc` mode.** Per-address rate limits are keyed on a spoofable forwarded
  address (a process-wide budget is the real bound); in-flight logins are lost on restart and
  only one worker is supported; unlinking does not end the provider's own session; there is
  no step-up re-authentication before Accept within a session; an `oidc` deployment with
  invalid static configuration refuses to start (and therefore does not scan), while an
  unreachable provider only makes sign-in fail.
- **Verification trust model.** The default `explorer` backend trusts a block explorer's
  canonical block at a given height. For full trustlessness, point Cairn at your own
  Bitcoin node (`node` backend).

## Supported versions

Cairn is pre-1.0. Only the latest `main` is supported.
