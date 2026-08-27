# Put a one-click review link in every alert

## Why
Cairn's alert email tells the operator *that* something happened and *which paths* changed, then
ends with:

> Review and acknowledge in the Cairn panel.

That last line is a dead end. The reader — often on a phone, away from the machine — has to
remember the panel's address, open it, find the right collection, and click through to its review
page before they can see what's missing and act on it. Every one of those steps is friction sitting
between "your files may be gone" and "here is what to do about it", and the review page
(`GET /collection/{id}/review`) that answers exactly that question already exists.

The alert should carry the link. Click it, land on the page listing the missing/modified files with
the acknowledge / accept / copy-paths controls already in reach.

Cairn cannot build that link today because it does not know its own public address: there is no
setting for it. (The same gap is why the Settings page currently prints a hardcoded placeholder,
`https://cairn.example.com/healthz`, as the monitoring URL.)

## What Changes
- Add a **`public_url`** setting (`CAIRN_PUBLIC_URL`) — the externally-reachable base URL of the
  panel, e.g. `https://cairn.example.com`. Optional, default unset, no secret content.
- Make it **editable from the panel** (Settings → Notifications, admin-only) using the existing
  `app_settings` key-value overlay, on the same **DB-wins-over-env** precedence as the SMTP config.
  No migration: `app_settings` already exists and is keyed by `Settings` field name.
- Add **`Alert.url`** to the notifier payload. When `public_url` is set, a scan's alert carries
  `{public_url}/collection/{id}/review` — the deep link to that collection's review page.
- Render the link in every channel that can carry one:
  - **email** — a plaintext "Review and acknowledge: <url>" line **and** a new HTML alternative part
    with a real "Review in Cairn" button (the message becomes `multipart/alternative`; the
    plaintext part remains complete and readable on its own).
  - **webhook** — a `url` field in the JSON payload.
  - **ntfy** — the native `Click` header (tap the notification, land on the review page) plus the
    link in the body.
  - **Signal (CallMeBot)** — the URL appended to the message text.
  - **Kuma push** is a heartbeat ping with no message body; it is unchanged.
- **Graceful when unset:** with no `public_url` configured, `Alert.url` is `None` and every channel
  falls back to today's wording. Cairn SHALL NOT emit a relative, `localhost`, or guessed link into
  an outbound alert — a wrong link is worse than no link.
- Validate on save: an absolute `http(s)://` URL only, trailing slash normalized away; anything else
  is rejected with a form error rather than silently stored.
- Use the same setting for the Settings page's monitoring URL, replacing the hardcoded
  `https://cairn.example.com/healthz` placeholder (which stays as the shown example only while
  `public_url` is unset).
- The "Send test email" button exercises the real link, so an operator can confirm the address is
  right before an actual incident. Its test alert points at the collections list (`/collections`),
  since no specific collection is involved.

## Non-goals
- **Per-file deep links or anchors.** One link per alert, to the collection's review page; the page
  already lists every affected file.
- **Authentication / one-click-from-email actions.** The link is an ordinary panel URL and lands on
  whatever auth the deployment fronts the panel with (Traefik OAuth today). No tokenized
  acknowledge-by-email, no bearer links in mail — that is a security surface, not a convenience, and
  is out of scope.
- **Deriving the public URL from request headers.** Alerts are dispatched from the scanner and the
  scheduler, where there is no request. The address is configuration, explicitly set once.
- **A full HTML email template system.** One small, inline-styled HTML part for the alert; no
  branding framework, no per-channel theming.

## Impact
- **Affected specs:** `alerting` (alerts carry a deep link to the review page; the fallback when
  unconfigured), `configuration` (the `public_url` setting and its validation), `web-panel` (the
  admin-editable Panel address field and its use for the monitoring URL).
- **Affected code:** `src/config.py` (`public_url` field + validator),
  `src/services/app_settings.py` (a `public_url` key in the overlay + a save function),
  `src/notify/base.py` (`Alert.url`), `src/notify/smtp.py` (plaintext line + HTML alternative part),
  `src/notify/webhook.py`, `src/notify/ntfy.py`, `src/notify/signal_callmebot.py`,
  `src/services/scanner.py` (build the review URL at the dispatch site),
  `src/control_panel/routes.py` (settings form plumbing, test-email link, `healthz_url`),
  `src/control_panel/templates/settings.html` (the field), `config.example.yaml` /
  `docker-compose.example.yml` / `DEPLOYMENT.md` (document `CAIRN_PUBLIC_URL`), `tests/`.
- **Data migration:** none. `public_url` is a new *row* in the existing `app_settings` table,
  written only when an admin saves it; an absent row falls back to env, and an unset env falls back
  to today's link-free behavior.
- **Backward compatibility:** total. Existing deploys that never set `public_url` send byte-identical
  alerts to what they send now.
