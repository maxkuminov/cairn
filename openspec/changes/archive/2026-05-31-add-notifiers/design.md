## Context

The photo tripwire this generalizes alerted via Signal (CallMeBot) + email and nagged until
`accept`. Cairn keeps that model: alert on newly-detected change, per-corpus routing, best-effort
channels, credentials from env. The scanner is the single writer and runs in an async context, so
network sends must not block or break it.

## Decisions

### D1 — Alert on NEW detections, batched per scan, post-commit
`scan_corpus` already creates events. It collects the events it newly created this run into the
`RunSummary` (counts + a capped list of relpaths by kind). After the DB commit, if there are new
**alarming** events, it calls `dispatch` once per corpus with a single batched `Alert` ("N missing,
M modified" + sample paths). Alarming = `missing` (any mode) or `modified` (WORM only). `added` is
informational (not alarming); churn `modified` is a silent re-baseline (no event, so never
alarms). Alerting on *new* detections — not the unacknowledged backlog — avoids re-nagging every
scan; the panel's nag-until-accept handles the persistent reminder.

### D2 — Best-effort, isolated sends
`dispatch` wraps every channel send in try/except, logs failures, and returns a per-channel
result; it never raises into the scanner. Sends run via `asyncio.to_thread` (smtplib) or async
httpx so the event loop isn't blocked. A misconfigured/unreachable channel degrades to a logged
warning, not a failed scan.

### D2b — Notifier protocol + registry
`Notifier` = `name: str` + `async send(alert: Alert) -> None`. A `build_channels(corpus, settings)`
factory reads `corpus.alert_json` for which channels are enabled and their per-corpus params
(e.g. email `to`), merges global credentials from `settings`, and returns the enabled notifier
instances. Unknown/disabled channels are skipped.

### D3 — SMTP is the implemented transport; Resend/SES recognized but stubbed
The settings UI offers Local SMTP / Resend / AWS SES. SMTP is fully implemented (host/port,
optional STARTTLS, optional auth, `From`). If `email_provider` is `resend`/`ses`, `smtp.py` raises
a clear `NotifierError("<provider> transport not yet wired; use Local SMTP")` so selecting it fails
loudly rather than silently dropping alerts. This keeps scope tight while honoring the UI.

### D4 — `alert_json` shape
```json
{"email": {"enabled": true, "to": ["alerts@example.com"]},
 "webhook": {"enabled": false, "url": "..."},
 "ntfy": {"enabled": false, "topic": "...", "server": "https://ntfy.sh"},
 "signal": {"enabled": false, "phone": "...", "apikey_ref": "..."},
 "kuma": {"enabled": false, "push_url": "..."}}
```
The corpus form persists at least `email.enabled` + `email.to`. Secrets (api keys, passwords) are
referenced from env/secret config, not stored in `alert_json` in cleartext where avoidable.

### D5 — Reusable for future triggers
`Alert` + `dispatch` are generic (subject/body/severity), so a later change can reuse them to
alert on `/healthz` degraded or stale-incomplete proofs without touching the channels.

## Risks / Trade-offs

- **Alert storms**: a corpus that loses many files in one scan produces one batched alert (capped
  path list), not N emails — by design.
- **Missed alert on transient channel outage**: best-effort means a down SMTP server drops that
  alert (logged). The event persists unacknowledged in the panel, so the signal isn't lost — the
  notification is. Acceptable; a retry queue is a later refinement.
- **Secrets**: channel credentials live in env/secret config; `alert_json` holds routing
  (recipients, URLs), avoiding cleartext secrets in the DB.
