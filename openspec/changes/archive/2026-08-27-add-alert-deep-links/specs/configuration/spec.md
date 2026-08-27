# configuration Specification (delta)

## ADDED Requirements

### Requirement: Public panel URL for outbound links

The system SHALL expose an optional `public_url` setting (`CAIRN_PUBLIC_URL`) holding the
externally-reachable base URL of the web panel. It SHALL default to unset. The value is not a secret
and MAY appear in logs and the panel UI.

A valid value SHALL be an absolute URL with scheme `http` or `https`, a non-empty host, an optional
port, and an optional path prefix (so a panel served under a reverse-proxy sub-path is
expressible). It SHALL NOT contain userinfo (`user:pass@`), a query string, a fragment, or any
ASCII control character or whitespace — each of those either changes where the link points or
corrupts the contexts the URL is embedded in (an HTML attribute, an HTTP header).

The normalized value SHALL be **pure ASCII**: a non-ASCII host SHALL be IDNA-encoded and a
non-ASCII path SHALL be percent-encoded, or the value SHALL be rejected. A URL is placed verbatim
into HTTP headers (ntfy's `Click`), and a non-ASCII header value raises at encoding time — which
would take out that channel's delivery entirely. Trailing slashes SHALL be stripped on
normalization.

The host is the operator's to choose: a private or LAN address such as `http://localhost:8000` or
`http://192.168.1.10:8000` SHALL be accepted, because for some deployments that *is* the address a
human reaches. What the system SHALL NOT do is **infer** an address it was not given.

#### Scenario: Unset by default

- **WHEN** the application starts with no `CAIRN_PUBLIC_URL`, no `public_url` in the YAML overlay,
  and no stored override
- **THEN** `public_url` SHALL be unset and no feature that depends on it SHALL emit a link

#### Scenario: The YAML overlay can supply it, below env

- **WHEN** `public_url` is set in the `CAIRN_CONFIG_FILE` overlay and `CAIRN_PUBLIC_URL` is also set
- **THEN** the environment value SHALL win, consistent with every other setting

#### Scenario: A non-ASCII URL is normalized or rejected, never emitted raw

- **WHEN** `public_url` is set to a value with a non-ASCII host or path (for example
  `https://cairn.example.com/é`)
- **THEN** the stored/normalized value SHALL be pure ASCII (IDNA host, percent-encoded path), or the
  value SHALL be rejected
- **AND** no channel SHALL ever be handed a URL that raises when encoded into an HTTP header

#### Scenario: A private address is accepted when explicitly configured

- **WHEN** an operator sets `public_url` to `http://localhost:8000`
- **THEN** it SHALL be accepted and used verbatim to build links

#### Scenario: Structurally invalid values are rejected

- **WHEN** `public_url` is set to a value that is not an absolute `http`/`https` URL with a non-empty
  host (for example `cairn.example.com`, `/cairn`, `javascript:alert(1)`, or `https://`), or that
  carries userinfo, a query string, a fragment, or a control character (for example
  `https://u:p@cairn.example.com`, `https://cairn.example.com/#overview`, or a value containing
  `"` or a newline)
- **THEN** the value SHALL be treated as invalid and SHALL NOT be used to build any link

#### Scenario: Trailing slashes are normalized

- **WHEN** `public_url` is set to `https://cairn.example.com/`
- **THEN** links built from it SHALL contain exactly one slash between the base and the path
  (`https://cairn.example.com/collection/1/review`)

#### Scenario: A path prefix is preserved

- **WHEN** `public_url` is set to `https://example.com/cairn` (a sub-path deployment)
- **THEN** a review link SHALL be `https://example.com/cairn/collection/1/review`

### Requirement: An invalid public URL SHALL NOT prevent the application from running

A malformed `public_url` arriving from the environment or the YAML overlay SHALL be treated as
**unset**, and the system SHALL log one warning naming the setting and the reason. Startup,
scanning, scheduling, and alert dispatch SHALL proceed normally, minus the link.

This is deliberate asymmetry: Cairn's job is to notice that files changed and say so. A typo in a
cosmetic link setting must never cost the operator a scan or an alert — that would trade a small
inconvenience for the exact failure this product exists to prevent. Validation is therefore
**fail-soft at load time and fail-loud at the panel-save boundary**, where a human is present to
read the error and fix it.

#### Scenario: Malformed environment value does not block startup

- **WHEN** the application starts with `CAIRN_PUBLIC_URL=cairn.example.com` (no scheme)
- **THEN** startup SHALL succeed, scans and alerts SHALL run normally, alerts SHALL carry no link,
  and a warning naming `CAIRN_PUBLIC_URL` SHALL be logged

### Requirement: Public URL is overridable from the panel, DB wins over env, and is validated on read

The system SHALL persist a panel-set `public_url` in the `app_settings` key-value table and SHALL
overlay it over the environment-derived value on read, on the same precedence as the stored SMTP
configuration: **a stored value wins over the environment**, an absent row falls back to
`CAIRN_PUBLIC_URL`, and an unset environment leaves the setting unset. The overlay SHALL take effect
without restarting the application.

Because the overlay applies stored values to the settings model without re-running its validators, a
stored `public_url` SHALL be validated and normalized **on every overlay read**. A stored value that
does not validate SHALL be ignored — the environment value applies as though no row existed — and a
warning SHALL be logged. An unvalidated stored value SHALL never reach an outbound alert, regardless
of how it came to be in the table (panel save, manual edit, restored backup, or corruption).

Clearing the override SHALL **delete** the stored row rather than store an empty value, so that the
environment value becomes visible again.

#### Scenario: Stored value overrides the environment

- **WHEN** `CAIRN_PUBLIC_URL` is set and a different, valid `public_url` is stored in `app_settings`
- **THEN** the effective settings used to build alert links SHALL use the stored value

#### Scenario: Empty table falls back to the environment

- **WHEN** no `public_url` row exists in `app_settings`
- **THEN** the effective settings SHALL use `CAIRN_PUBLIC_URL`, or remain unset if that is unset

#### Scenario: An invalid stored value is ignored, not emitted

- **WHEN** an `app_settings` row `public_url` holds `javascript:alert(1)` (or any value failing
  validation) and `CAIRN_PUBLIC_URL=https://cairn.example.com` is set
- **THEN** the effective `public_url` SHALL be `https://cairn.example.com`, a warning SHALL be
  logged, and the invalid value SHALL NOT appear in any alert

#### Scenario: Clearing the override exposes the environment value

- **WHEN** a stored `public_url` override is cleared while `CAIRN_PUBLIC_URL=https://env.example`
  is set
- **THEN** the stored row SHALL be deleted and the effective `public_url` SHALL become
  `https://env.example`
