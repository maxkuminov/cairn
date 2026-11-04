# Delta: configuration (oidc-sso-login)

## MODIFIED Requirements

### Requirement: Auth mode selector

The system SHALL expose `CAIRN_AUTH_MODE` accepting exactly `single`, `multi` or `oidc`,
defaulting to `single`. In `single` mode the application SHALL operate with one implicit user and
SHALL NOT present a login wall, and its behaviour (responses, cookies, session middleware
settings) SHALL be identical to the behaviour before `oidc` existed. In `multi` mode the
application SHALL require a configured session `secret_key`. In `oidc` mode the application SHALL
operate with one implicit owner behind the OIDC login wall defined by the `oidc-login`
capability. Settings prefixed `CAIRN_OIDC_` SHALL be ignored outside `oidc` mode.

#### Scenario: Invalid auth mode is rejected

- **WHEN** `CAIRN_AUTH_MODE` is set to a value other than `single`, `multi` or `oidc`
- **THEN** startup SHALL fail with a clear validation error naming the allowed values

#### Scenario: Multi mode requires a secret key

- **WHEN** `CAIRN_AUTH_MODE=multi` and no `secret_key` is configured
- **THEN** startup SHALL fail with an error instructing the operator to set `CAIRN_SECRET_KEY`

#### Scenario: Single mode is unchanged

- **WHEN** the application runs in `single` mode, with or without `CAIRN_OIDC_*` variables set
- **THEN** every panel response, cookie and session middleware setting SHALL match the recorded
  pre-change goldens

## ADDED Requirements

### Requirement: OIDC mode SHALL refuse to start with incomplete static configuration

In `oidc` mode startup SHALL fail with an error naming the offending setting when any of the
following holds: `CAIRN_OIDC_ISSUER`, `CAIRN_OIDC_CLIENT_ID` or `CAIRN_OIDC_CLIENT_SECRET` is
missing; the issuer is not an `https` URL or carries userinfo, a query or a fragment;
`CAIRN_PUBLIC_URL` from the environment or YAML overlay is unset or invalid;
`CAIRN_SECRET_KEY` is missing or shorter than 32 characters; `CAIRN_OIDC_EXTRA_SCOPES` contains
`offline_access`; `CAIRN_OIDC_TOKEN_AUTH_METHOD` is not `client_secret_basic` or
`client_secret_post`; or `CAIRN_OIDC_SESSION_MAX_AGE` is not a positive integer. In `oidc` mode
neither the session middleware nor the CSRF signer SHALL fall back to a built-in key. Startup
SHALL NOT contact the provider.

#### Scenario: Missing secret key

- **WHEN** `CAIRN_AUTH_MODE=oidc` and `CAIRN_SECRET_KEY` is unset
- **THEN** startup SHALL fail with an error naming `CAIRN_SECRET_KEY`

#### Scenario: Public URL only in the panel override

- **WHEN** `CAIRN_AUTH_MODE=oidc`, `CAIRN_PUBLIC_URL` is unset in env and YAML, and a public URL
  is stored only through the panel
- **THEN** startup SHALL fail with an error naming `CAIRN_PUBLIC_URL`

#### Scenario: Provider down at startup

- **WHEN** `CAIRN_AUTH_MODE=oidc` with complete configuration and the provider is unreachable
- **THEN** the application SHALL start, scan and answer `/healthz` normally
