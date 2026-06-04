## ADDED Requirements

### Requirement: Environment-driven settings with no hardcoded secrets or host paths

The system SHALL load all configuration from environment variables (prefix `CAIRN_`) with an
optional YAML overlay file (`CAIRN_CONFIG_FILE`), where environment values take precedence.
Secrets (session key, notifier credentials, node RPC URLs) and host-specific paths SHALL NOT be
hardcoded in tracked source; they SHALL come from the environment or a referenced secret/YAML
file. Reasonable non-secret defaults SHALL be provided (datastore path, proof-store path, public
OTS calendars, block-explorer verify backend).

#### Scenario: Defaults apply with no configuration

- **WHEN** the application starts with no `CAIRN_*` variables and no config file set
- **THEN** it SHALL use `sqlite+aiosqlite:///./data/cairn.db` as the datastore, `./proofs` as
  the proof store, the default public OTS calendars, and the block-explorer verify backend
- **AND** it SHALL run in `single` auth mode

#### Scenario: Environment overrides YAML overlay

- **WHEN** both `CAIRN_CONFIG_FILE` defines a key and an environment variable defines the same key
- **THEN** the environment variable value SHALL win

### Requirement: Auth mode selector

The system SHALL expose `CAIRN_AUTH_MODE` accepting exactly `single` or `multi`, defaulting to
`single`. In `single` mode the application SHALL operate with one implicit user and SHALL NOT
present a login wall. In `multi` mode the application SHALL require a configured session
`secret_key`.

#### Scenario: Invalid auth mode is rejected

- **WHEN** `CAIRN_AUTH_MODE` is set to a value other than `single` or `multi`
- **THEN** startup SHALL fail with a clear validation error naming the allowed values

#### Scenario: Multi mode requires a secret key

- **WHEN** `CAIRN_AUTH_MODE=multi` and no `secret_key` is configured
- **THEN** startup SHALL fail with an error instructing the operator to set `CAIRN_SECRET_KEY`
