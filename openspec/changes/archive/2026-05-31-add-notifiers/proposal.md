## Why

Cairn detects missing/modified files and records events, but a safety tool is only useful if it
*tells you*. This change adds pluggable notifications: when a scan newly detects an alarming
change (a missing file, or a modified file in a WORM corpus), Cairn routes an alert to the
channels the owner configured for that corpus. Email (SMTP) is the shipped, active channel;
webhook / ntfy / Signal (CallMeBot) / Kuma-push are scaffolded plugins.

References: DESIGN.md §5 (`notify/` plugins: smtp, signal_callmebot, webhook, ntfy, kuma_push;
alert routing per user/corpus), §3 ("core vs personal" — credentials from env/secret, never
hardcoded). Note: the dead-man's-switch is the `/healthz` **poll** model (from `add-scheduler`);
`kuma_push` is an optional legacy push channel, not the primary heartbeat.

## What Changes

- **Notifier plugins** (`src/notify/`): a small `Notifier` protocol (`name`, `send(alert)`), a
  registry, and implementations:
  - `smtp.py` — **active**: send email via SMTP (host/port/STARTTLS/credentials from config).
    The Resend / AWS-SES providers from the settings UI are recognized; SMTP is the implemented
    transport, Resend/SES are config-validated stubs that raise a clear "not yet wired" if
    selected (kept minimal — the UI already shows them).
  - `webhook.py` — POST a JSON payload to a configured URL.
  - `ntfy.py` — POST to an ntfy topic/server.
  - `signal_callmebot.py` — GET the CallMeBot API for a Signal message.
  - `kuma_push.py` — GET an Uptime-Kuma push URL (optional; status up/down).
- **Alert routing + dispatch** (`src/notify/dispatch.py`): `dispatch(alert, corpus, settings)`
  reads the corpus's `alert_json` (which channels are enabled + their per-corpus params, e.g. the
  email recipient) plus global channel credentials from config, and fans out to the enabled
  channels. Each send is best-effort: a channel failure is logged and never raises into the
  scanner. An `Alert` dataclass carries corpus name, kind summary (e.g. "2 missing, 1 modified"),
  the affected relpaths (capped), and detected-at.
- **Scanner integration**: `scan_corpus` collects the events it *newly* creates this run and, after
  the DB commit, dispatches a single batched alert per corpus when there are new **alarming**
  events (missing in any mode; modified in WORM — churn re-baselines are not alarming, `added` is
  informational). It alerts on new detections only (not on the whole unacknowledged backlog), so
  you are not re-nagged every scan. Dispatch failure never fails the scan.
- **Corpus alert config**: the add/edit-corpus form's Email toggle + recipient now persist into
  `alert_json` (`{"email": {"enabled": true, "to": ["addr"]}}`); the planned channels remain
  shown-disabled. Settings → Notifications reflects the configured global channels.
- **Config**: SMTP settings (`smtp_host`, `smtp_port`, `smtp_starttls`, `smtp_user`, `smtp_password`,
  `smtp_from`), plus optional global defaults for the other channels. All from env/secret file.

### Out of scope (deferred)

- Resend / AWS-SES HTTP transports (UI options exist; SMTP is the shipped transport).
- Telegram (not in the `notify/` set; the UI shows it "Planned").
- Alerting on `/healthz` degraded / stale-incomplete proofs from a scheduler hook — the dispatch
  layer is reusable for it, but wiring those triggers is a later refinement.
- Per-user (vs per-corpus) channel preferences UI beyond the email recipient — multi-user.

## Capabilities

### New Capabilities

- `alerting`: pluggable notification channels (SMTP active; webhook/ntfy/Signal/Kuma scaffolded),
  per-corpus routing via `alert_json`, and best-effort dispatch of a batched alert when a scan
  newly detects missing/modified-WORM changes.

### Modified Capabilities

None (the scanner gains a post-commit dispatch hook; its existing requirements are unchanged).

## Impact

- **Code**: `src/notify/{base,dispatch,smtp,webhook,ntfy,signal_callmebot,kuma_push}.py` (new),
  `src/services/scanner.py` (collect new alarming events + post-commit dispatch hook),
  `src/config.py` (channel settings), `src/control_panel/routes.py` + the corpus form template
  (persist the Email toggle/recipient into `alert_json`).
- **Database**: reads/writes `corpora.alert_json`. No schema change.
- **Tests**: `tests/test_notify.py` — the SMTP message is composed correctly (transport mocked);
  dispatch fans out only to enabled channels; a missing file (any mode) and a WORM modification
  trigger an alert while a churn modification and an `added` event do not; a channel error doesn't
  break dispatch/scan; alert-config round-trips through the corpus form.
