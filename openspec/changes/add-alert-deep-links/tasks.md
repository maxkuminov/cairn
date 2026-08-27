# Tasks — put a one-click review link in every alert

## 1. Configuration
- [ ] 1.1 `src/config.py`: add `public_url: str | None = None` to `Settings`, with a validator that
  accepts only an absolute `http`/`https` URL with a non-empty host and strips trailing slashes;
  `None`/empty stays unset. Raise a clear `ValueError` naming `CAIRN_PUBLIC_URL` otherwise.
- [ ] 1.2 Add a small helper (e.g. `Settings.panel_link(path)`) returning `None` when `public_url`
  is unset and `f"{public_url}{path}"` (exactly one joining slash) when it is set. Single place
  where links are built.

## 2. App-settings overlay
- [ ] 2.1 `src/services/app_settings.py`: add `"public_url"` to the overlaid field set (a new
  `PUBLIC_FIELDS`/extended tuple — keep `SMTP_FIELDS` meaning what it says) so
  `effective_settings()` overlays it with DB-wins precedence, and an absent/empty row falls back
  to env.
- [ ] 2.2 Add `save_public_url(session, url)` writing the normalized value (empty string clears the
  override). Validate before writing; raise on an invalid value so the route can surface it.

## 3. Alert payload
- [ ] 3.1 `src/notify/base.py`: add `url: str | None = None` to `Alert` (default keeps every
  existing construction site valid).

## 4. Channels
- [ ] 4.1 `src/notify/smtp.py`: plaintext part gains `Review and acknowledge: <url>` when
  `alert.url` is set, keeping today's "Review and acknowledge in the Cairn panel." wording when it
  is not. Add an inline-styled HTML alternative part via `msg.add_alternative(..., subtype="html")`
  with the same content and a "Review in Cairn" anchor; **HTML-escape** the collection name,
  summary, and every path. Plaintext must remain complete on its own.
- [ ] 4.2 `src/notify/webhook.py`: add `"url": alert.url` to the JSON payload.
- [ ] 4.3 `src/notify/ntfy.py`: set the `Click` header to `alert.url` when present (omit the header
  entirely when not) and append the link to the body.
- [ ] 4.4 `src/notify/signal_callmebot.py`: append the link to the message text when present.
- [ ] 4.5 `src/notify/kuma_push.py`: unchanged (heartbeat, no body) — confirm and leave alone.

## 5. Dispatch sites
- [ ] 5.1 `src/services/scanner.py` (~line 548): build the review link from the *effective* settings
  (`app_settings.effective_settings`, already fetched there) and pass it as `Alert(url=...)`. It is
  already inside the best-effort try/except — a link failure must never affect the scan.
- [ ] 5.2 `src/control_panel/routes.py` `settings_smtp_test`: pass a link to `/collections` so the
  test email exercises the configured address.

## 6. Panel
- [ ] 6.1 `settings.html`: add an admin-only "Panel address" field (its own small card above the
  email channel card, Notifications tab) posting to a new `POST /settings/panel-url`, with a hint
  explaining it is the address alert links point at. Non-admins see it read-only in the existing
  `config-preview` style.
- [ ] 6.2 `routes.py`: `settings_page` passes `public_url` (from `eff`) into the context and derives
  `healthz_url` from it, falling back to the current `https://cairn.example.com/healthz` **labelled
  as an example** when unset. Add the `POST /settings/panel-url` route: CSRF-protected,
  `_require_admin`, validates, saves, redirects back with a saved/error flash reusing the existing
  `?saved=`/`?msg=` pattern.

## 7. Docs
- [ ] 7.1 Document `CAIRN_PUBLIC_URL` in `config.example.yaml`, `docker-compose.example.yml`, and
  `DEPLOYMENT.md` (note it must be the address a human reaches, i.e. the reverse-proxy URL).
- [ ] 7.2 Add a line to the alerting note in `CLAUDE.md`.

## 8. Tests
- [ ] 8.1 `public_url` validation: absolute http/https accepted and trailing slash stripped; bare
  host / path-only / empty-host rejected.
- [ ] 8.2 `effective_settings` overlays a stored `public_url` over env (DB wins) and falls back when
  the row is absent.
- [ ] 8.3 SMTP message with a link: plaintext part contains the absolute URL, HTML part contains an
  anchor with the same href, and a path containing HTML metacharacters is escaped in the HTML part.
- [ ] 8.4 SMTP message with `url=None` contains no link and matches the previous wording.
- [ ] 8.5 A scan that triggers an alert on a collection dispatches an `Alert` whose `url` is
  `{public_url}/collection/{id}/review` (assert via a stubbed dispatch).
- [ ] 8.6 `POST /settings/panel-url` rejects an invalid URL without changing the stored value, and a
  non-admin is refused (403).
