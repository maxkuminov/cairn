# Tasks: oidc-sso-login

Slices are sized for independent subagents. Order: **slice 0 lands first** (goldens recorded
against unchanged code). Then slices A, C and D run in parallel; **slice B starts once both A and
C are merged** (it imports A's model columns and identity service, and renders C's `login.html`
and `auth_refused.html`). No slice renders, imports or tests a file that only a slice running in
parallel with it creates. Every slice ends with `make test` (or
`pytest`) green on its own branch; the authoritative gate is the full suite on the merged result.

File ownership is exclusive: a slice touches only the files listed for it. Shared contracts are
fixed below so no slice has to wait on another's guesswork.

**Fixed contracts**

- Columns (A): `users.oidc_issuer`, `users.oidc_subject`, `users.oidc_linked_at`,
  `users.session_epoch`.
- Identity service (A), `src/services/identity.py`:
  `async def find_linked_user(session, issuer: str, subject: str) -> User | None`,
  `async def link(session, *, subject, issuer, username=None, replace=False) -> User`,
  `async def unlink(session, *, username=None) -> User`,
  `async def revoke_sessions(session, *, username=None) -> User`, raising `IdentityError(msg)`.
- Label (C): `src/auth/ui.py` defines `SSO_BUTTON_LABEL = "Sign in with SSO"`; nothing else
  spells the label.
- Template context (C defines the templates, B supplies the values): `login.html` receives
  `csrf_token`, `mode`, `next`, `error` (one of `None`, `"failed"`, `"busy"`); `auth_refused.html`
  receives `mode`, `issuer`, `subject`; `base.html` receives `auth_mode` and `session_username`
  (None outside `oidc`). **B owns the wiring**: it adds `session_username` to `_base_context` in
  `src/control_panel/routes.py`, so every panel page passes it. C's tests render templates with a
  hand-built context and do not depend on B.
- Routes (B): `GET /login`, `POST /auth/oidc/start`, `GET /auth/oidc/callback`,
  `GET /auth/refused`, `POST /auth/logout`.

## 0. OIDC-off regression goldens (lands first, alone)

- [ ] 0.1 New `tests/test_single_mode_goldens.py` + `tests/goldens/single/`: in `single` mode,
  against a seeded fixture database, record status, relevant headers (`content-type`,
  `set-cookie` attributes, `location`, `hx-*`) and body for `/`, `/collections`,
  `/collection/{id}`, `/collection/{id}/review`, `/review`, `/settings`, `/verify`,
  `/healthz`, `/mode/toggle`, one htmx fragment, and one CSRF-protected POST (ack). Normalize only
  values that legitimately vary (timestamps, CSRF tokens, session cookie value), and assert the
  normalization did not hide anything else.
- [ ] 0.2 Record `SessionMiddleware` arguments in `single` mode (secret source, `same_site`,
  `https_only`, `max_age`, cookie name) and the help text of every **pre-existing** command
  (`cairn <cmd> --help` for each) plus the top-level `cairn --help`. The top-level comparison
  removes exactly one line, the `users` subcommand entry, if present, before comparing, and fails
  if anything else differs; the presence of that entry is asserted by A.4, not hidden here.
- [ ] 0.3 The same suite run with `CAIRN_OIDC_*` variables set (and `CAIRN_AUTH_MODE` unset)
  must produce identical results. Commit goldens with a regeneration flag that is off by default.

## A. Migration, model, identity service, CLI

Files: `alembic/versions/0013_oidc_identity.py`, `src/models/db.py` (User columns only),
`src/services/identity.py`, `src/cli.py` (the `users` subcommand group only),
`tests/test_migration_0013.py`, `tests/test_identity_cli.py`.

- [ ] A.1 Migration `0013` **without any table rebuild** (design D5): refuse below SQLite 3.35;
  upgrade = four `op.execute("ALTER TABLE users ADD COLUMN …")` statements carrying the named
  length CHECKs and the all-or-none CHECK (on `oidc_linked_at`, added last), then
  `CREATE UNIQUE INDEX uq_users_oidc_identity`; downgrade = drop the index, then drop
  `oidc_linked_at`, `oidc_subject`, `oidc_issuer`, `session_epoch` in that order. Never
  `batch_alter_table` on `users`.
- [ ] A.1a Preservation test on a populated `0012` fixture (two users, collections owned by each,
  files, runs, events with `acknowledged_by` set): record row counts and full ID sets of `users`,
  `collections`, `files`, `runs`, `events`, every `collections.user_id` and
  `events.acknowledged_by`; upgrade, assert identical and `PRAGMA foreign_key_check` empty;
  downgrade, assert identical again. A `sqlite3` trace callback asserts neither direction issues
  `CREATE TABLE`, `DROP TABLE` or `ALTER TABLE … RENAME` on `users`. Run with foreign keys ON,
  exactly as `alembic/env.py` connects.
- [ ] A.1b Schema tests: UNIQUE violation, each half-linked combination, issuer of 512 accepted
  and 513 refused, subject of 255 accepted and 256 refused (direct SQL writes, not the service).
- [ ] A.2 Model columns on `User`, with the same three named CHECKs and the unique index in
  `__table_args__`.
- [ ] A.3 `src/services/identity.py` per the contract: subject validation (non-empty, ≤ 255, no
  control characters), issuer validation whether given or defaulted (`https`, no userinfo, query
  or fragment, ≤ 512), issuer default from settings, the only-user default and the multi-user
  `--user` requirement, `replace` guard, pair-already-linked refusal, `session_epoch` increments
  on link/unlink/revoke, one commit per call.
- [ ] A.4 `cairn users list|link|unlink|revoke-sessions` in `src/cli.py`; non-zero exit with a
  one-line message on `IdentityError`; works in every auth mode without a server running. Assert
  explicitly that `cairn --help` lists `users` exactly once and that the slice 0 help goldens for
  every pre-existing command still pass unchanged.
- [ ] A.5 Tests for every scenario under "The CLI SHALL manage identity links…", including the
  512/513 issuer and 255/256 subject boundaries through the CLI.

## B. OIDC core and the login wall (after A and C merge)

Files: `src/config.py`, `src/main.py`, `src/database.py` (`ensure_implicit_user` only),
`src/csrf.py`, `src/auth/oidc.py`, `src/auth/wall.py`, `src/auth/routes.py`,
`src/auth/access_log.py` (new), `src/control_panel/routes.py` (`current_user`, `_base_context`'s
`session_username`, the `mode_toggle` handler, and the attribution log lines only),
`requirements.txt`, `pyproject.toml`, `tests/test_oidc_*.py`.

- [ ] B.1 Config: `auth_mode` gains `oidc`; the `CAIRN_OIDC_*` settings; the fail-loud
  validation list (D9), reading `public_url` from env/YAML only and requiring `https` in `oidc`
  mode only (an `http` public URL still loads in `single`/`multi`, unchanged). Tests per scenario.
- [ ] B.2 `ensure_implicit_user` runs in `single` and `oidc`. `SessionMiddleware` and the CSRF
  serializer take the real key in `oidc` mode with no fallback, `https_only=True`,
  `max_age=CAIRN_OIDC_SESSION_MAX_AGE`; `single` mode arguments unchanged (slice 0 goldens).
- [ ] B.3 `src/auth/oidc.py`: bounded transport (no redirects, `trust_env=False`, 10 s, 128 KiB),
  discovery cache (1 h, exact issuer, https endpoints), JWKS cache with rate-limited refetch on
  unknown `kid`, algorithm allowlist ∩ advertised, transaction store (TTL 5 min, 1024 cap, purge
  before admission, `Retry-After`, atomic single-use, browser binding), rate limits (global
  60/min, per-address 10/min start and callback), token exchange (basic|post), ID-token claim
  checks (including the `aud` profile: string, or array containing the client with more than
  one element only when `azp` equals the client ID), RFC 9207 `iss`, closed reason codes. Use Authlib + joserfc pinned exactly; run
  `make trivy` on the built image and record the result in the PR.
- [ ] B.4 `src/auth/wall.py`: default-deny middleware with the public allowlist; refusal shapes
  (303 / htmx 401 + `HX-Redirect` / JSON 401); safe `next`; per-request re-validation (exists,
  active, linked, epoch, absolute lifetime) clearing the session on failure. The local-path rule
  lives in one function used by `next`, the callback redirect and `/mode/toggle`.
- [ ] B.4a `/mode/toggle` in `oidc` mode: Location = path and query of the `Referer` only when it
  passes the local-path rule, else `/`. Tests: `Referer: https://evil.example/phish` →
  `Location: /phish` (never another origin), `//evil.example/x` and a backslash form → `/`, a
  same-site panel page → that page. `single` mode handler untouched (slice 0 golden).
- [ ] B.4b `_base_context` passes `session_username` (the session user's name in `oidc`, `None`
  otherwise); a test renders a real panel page in `oidc` mode and finds the sign-out control.
- [ ] B.5 `src/auth/routes.py`: `GET /login`, `POST /auth/oidc/start` (CSRF), `GET
  /auth/oidc/callback` (never renders at its own URL: success 303 → `next` with a fresh session,
  protocol failure 303 → `/login?error=failed`, D7 refusal 303 → `/auth/refused` with the one-shot
  `refused` session value), `GET /auth/refused` (403, consumes the value; without one it shows the
  generic text only), `POST /auth/logout` (CSRF). Callback and refusal responses carry
  `Referrer-Policy: no-referrer` and `Cache-Control: no-store`. Provider tokens discarded before
  the session is written.
- [ ] B.5a `src/auth/access_log.py`: in `oidc` mode only, a filter on the `uvicorn.access` logger
  that rewrites any `/auth/oidc/callback` request target to the bare path and drops a record whose
  arguments it does not recognise; installed from `src/main.py` so both entrypoints (`cairn serve`
  and the image's `uvicorn` CMD) get it.
- [ ] B.5b Real-server log test: start uvicorn in a subprocess through `cairn serve` and through
  the image's CMD argv, hit the callback with recognisable `code`/`state`/`iss` values on the
  success, failure and refusal paths, and assert none of those values nor the raw query string
  appear in captured stdout/stderr.
- [ ] B.6 `current_user` returns the session user in `oidc` mode; Accept, Ack and Ack-all log
  action, collection, username and `auth=oidc` (no subject).
- [ ] B.7 Tests with a local fake provider (signing keys generated in the test, never committed):
  every scenario under the `oidc-login` capability, including the route-enumeration test, alg
  `none`/`HS256` refusal, multi-audience with and without a matching `azp`, foreign `azp`, missing/mismatched `iss` parameter,
  replayed and cross-browser state, oversized and redirecting provider responses, unlink and
  epoch revocation mid-session, lifetime expiry, fixation, group checks, and a log-capture
  assertion that no secret, token, state, nonce, verifier or subject is logged.
- [ ] B.8 Slice 0 goldens still pass unchanged.

## C. Login page and templates (parallel with A and D; merges before B starts)

Files: `src/auth/ui.py`, `src/control_panel/templates/login.html`,
`src/control_panel/templates/auth_refused.html` (new), `src/control_panel/templates/base.html`
(signed-in user + sign-out control, rendered only when `auth_mode == "oidc"`),
`src/control_panel/static/css/panel.css` if needed, `tests/test_auth_templates.py`.

- [ ] C.1 `SSO_BUTTON_LABEL` in `src/auth/ui.py`, exposed to templates as a global.
- [ ] C.2 `login.html` in the Slate design: one "Sign in with SSO" POST form with the CSRF token
  and `next`, no username/password fields, light/dark toggle kept, `error` messages for
  `failed`/`busy`. Replace the stale "multi-user mode" info strip.
- [ ] C.3 `auth_refused.html`: "This identity is not authorized for this Cairn instance", the
  person's own issuer and subject in a copyable block, a link back to `/login`; no local username.
- [ ] C.4 `base.html`: in `oidc` mode only, the signed-in username and a CSRF-protected "Sign out"
  POST; `single` mode markup unchanged (slice 0 goldens).
- [ ] C.5 Template tests: render each template with the contract context; assert the label
  appears exactly once and is not spelled anywhere else in `src/`; phone width usable.

## D. Documentation (parallel; final review after B and C merge)

Files: `README.md`, `DEPLOYMENT.md`, `SECURITY.md`, `DESIGN.md` (§4 and the auth notes),
`CLAUDE.md`, `config.example.yaml`, `.env.example`, `docker-compose.example.yml`,
`Caddyfile.example`. Use placeholders only (`https://idp.example.com/realms/example`,
`https://cairn.example.com`, client ID `cairn`).

- [ ] D.1 DEPLOYMENT.md: an "Authentication" section — the three modes; a **provider setup**
  subsection (generic: confidential client, authorization code flow, redirect URI
  `https://cairn.example.com/auth/oidc/callback`, signing algorithm RS256/PS256/ES256, optional
  groups claim in the ID token; then a Keycloak example: client authentication on, standard flow
  only, valid redirect URI, a group-membership mapper added to the ID token with "full group
  path" as the operator prefers, where to find the user's ID = subject); `cairn users link`;
  the rollout (run alongside an existing front-door gate, link, verify, then remove the gate,
  keeping `/healthz` public); rollback; break-glass; the new variables in the configuration table.
- [ ] D.2 SECURITY.md: the `oidc` mode and what it protects; the network-isolation requirement
  (`cairn serve` trusts forwarded headers from any peer, so the container port must be reachable
  only by the reverse proxy); a TLS terminator can hijack sessions; the evidence (read-only
  mounts + OTS proofs) is unaffected by panel compromise; suppress callback query logging at the
  proxy.
- [ ] D.3 README: status and quickstart mention SSO; link to the provider setup.
- [ ] D.4 DESIGN.md §4: add `oidc` to the modes, update the "shipped state" note and the
  forward-auth caveat; record D1/D2 rationale briefly with a pointer to this change.
- [ ] D.5 `config.example.yaml`, `.env.example`: `auth_mode` values, commented `oidc_*` keys,
  secrets marked env-only.
- [ ] D.6 `docker-compose.example.yml`: comment showing the `oidc` variables and that the
  front-door auth middleware becomes optional once `oidc` is verified; `/healthz` router
  unchanged. `Caddyfile.example`: `oidc` mode proxies straight through, with a log filter
  example that drops the callback query string.
- [ ] D.7 CLAUDE.md: the `oidc-sso-login` note (mode, routes, wall allowlist, CLI, settings,
  accepted limitations) and the commands list gains `cairn users`.
- [ ] D.8 Public-repo gate (design D12) over the **whole tracked tree** (`git grep` on every
  tracked file: docs, archived OpenSpec changes, code comments, tests, design mock data), not the
  diff: personal names other than the copyright/author lines, real host paths, real collection
  sizes and file names, private-deployment monitor/ticket identifiers, private hostnames and
  repositories, private addresses, non-`example` emails. The names/hostnames list comes from the
  operator at gate time and is never committed; only generic category patterns may be.
- [ ] D.9 Redact or generalise every existing hit found by D.8 (DESIGN.md first; also CLAUDE.md,
  archived OpenSpec changes, code comments, tests, CI workflow comments and the design mock data)
  as **one dedicated commit** within this change, separate from the feature commits. It runs
  after A, B and C have merged (it touches files those slices own, so it is not part of the
  parallel D slice); behaviour
  and test assertions unchanged except for renamed fixture strings.

## E. Verification and archive (supervisor)

- [ ] E.1 `openspec validate oidc-sso-login --strict`; re-run the D.8 whole-tree gate on the final
  branch and record zero hits in the PR.
- [ ] E.2 `openspec-verifier` against the merged result; adversarial Codex review of slices A
  and B (auth, session and attribution correctness).
- [ ] E.3 Archive and docs as the last commits of the feature PR (`Closes #61`).
