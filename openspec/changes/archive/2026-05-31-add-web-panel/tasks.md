## 1. Design system + base shell

- [x] 1.1 `static/css/tokens.css` — copy the Slate `:root`/`[data-mode=light|dark]` oklch variables from `docs/design/cairn/theme.css` (Slate only). `static/css/panel.css` — component classes per the handoff (card, pill, status-badge, ots-badge, dot/pulse `cairnPulse`, btn variants, field/input/select/textarea, toggle, seg control, segbar, page-header, spinner `cairnSpin`). Self-host fonts under `static/fonts/` + `@font-face`; vendor `static/js/htmx.min.js`.
- [x] 1.2 `templates/base.html` — `<html data-mode>` from the `cairn_mode` cookie; load tokens/panel css + htmx; blocks for title/content. Macros: `{% macro icon(name) %}` (inline SVG set the handoff lists) and the CairnMark SVG.
- [x] 1.3 `templates/_shell.html` (or base) — 248px sidebar (brand+CairnMark, primary nav Dashboard/Corpora/Verify with the missing-files alert badge, CORPORA list with status dots + counts, dashed "Add corpus", footer user block with ADMIN chip) + 60px topbar (search box, health pill bound to `/healthz`, mode toggle button, logout placeholder). Content max-width 1240px.
- [x] 1.4 Mount the panel router in `src/main.py`; wire session + CSRF middleware (lift `csrf.py` from obsidian_mcp). `GET /panel/mode/toggle` flips the cookie.

## 2. Dashboard

- [x] 2.1 `GET /panel` (or `/`) — summary tiles (files monitored + total size, open issues colored, proofs anchored + pending, last activity), CorpusCards (status pill, root mono, meta row, SegBar of ok/new/modified/missing + legend, OTS footer, whole-card link), Recent Events feed (EventRow with kind icon/color, relpath, corpus, relative time, Acknowledge button for unacked).
- [x] 2.2 `POST /panel/events/{id}/ack` (CSRF) — acknowledge; return the updated row partial + OOB-swap the sidebar Dashboard badge and the "N need action" pill.
- [x] 2.3 `POST /panel/scan` — out-of-cadence scan (all or a corpus); refresh on completion.

## 3. Corpus detail

- [x] 3.1 `GET /panel/corpus/{id}` — back link, header with Edit/Scan-now/(Accept when issues), meta strip, stat row, files table Card.
- [x] 3.2 `GET /panel/corpus/{id}/files` — SERVER-SIDE partial: `q` search (`relpath LIKE`), `filter` (all/issues/new/ok), `page` (LIMIT/OFFSET ~50). Returns the rows partial + footer "Showing N of TOTAL" / "N matching". htmx `keyup changed delay:200ms` on the search box; segmented filter swaps too. Tripwire corpora hide the notarization column; a complete OtsBadge links to Verify.
- [x] 3.3 `POST /panel/corpus/{id}/accept` (CSRF) — `accept_corpus`; re-render stat row + table partial. `POST /panel/corpus/{id}/scan` (CSRF) — scan; refresh.

## 4. Add / edit corpus

- [x] 4.1 `GET /panel/corpus/new` + `GET /panel/corpus/{id}/edit` — sectioned form (identity+root, integrity policy WORM/Churn + cadence, OTS Per-file/Tripwire radio cards, exclusions textarea + Email toggle with Planned rows).
- [x] 4.2 `GET /panel/corpus/validate-root?path=` — htmx partial: ok check when the resolved path exists/“under base” (single mode: exists & is a dir), ✗ + message otherwise; toggles submit.
- [x] 4.3 `POST /panel/corpus` / `POST /panel/corpus/{id}` (CSRF) — create/update via `corpora` helpers (server re-validates root); redirect to detail.

## 5. Verify + Settings

- [x] 5.1 `GET /panel/verify` + `GET /panel/verify/search?q=` — server-side search of anchored files (no upload). `POST /panel/verify` (CSRF) for a selected file → re-hash + `ots.verify -d` → result partial (verdict banner, SHA-256 with copy, existed-by, Bitcoin block, calendars, verified-via) + Export bundle action. The Anchored badge deep-links here and verifies immediately.
- [x] 5.2 `GET /panel/verify/export/{file_id}` — stream/export the bundle (file + `.ots`).
- [x] 5.3 `GET /panel/settings` — Notifications tab (Email provider preview Local SMTP/Resend/SES; planned channels dimmed), Health monitoring (`/healthz` URL + copy), Verification tab (explorer default vs node; calendar list). Users & mounts tab present but admin-gated/deferred.

## 6. Verification

- [x] 6.1 `tests/test_panel.py` (TestClient, `CAIRN_SCHEDULER_ENABLED=0`, seeded corpus/files/events): each page returns 200; `/files` search+filter+pagination returns correct subset and never the full list (assert with >page_size files that only a page is returned); acknowledge → event acked + partial; accept → files ok; scan-now → run created; validate-root accepts in-dir / rejects missing; verify renders a result (ots mocked); mode toggle sets the cookie.
- [x] 6.2 Pixel/UX spot-check the rendered HTML against the handoff (tokens present, semantic colors, tripwire hides notarization column, dark mode attribute). Optionally drive with the user-representative agent after serve.
- [x] 6.3 `openspec validate add-web-panel --strict`, `.venv/bin/ruff check src tests`, `PYTHONPATH=. .venv/bin/pytest -q`. Spawn the `openspec-verifier`; resolve drift. Update `CLAUDE.md`. Archive.
