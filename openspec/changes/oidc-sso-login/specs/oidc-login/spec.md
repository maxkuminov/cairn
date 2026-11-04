# Delta: oidc-login (oidc-sso-login)

## ADDED Requirements

### Requirement: In oidc mode every panel route SHALL require a verified session, by default deny

When `CAIRN_AUTH_MODE=oidc`, the application SHALL refuse every HTTP request that does not carry
a valid session, except requests to an explicit public allowlist: `/healthz`, `/static/*`,
`/login`, `/auth/oidc/start`, `/auth/oidc/callback`, `/auth/logout` and `/mode/toggle`. The check
SHALL be applied to all routes centrally, so a route added later is protected without being
listed. An unauthenticated browser GET SHALL be redirected (303) to `/login` with a `next`
parameter; an htmx request SHALL receive 401 with an `HX-Redirect: /login` header; a request
under `/api` SHALL receive a JSON 401. A `next` value SHALL be honoured only when it is a local
absolute path (a single leading `/`, no scheme, no `//`, no backslash); anything else SHALL be
replaced by `/`.

#### Scenario: Anonymous request to the dashboard

- **WHEN** a request without a session asks for `/` in `oidc` mode
- **THEN** the response SHALL be a 303 redirect to `/login?next=/`
- **AND** no collection data SHALL be rendered

#### Scenario: Health endpoint stays public

- **WHEN** a request without a session asks for `/healthz` in `oidc` mode
- **THEN** it SHALL receive the same health response it would in `single` mode, with `mode`
  reported as `oidc`

#### Scenario: Every non-allowlisted route is walled

- **WHEN** the test suite enumerates every registered route and requests each without a session
- **THEN** every route not on the public allowlist SHALL refuse

#### Scenario: Open redirect refused

- **WHEN** the login flow completes with `next=//evil.example.com/` or `next=https://evil.example.com/`
- **THEN** the user SHALL be redirected to `/`

### Requirement: Login SHALL use the OIDC authorization code flow with PKCE, state and nonce

The login page in `oidc` mode SHALL offer a single control labelled "Sign in with SSO", with the
label defined in exactly one place in the code, and SHALL NOT offer a password form. Starting a
login SHALL be a CSRF-protected POST that creates a single-use transaction (random `state`,
random `nonce`, PKCE `S256` verifier, local `next` path, 5-minute expiry), binds its `state` to
the initiating browser session, and redirects to the provider's authorization endpoint with the
redirect URI `{public_url}/auth/oidc/callback` built from the env/YAML `CAIRN_PUBLIC_URL`. The
redirect URI SHALL NOT be derived from request headers or from the panel-editable public URL
override. Requested scopes SHALL be `openid` plus any configured extra scopes and SHALL NOT
include `offline_access`.

#### Scenario: Start redirects to the provider

- **WHEN** a user presses "Sign in with SSO"
- **THEN** the browser SHALL be redirected to the discovered authorization endpoint carrying
  `response_type=code`, the client ID, the configured redirect URI, `state`, `nonce`,
  `code_challenge` and `code_challenge_method=S256`

#### Scenario: Forwarded host does not change the redirect URI

- **WHEN** the start request carries `X-Forwarded-Host: attacker.example.com`
- **THEN** the redirect URI sent to the provider SHALL still be built from `CAIRN_PUBLIC_URL`

#### Scenario: Replayed or cross-browser callback

- **WHEN** a callback arrives with a `state` that is expired, already consumed, unknown, or not
  the one stored in this browser's session
- **THEN** login SHALL fail and no session SHALL be created

### Requirement: The callback SHALL validate the provider response and ID token strictly

The callback SHALL require the authorization response `iss` parameter when discovery advertises
`authorization_response_iss_parameter_supported`, and whenever `iss` is present SHALL require it
to equal the configured issuer. It SHALL exchange the code with the PKCE verifier using
`client_secret_basic` or `client_secret_post` as configured. It SHALL accept an ID token only if
its signature verifies against the issuer's JWKS with an algorithm in {RS256, PS256, ES256}
intersected with the provider's advertised ID-token algorithms, and SHALL always refuse `none`
and `HS*`. The token SHALL have `iss` equal to the issuer, `aud` equal to the client ID or a
one-element array holding it, `azp` (if present) equal to the client ID, integer `exp` in the
future and integer `iat` not in the future (30 s leeway), `nonce` equal to the transaction's, and
a non-empty string `sub` of at most 255 characters. Discovery SHALL require the document's
`issuer` to equal the configured issuer exactly and its endpoints to be `https` without userinfo
or fragment. All provider HTTP calls SHALL disable redirects and environment proxies, time out
after 10 s, and cap response bodies at 128 KiB.

#### Scenario: Wrong audience

- **WHEN** a correctly signed ID token carries `aud: ["cairn", "other"]`
- **THEN** login SHALL be refused

#### Scenario: Unsigned or symmetric token

- **WHEN** the ID token's header names `alg: none` or `alg: HS256`
- **THEN** login SHALL be refused even if the provider advertises that algorithm

#### Scenario: Mix-up via iss parameter

- **WHEN** the provider advertises RFC 9207 support and the callback has no `iss`, or an `iss`
  that differs from the configured issuer
- **THEN** login SHALL be refused before the token exchange

#### Scenario: Oversized provider response

- **WHEN** the discovery, JWKS or token response body exceeds 128 KiB or answers with a redirect
- **THEN** login SHALL be refused with a closed reason and nothing SHALL be cached

### Requirement: Only an identity linked by issuer and subject SHALL be admitted, with no provisioning

After a valid ID token, the application SHALL admit the login only if exactly one active user has
`oidc_issuer` equal to the token's `iss` and `oidc_subject` equal to its `sub`, and, when
`CAIRN_OIDC_REQUIRED_GROUP` is set, the configured groups claim is an array of at most 256
strings of at most 512 characters that contains the required group by exact case-sensitive
equality. It SHALL NOT create, modify or link users at login, and SHALL NOT consult email,
username or any other claim for admission. Every refusal at this stage SHALL return the same
403 page, which SHALL NOT reveal any local username, whether a local user exists, or which check
failed; it MAY show the signed-in person their own issuer and subject.

#### Scenario: Unlinked identity

- **WHEN** a valid ID token's `(iss, sub)` matches no user
- **THEN** login SHALL be refused with the generic 403 page
- **AND** no user row SHALL be created or changed

#### Scenario: Same email, different subject

- **WHEN** a valid ID token carries the operator's email address but a subject that is not linked
- **THEN** login SHALL be refused

#### Scenario: Required group missing or case-variant

- **WHEN** `CAIRN_OIDC_REQUIRED_GROUP` is set and the token's groups claim is missing, a scalar,
  or contains only a case variant of the required group
- **THEN** login SHALL be refused with the same 403 page as an unlinked identity

### Requirement: OIDC sessions SHALL be fresh, bounded and re-validated on every request

A successful login SHALL clear any existing session before writing the new one, and SHALL store
only the user ID, the user's `session_epoch`, the login time, the auth method and a new CSRF
nonce: no subject, claims or provider tokens. Provider codes and tokens SHALL be discarded before
the session is written and SHALL NOT be stored in cookies, the database or logs. On every walled
request the application SHALL reject and clear the session unless the user exists, is active, is
still linked, has a `session_epoch` equal to the session's, and the login is younger than
`CAIRN_OIDC_SESSION_MAX_AGE` (default 43200 seconds). In `oidc` mode the session cookie SHALL be
`Secure` and `SameSite=Lax` with a max age equal to that lifetime. Logout SHALL be a
CSRF-protected POST that clears the local session.

#### Scenario: Unlink ends an active session

- **WHEN** a signed-in user is unlinked with `cairn users unlink`
- **THEN** that user's next panel request SHALL be refused and redirected to `/login`

#### Scenario: Absolute lifetime

- **WHEN** a session's login time is older than `CAIRN_OIDC_SESSION_MAX_AGE`
- **THEN** the next request SHALL be refused even if the cookie itself is still valid

#### Scenario: Session fixation

- **WHEN** a browser holding a pre-login session completes a login
- **THEN** the pre-login session values, including its CSRF nonce, SHALL NOT survive into the
  new session

### Requirement: Integrity-critical actions SHALL be attributed to the verified session user

In `oidc` mode, Accept (every scoped accept), Acknowledge and Acknowledge-all SHALL record the
session's user as `acknowledged_by`, as they do today for the implicit user, and SHALL write an
application log line naming the action, the collection, the local username and the auth method.
The log line SHALL NOT contain the subject or any token.

#### Scenario: Accept after SSO login

- **WHEN** a user signed in through SSO accepts a collection's missing files
- **THEN** the acknowledged events SHALL carry that user's ID
- **AND** the log SHALL record the action with that user's username and `auth=oidc`

### Requirement: OIDC login SHALL be bounded and its logs redacted

The application SHALL keep at most 1024 live login transactions, purge expired ones before
admitting a new start, and refuse a new start with `Retry-After` rather than evict a live one. It
SHALL apply a process-global start budget and per-address start and callback limits. Login logs
SHALL record start, success and refusal with a closed reason code, and SHALL NOT contain the
callback query, authorization code, tokens, `state`, `nonce`, PKCE verifier, client secret,
subject or raw claims. Provider discovery and JWKS failures SHALL fail the login closed and
SHALL NOT affect scanning, scheduling or `/healthz`.

#### Scenario: Full transaction store

- **WHEN** 1024 unexpired transactions are live and another login starts
- **THEN** the start SHALL be refused with `Retry-After` and no live transaction SHALL be evicted

#### Scenario: Provider unreachable

- **WHEN** the provider's discovery endpoint cannot be reached
- **THEN** login SHALL fail with a generic message
- **AND** scheduled scans and `/healthz` SHALL behave as if the provider were up

#### Scenario: Callback logging

- **WHEN** a callback succeeds or fails
- **THEN** no log line SHALL contain the callback query string, a token, the subject or the
  client secret

### Requirement: The CLI SHALL manage identity links and work without authentication

The `cairn` CLI SHALL provide `users list`, `users link --subject SUB [--issuer URL] [--user
NAME] [--replace]`, `users unlink [--user NAME]` and `users revoke-sessions [--user NAME]`, and
every CLI command SHALL keep working without any login in every auth mode. `link` SHALL default
the issuer to `CAIRN_OIDC_ISSUER` and the user to the only user, SHALL require `--user` when more
than one user exists, SHALL require `--replace` to relink an already-linked user, and SHALL
refuse an empty subject, one longer than 255 characters, one containing control characters, and
a pair already linked to another user. `link`, `unlink` and `revoke-sessions` SHALL increment the
user's `session_epoch`.

#### Scenario: Link the operator

- **WHEN** an operator runs `cairn users link --subject 1234-abcd` with `CAIRN_OIDC_ISSUER` set
  and one user in the database
- **THEN** that user SHALL be linked to `(CAIRN_OIDC_ISSUER, "1234-abcd")` with `oidc_linked_at`
  set, and its collections SHALL keep their owner

#### Scenario: Relink without --replace

- **WHEN** an operator links a user that is already linked, without `--replace`
- **THEN** the command SHALL exit non-zero and change nothing

#### Scenario: Revoke sessions

- **WHEN** an operator runs `cairn users revoke-sessions`
- **THEN** every existing session of that user SHALL be refused on its next request, and the
  link SHALL be unchanged
