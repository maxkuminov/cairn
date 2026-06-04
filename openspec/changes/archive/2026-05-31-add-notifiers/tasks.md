## 1. Config

- [x] 1.1 Add channel settings to `src/config.py`: `smtp_host`, `smtp_port` (587), `smtp_starttls` (True), `smtp_user`, `smtp_password`, `smtp_from`, `email_provider` (local|resend|ses, default local). Document in `.env.example`/`config.example.yaml`. Secrets via env only.

## 2. Notifier framework (`src/notify/`)

- [x] 2.1 `base.py`: `Alert` dataclass (corpus_name, summary, paths: list[str] capped, detected_at, severity); `Notifier` protocol (`name`, `async send(alert)`); `NotifierError`; a `build_channels(corpus, settings) -> list[Notifier]` factory reading `corpus.alert_json` + global config.
- [x] 2.2 `smtp.py`: compose a subject ("Cairn: <summary> in <corpus>") + plaintext body; send via `smtplib`/`aiosmtplib` (run blocking smtplib in `asyncio.to_thread`); honor STARTTLS + optional auth. If `email_provider` is resend/ses, raise `NotifierError(...not yet wired...)`.
- [x] 2.3 `webhook.py` (POST JSON), `ntfy.py` (POST to topic), `signal_callmebot.py` (GET CallMeBot), `kuma_push.py` (GET push URL). All via httpx with a short timeout; raise `NotifierError` on non-2xx.

## 3. Dispatch (`src/notify/dispatch.py`)

- [x] 3.1 `async dispatch(alert, corpus, settings) -> dict[str,bool]`: build enabled channels, send each best-effort (try/except, log, never raise), return per-channel success map.

## 4. Scanner integration

- [x] 4.1 In `scan_corpus`, collect the events newly created THIS run (kind + relpath, capped). Add the alarming summary to `RunSummary`. After the final commit, if there are new alarming events (missing any-mode, modified WORM), build an `Alert` and `await dispatch(...)`. Wrap so any dispatch error is logged and never fails the scan. `added` and churn `modified` do NOT alert.

## 5. Panel alert config

- [x] 5.1 Corpus add/edit form: persist the Email toggle + recipient into `alert_json` (`{"email":{"enabled":bool,"to":[...]}}`); read it back when editing. `corpora.create_corpus`/`update_corpus` accept an `alert` dict (or the route serializes it). Planned channels stay shown-disabled.
- [x] 5.2 Settings → Notifications reflects the configured global SMTP/provider (read-only display from config is fine).

## 6. Verification

- [x] 6.1 `tests/test_notify.py` (transports mocked — monkeypatch smtplib/httpx; no real network): SMTP message subject/body/recipient composed correctly; `build_channels` returns only enabled channels from `alert_json`; `dispatch` continues + reports failure when one channel raises; a scan that newly detects a missing file (any mode) and a WORM modification triggers dispatch, while a churn modification and an `added`-only scan do NOT; alert config round-trips through the corpus form (create→edit shows the saved email).
- [x] 6.2 `openspec validate add-notifiers --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier`; resolve drift. Update `CLAUDE.md`. Archive.
