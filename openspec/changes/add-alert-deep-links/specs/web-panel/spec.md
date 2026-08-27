# web-panel Specification (delta)

## ADDED Requirements

### Requirement: Panel address is configurable from the Settings page

The Settings page SHALL present an admin-only "Panel address" field holding the panel's
externally-reachable base URL, saved to the app-settings overlay and used to build the review links
carried by outbound alerts. Non-admin users SHALL see the configured value read-only, consistent
with how the shared SMTP configuration is presented.

Saving SHALL validate the value against the canonical base-URL grammar (see the `configuration`
capability) and SHALL report a clear inline error on rejection, leaving the stored value unchanged.
This is the fail-loud boundary: a human is present to read the error, so an invalid value is
refused here rather than silently ignored. Saving an empty value SHALL **delete** the stored row,
so the environment value becomes visible again.

The page SHALL derive the health-monitoring URL it displays from this setting rather than a
hardcoded address, showing an illustrative example only while the setting is unconfigured, labelled
as an example.

#### Scenario: Admin sets the panel address

- **WHEN** an admin saves `https://cairn.example.com` as the panel address
- **THEN** it SHALL be persisted to the app-settings overlay and subsequent alerts SHALL link to
  `https://cairn.example.com/collection/{id}/review`

#### Scenario: An invalid address is rejected

- **WHEN** an admin saves a value that is not an absolute `http`/`https` URL
- **THEN** the page SHALL show a clear error, the value SHALL NOT be stored, and any previously
  stored value SHALL remain in effect

#### Scenario: Clearing the field falls back to the environment

- **WHEN** an admin saves an empty panel address
- **THEN** the stored row SHALL be deleted and the effective value SHALL come from
  `CAIRN_PUBLIC_URL`, or be unset if that is unset

#### Scenario: The saved address is shown back, normalized

- **WHEN** an admin saves `https://cairn.example.com/` and reloads the Settings page
- **THEN** the field SHALL show the normalized `https://cairn.example.com`, so the operator sees
  exactly what links will be built from

#### Scenario: Non-admins cannot edit it

- **WHEN** a non-admin user opens the Settings page
- **THEN** the panel address SHALL be shown read-only and any attempt to save it SHALL be refused

#### Scenario: The test email exercises the real link

- **WHEN** an admin sends a test email with a panel address configured
- **THEN** the test message SHALL contain a link built from that address, so the operator can
  confirm it is reachable before an incident
