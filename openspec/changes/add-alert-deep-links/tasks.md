# Tasks — put a one-click review link in every alert

## 1. URL validation & link building (one place)
- [ ] 1.1 New `src/services/panel_url.py` (or an equivalent single module) with
  `normalize_public_url(value) -> str | None` implementing the canonical grammar: scheme
  `http`/`https`, non-empty host, optional port, optional path prefix; **reject** userinfo, query,
  fragment, and any ASCII control character or whitespace; strip trailing slashes. The result MUST
  be pure ASCII — IDNA-encode a non-ASCII host, percent-encode a non-ASCII path, or reject (a
  non-ASCII URL raises when encoded into ntfy's `Click` header, which would kill that channel).
  Returns `None` for empty/`None`. Raises `ValueError` with a human-readable reason for an invalid
  non-empty value.
- [ ] 1.2 `panel_link(public_url, path) -> str | None` — `None` when `public_url` is falsy, else the
  base joined to `path` with exactly one slash. The **only** place links are built.
- [ ] 1.3 `src/config.py`: add `public_url: str | None = None`. Its validator is **fail-soft** —
  invalid input is coerced to `None` with a single `logging.getLogger("cairn.config").warning(...)`
  naming `CAIRN_PUBLIC_URL` and the reason. It MUST NOT raise: `get_settings()` builds the whole
  cached model, so raising here would stop startup, scanning, and every alert.

## 2. App-settings overlay
- [ ] 2.1 `src/services/app_settings.py`: overlay `public_url` alongside the SMTP fields (keep
  `SMTP_FIELDS` meaning what it says — add e.g. `OVERLAY_FIELDS = SMTP_FIELDS + ("public_url",)`).
- [ ] 2.2 **Validate on read.** `model_copy(update=...)` does not re-run validators, so
  `get_overrides()` MUST pass a stored `public_url` through `normalize_public_url` and **drop the
  key** (log a warning) if it fails — the env value then applies as if no row existed. An
  unvalidated stored value must never reach an alert.
- [ ] 2.3 `save_public_url(session, url)`: normalize (raising `ValueError` for the route to catch)
  and write; an empty value **deletes** the row (`session.delete`), it does not store `""` —
  storing empty would shadow the env value, unlike the SMTP fields where empty-means-cleared is the
  intended semantic.

## 3. Alert payload
- [ ] 3.1 `src/notify/base.py`: add `url: str | None = None` to `Alert` (default keeps both existing
  construction sites valid).

## 4. Channels — link-free output must stay byte-identical
- [ ] 4.1 `src/notify/smtp.py`: when `alert.url` is set, plaintext gains a
  `Review and acknowledge: <url>` line and the message adds an HTML alternative
  (`msg.add_alternative(..., subtype="html")`) with a "Review in Cairn" anchor. When it is **not**
  set, keep today's wording and emit a **single `text/plain` part** — no `multipart/alternative`.
  HTML-escape the collection name, summary, and every path; attribute-escape the URL in the `href`.
- [ ] 4.2 `src/notify/webhook.py`: include `"url"` **only when set** — omit the key entirely
  otherwise (never `"url": null`; a strict consumer may reject it, and a rejected webhook is a
  silently missed alert).
- [ ] 4.3 `src/notify/ntfy.py`: set the `Click` header only when a link exists, and only if the URL
  is header-safe (no control characters — the grammar already excludes them; assert, don't trust);
  append the link to the body.
- [ ] 4.4 `src/notify/signal_callmebot.py`: append the link to the message text when present.
- [ ] 4.5 `src/notify/kuma_push.py`: its `msg` query parameter already carries human-readable text
  (`"{summary} in {collection_name}"`), so append the link there when present. (The earlier "no
  message body" premise was wrong.)

## 5. Dispatch sites
- [ ] 5.1 `src/services/scanner.py` (~line 548): build the link in **its own** `try/except` that
  yields `url = None` on any failure, so a link error can never skip `Alert(...)`/`dispatch(...)`.
  Note the enclosing best-effort block already guards the scan; this inner guard guards the *alert*.
- [ ] 5.2 `src/control_panel/routes.py` `settings_smtp_test`: pass a link to `/collections` so the
  test email exercises the configured address before a real incident depends on it.

## 6. Panel
- [ ] 6.1 `settings.html`: admin-only "Panel address" field in its own card above the email channel
  card (Notifications tab), posting to `POST /settings/panel-url`, with a hint that this is the
  address alert links point at. Non-admins see it read-only in the existing `config-preview` style.
- [ ] 6.2 `routes.py`: `settings_page` passes the effective `public_url` into the context and derives
  `healthz_url` from it, falling back to `https://cairn.example.com/healthz` **labelled as an
  example** when unset. Add `POST /settings/panel-url`: CSRF-protected, `_require_admin`, catches
  `ValueError` from `save_public_url` and redirects back with the existing `?saved=`/`?msg=` flash
  pattern.

## 7. Docs
- [ ] 7.1 Document `CAIRN_PUBLIC_URL` in `config.example.yaml`, `docker-compose.example.yml`, and
  `DEPLOYMENT.md` — it must be the address a *human* reaches (the reverse-proxy URL), and it is
  optional.
- [ ] 7.2 Add a line to the alerting note in `CLAUDE.md`, including the owner-scoping limitation.
- [ ] 7.3 Collection alert-routing UI: a hint that the review link is actionable only by the
  collection's owner (relevant in `multi` mode; harmless in `single`).

## 8. Tests
### Validation & config
- [ ] 8.1 `normalize_public_url`: accepts http/https with host, port, and path prefix; strips
  trailing slashes; **rejects** bare host, path-only, `javascript:`, empty host, userinfo, query,
  fragment, embedded `"`, newline, and other control characters.
- [ ] 8.2 A malformed `CAIRN_PUBLIC_URL` **does not raise** — `Settings` builds with `public_url is
  None` (the startup-safety guarantee).
- [ ] 8.3 `panel_link` joins with exactly one slash and preserves a path prefix.
### Overlay
- [ ] 8.4 `effective_settings` overlays a stored `public_url` over env (DB wins); falls back when the
  row is absent; **ignores an invalid stored row** so the env value survives; a deleted row exposes
  env again.
### Channels — linked and link-free contracts for each
- [ ] 8.5 SMTP with a link: plaintext contains the absolute URL; the message is
  `multipart/alternative`; the HTML part's anchor `href` matches.
- [ ] 8.6 SMTP without a link: message is a **single `text/plain` part** (not multipart) and matches
  the previous wording.
- [ ] 8.7 SMTP escaping: a path containing `<img src=x onerror=alert(1)>.txt` and a collection name
  containing `&`/`<` render as literal text in the HTML part; the plaintext part is unaffected.
- [ ] 8.8 Webhook: payload contains `url` when linked, and the key is **absent** (not null) when not.
- [ ] 8.9 ntfy: `Click` header set when linked, header **absent** when not.
- [ ] 8.10 Signal: link appended when present, text unchanged when absent.
- [ ] 8.10a Kuma: `msg` param includes the link when present, unchanged when absent.
- [ ] 8.10b A non-ASCII `public_url` is either ASCII-normalized or rejected, and every channel that
  puts the URL in a header can encode what it is handed (regression for the ntfy-only operator who
  would otherwise silently receive nothing).
### Dispatch
- [ ] 8.11 A scan that triggers an alert dispatches an `Alert` whose `url` is
  `{public_url}/collection/{id}/review` (stubbed dispatch).
- [ ] 8.12 **Failure injection:** make the link builder raise; assert `dispatch` is still called
  once, with `alert.url is None`, and the run still records its result.
### Panel
- [ ] 8.13 `POST /settings/panel-url` rejects an invalid URL without changing the stored value and
  shows an error; a valid save normalizes; an empty save deletes the row; a non-admin gets 403.
