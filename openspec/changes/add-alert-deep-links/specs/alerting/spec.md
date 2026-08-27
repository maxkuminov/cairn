# alerting Specification (delta)

## ADDED Requirements

### Requirement: Alerts carry a deep link to the collection's review page

When `public_url` is configured, a dispatched alert SHALL carry an absolute link to the review page
of the collection the alert is about (`{public_url}/collection/{collection_id}/review`), and every
channel capable of conveying a link SHALL present it, so the recipient reaches the list of affected
files and the acknowledge/accept controls in one click.

When `public_url` is not configured, the alert SHALL carry no link and each channel SHALL fall back
to its previous, link-free output. The system SHALL NOT substitute a relative path or an address
inferred from request headers, the bound `host`/`port`, or any other source — the only source of the
address is the configured setting. An alert SHALL contain either a correct absolute link or none.

#### Scenario: Configured public URL produces a review link

- **WHEN** an alert is dispatched for a collection and `public_url` is configured
- **THEN** the alert SHALL carry `{public_url}/collection/{collection_id}/review`
- **AND** the email SHALL present that link as a clickable action to review the changes

#### Scenario: Unconfigured public URL sends a link-free alert

- **WHEN** an alert is dispatched and `public_url` is not configured
- **THEN** the alert SHALL contain no URL and no placeholder, relative, or inferred link
- **AND** the email SHALL still identify the collection, the summary, and the affected paths

#### Scenario: Every channel that can carry a link does

- **WHEN** an alert with a link is dispatched to the enabled channels
- **THEN** the email SHALL include it as a plaintext line and as a link in the HTML part,
  the webhook JSON payload SHALL include it as a `url` field, the ntfy notification SHALL set it as
  the notification's click target, and the Signal message SHALL append it to the text
- **AND** the Kuma push heartbeat, which carries no message body, SHALL be unaffected

### Requirement: Building the link SHALL NOT be able to suppress an alert

Link construction SHALL be isolated from alert construction and dispatch. Any failure while
resolving the effective settings or building the URL SHALL result in an alert with no link that is
**still dispatched to every enabled channel**. It SHALL NOT skip, delay, or abort dispatch, and it
SHALL NOT affect the scan.

The alert is the product's whole purpose; the link is a convenience attached to it. No convenience
may be placed on the path that decides whether the operator is told at all.

#### Scenario: A raising link builder still delivers the alert

- **WHEN** resolving the effective settings or building the review URL raises an exception during a
  scan that detected an alarming change
- **THEN** the alert SHALL still be dispatched to every enabled channel, carrying no link
- **AND** the failure SHALL be logged
- **AND** the scan SHALL still complete and record its run

## MODIFIED Requirements

### Requirement: Pluggable channels with email active, others scaffolded

The system SHALL provide pluggable notification channels behind a common notifier interface, with
SMTP email as the implemented, active channel and webhook / ntfy / Signal (CallMeBot) / Kuma-push
as scaffolded channels. Channel credentials SHALL come from configuration (env/secret) or the
app-settings overlay, never hardcoded; per-corpus routing parameters (recipients, URLs) live in the
corpus `alert_json`.

**A link-free alert SHALL produce byte-identical channel output to that produced before deep links
existed.** Specifically: an email with no link SHALL remain a single `text/plain` message, and a
webhook payload with no link SHALL omit the `url` key entirely rather than send a null. A
deployment that never configures `public_url` SHALL observe no change whatsoever, and a strict
webhook consumer SHALL NOT encounter a new or null-valued field.

When an alert **does** carry a link, the email SHALL be a `multipart/alternative` message: a
complete plaintext part that is readable on its own, and an HTML part presenting the same
information with the link as a clickable action. A client that renders only plaintext SHALL lose no
information.

#### Scenario: Email is composed and sent via SMTP

- **WHEN** an alert is dispatched to an enabled email channel
- **THEN** the system SHALL compose a subject and body identifying the corpus and the change and
  send it via the configured SMTP server to the configured recipient(s)

#### Scenario: A link-free email is unchanged from today

- **WHEN** an alert with no link is sent by email
- **THEN** the message SHALL be a single `text/plain` part with the previous wording, with no HTML
  alternative part

#### Scenario: A link-free webhook payload omits the key

- **WHEN** an alert with no link is sent to a webhook
- **THEN** the JSON payload SHALL NOT contain a `url` key

#### Scenario: Plaintext-only clients lose nothing

- **WHEN** an alert email carrying a link is opened in a client that renders only the plaintext part
- **THEN** the collection, summary, detection time, affected paths, and the review link SHALL all be
  present in that part

#### Scenario: Only enabled channels receive the alert

- **WHEN** a corpus's `alert_json` enables some channels and disables others
- **THEN** the dispatch SHALL send only to the enabled channels

### Requirement: Untrusted alert content SHALL be escaped for the context it is rendered into

An alert's collection name, summary, and affected paths SHALL be escaped for the context each
channel renders them into, and the link SHALL be attribute-escaped where it is emitted as an HTML
`href`. **File paths are attacker-influenced**: anyone able to create a file inside a watched
directory chooses a string that Cairn then places in an outbound message. A path is data, never
markup.

#### Scenario: A path containing HTML metacharacters is escaped

- **WHEN** an alert's paths include a filename containing `<`, `>`, `&`, or `"` (for example
  `<img src=x onerror=alert(1)>.txt`)
- **THEN** the HTML part of the email SHALL render it as literal text, not as markup

#### Scenario: The link is attribute-escaped in the href

- **WHEN** the HTML part emits the review link as an anchor
- **THEN** the URL SHALL be escaped for an HTML attribute context

#### Scenario: Header-bound values cannot inject headers

- **WHEN** a link or collection name is placed in a message header or an ntfy `Click` header
- **THEN** the resulting message SHALL contain no injected header, and a value that cannot be
  safely represented SHALL be omitted rather than emitted
