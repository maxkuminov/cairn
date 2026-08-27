# alerting Specification (delta)

## ADDED Requirements

### Requirement: Alerts carry a deep link to the collection's review page

When `public_url` is configured, a dispatched alert SHALL carry an absolute link to the review page
of the collection the alert is about (`{public_url}/collection/{collection_id}/review`), and every
channel capable of conveying a link SHALL present it, so the recipient reaches the list of affected
files and the acknowledge/accept controls in one click.

When `public_url` is not configured, the alert SHALL carry no link and each channel SHALL fall back
to its previous link-free wording. The system SHALL NOT substitute a relative path, a `localhost`
address, or an address inferred from any other source — an alert SHALL contain either a correct
absolute link or none.

#### Scenario: Configured public URL produces a review link

- **WHEN** an alert is dispatched for a collection and `public_url` is configured
- **THEN** the alert SHALL carry `{public_url}/collection/{collection_id}/review`
- **AND** the email body SHALL present that link as a clickable action to review the changes

#### Scenario: Unconfigured public URL sends a link-free alert

- **WHEN** an alert is dispatched and `public_url` is not configured
- **THEN** the alert SHALL contain no URL and no placeholder or relative link
- **AND** the email SHALL still identify the collection, the summary, and the affected paths

#### Scenario: Every channel that can carry a link does

- **WHEN** an alert with a link is dispatched to the enabled channels
- **THEN** the email SHALL include it as a plaintext line and as a link in the HTML part,
  the webhook JSON payload SHALL include it as a `url` field, the ntfy notification SHALL set it as
  the notification's click target, and the Signal message SHALL append it to the text
- **AND** the Kuma push heartbeat, which carries no message body, SHALL be unaffected

#### Scenario: A malformed or missing link never breaks dispatch

- **WHEN** a link cannot be built for a collection
- **THEN** the alert SHALL still be dispatched without a link, and the scan that produced it SHALL
  still complete and record its run

## MODIFIED Requirements

### Requirement: Pluggable channels with email active, others scaffolded

The system SHALL provide pluggable notification channels behind a common notifier interface, with
SMTP email as the implemented, active channel and webhook / ntfy / Signal (CallMeBot) / Kuma-push
as scaffolded channels. Channel credentials SHALL come from configuration (env/secret) or the
app-settings overlay, never hardcoded; per-corpus routing parameters (recipients, URLs) live in the
corpus `alert_json`.

The email channel SHALL send a `multipart/alternative` message: a complete plaintext part that is
readable on its own, and an HTML part presenting the same information with the review link as a
clickable action. A client that renders only plaintext SHALL lose no information.

#### Scenario: Email is composed and sent via SMTP

- **WHEN** an alert is dispatched to an enabled email channel
- **THEN** the system SHALL compose a subject and body identifying the corpus and the change and
  send it via the configured SMTP server to the configured recipient(s)

#### Scenario: Plaintext-only clients lose nothing

- **WHEN** an alert email is opened in a client that renders only the plaintext part
- **THEN** the collection, summary, detection time, affected paths, and the review link (when
  configured) SHALL all be present in that part

#### Scenario: Only enabled channels receive the alert

- **WHEN** a corpus's `alert_json` enables some channels and disables others
- **THEN** the dispatch SHALL send only to the enabled channels
