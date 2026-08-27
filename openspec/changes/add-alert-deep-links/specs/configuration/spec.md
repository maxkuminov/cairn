# configuration Specification (delta)

## ADDED Requirements

### Requirement: Public panel URL for outbound links

The system SHALL expose an optional `public_url` setting (`CAIRN_PUBLIC_URL`) holding the
externally-reachable base URL of the web panel (for example `https://cairn.example.com`). It SHALL
default to unset. When set it SHALL be an absolute `http://` or `https://` URL; the system SHALL
reject any other value with a clear error naming the requirement, and SHALL normalize a stored value
by stripping trailing slashes. The value is not a secret and MAY appear in logs and the panel UI.

The system SHALL NOT infer this address from an incoming request, from `host`/`port`, or from any
other source: outbound alerts are dispatched from the scanner and the scheduler, where no request
exists, and a guessed address in an alert would send the operator to the wrong place.

#### Scenario: Unset by default

- **WHEN** the application starts with no `CAIRN_PUBLIC_URL` and no stored override
- **THEN** `public_url` SHALL be unset and no feature that depends on it SHALL emit a link

#### Scenario: A non-absolute value is rejected

- **WHEN** `public_url` is set to a value that is not an absolute `http`/`https` URL
  (for example `cairn.example.com`, `/cairn`, or an empty-host URL)
- **THEN** the value SHALL be rejected with a clear error and SHALL NOT be used to build links

#### Scenario: Trailing slashes are normalized

- **WHEN** `public_url` is set to `https://cairn.example.com/`
- **THEN** links built from it SHALL contain exactly one slash between the base and the path
  (`https://cairn.example.com/collection/1/review`)

### Requirement: Public URL is overridable from the panel, DB wins over env

The system SHALL persist a panel-set `public_url` in the `app_settings` key-value table and SHALL
overlay it over the environment-derived value on read, on the same precedence as the stored SMTP
configuration: **a stored value wins over the environment**, an absent row falls back to
`CAIRN_PUBLIC_URL`, and an unset environment leaves the setting unset. The overlay SHALL take effect
without restarting the application.

#### Scenario: Stored value overrides the environment

- **WHEN** `CAIRN_PUBLIC_URL` is set in the environment and a different `public_url` is stored in
  `app_settings`
- **THEN** the effective settings used to build alert links SHALL use the stored value

#### Scenario: Empty table falls back to the environment

- **WHEN** no `public_url` row exists in `app_settings`
- **THEN** the effective settings SHALL use `CAIRN_PUBLIC_URL`, or remain unset if that is unset
