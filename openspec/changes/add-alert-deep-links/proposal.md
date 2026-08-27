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
  panel, e.g. `https://cairn.example.com`. Optional, default unset, no secret content. A private or
  LAN address is fine if that is what a human actually reaches; a sub-path (`https://x/cairn`) is
  supported.
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
  - **Kuma push** — appended to the `msg` query parameter it already sends.
- **Graceful when unset:** with no `public_url` configured, `Alert.url` is `None` and every channel
  produces **byte-identical output to today** — the email stays a single `text/plain` part (no
  multipart), and the webhook omits the `url` key rather than sending `null` (a strict consumer
  rejecting an unknown field would turn a cosmetic change into a missed alert). Cairn never emits a
  relative or inferred link — a wrong link is worse than no link.
- **Validation is fail-soft at load, fail-loud on save.** A malformed `CAIRN_PUBLIC_URL` in the
  environment is treated as unset and logged, never raised: `get_settings()` builds the whole cached
  model, so raising there would stop startup, scanning, and every alert over a cosmetic setting. At
  the panel-save boundary a human is present, so an invalid value is refused with an inline error.
- **Stored overrides are validated on read.** The existing overlay applies DB values via
  `model_copy(update=...)`, which does not re-run validators — so a preseeded, hand-edited, or
  corrupt `app_settings.public_url` would otherwise sail straight into an outbound email. Every
  overlay read re-validates and drops a bad value back to the env default.
- **Escaping:** the collection name, summary, and every path are HTML-escaped in the new HTML part,
  and the URL is attribute-escaped in its `href`. File paths are attacker-influenced — anyone who
  can create a file in a watched directory chooses a string that Cairn then mails out.
- **Link construction cannot suppress an alert.** It sits in its own guard, separate from alert
  construction and dispatch: any failure yields a link-free alert that is still delivered.
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
- **Making the link work for non-owner recipients.** The review page is owner-scoped and answers
  "not found" to everyone else, deliberately not disclosing whether a collection exists. Alert
  recipients are arbitrary addresses, not Cairn users, so in `multi` mode a recipient who is not the
  owner reaches that "not found" page. Weakening the scoping to fix this is exactly the wrong trade
  for a security tool; mapping recipients to users is Phase-2 auth work. Recorded as an accepted
  limitation with a hint in the routing UI. Unconditionally fine in `single` mode, which is what
  ships today.
- **Deriving the public URL from request headers.** Alerts are dispatched from the scanner and the
  scheduler, where there is no request. The address is configuration, explicitly set once.
- **A full HTML email template system.** One small, inline-styled HTML part for the alert; no
  branding framework, no per-channel theming.

## Impact
- **Affected specs:** `alerting` (alerts carry a deep link to the review page; the fallback when
  unconfigured), `configuration` (the `public_url` setting and its validation), `web-panel` (the
  admin-editable Panel address field and its use for the monitoring URL).
- **Affected code:** `src/services/panel_url.py` (new: URL grammar + link builder),
  `src/config.py` (`public_url` field + fail-soft validator),
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
- **Backward compatibility:** total, and specified as a testable requirement rather than an
  aspiration. Existing deploys that never set `public_url` send byte-identical alerts on every
  channel to what they send now.

## Review history
The first draft of this proposal was audited by an independent cross-family reviewer before any code
was written, and returned FAIL with one blocker and five majors. All eight findings are folded in
above: link-building could suppress dispatch (blocker); a strict env validator could stop the whole
application; the URL grammar was underspecified and self-contradictory (it accepted `localhost`
while the alerting delta forbade it, and permitted query/fragment/userinfo/control characters);
DB overrides bypassed validation entirely; unconditional multipart contradicted the byte-identical
claim; `"url": null` did the same for webhooks; the clear-override storage contract was undefined;
and the test plan covered almost none of the per-channel contracts.

A second, independent review of the same draft added four more, also folded in: the review page is
owner-scoped so the link is a dead end for a non-owner recipient in `multi` mode (now an explicit,
documented limitation rather than a surprise); a non-ASCII URL passes a naive absolute-URL check but
raises when encoded into ntfy's `Click` header, silently costing an ntfy-only operator every alert
(the grammar is now ASCII-normalized or reject); the YAML overlay is also a configuration source and
the scenarios ignored it; and Kuma does carry human-readable text in its `msg` parameter, so
excluding it as "no message body" rested on a false premise.
