## Context

DESIGN is web-panel-first; the handoff (`docs/design/`) is a high-fidelity React prototype that we
must reproduce as server-rendered Jinja2 + htmx, NOT port literally. obsidian_mcp's
`control_panel/` (base layout, routes returning `TemplateResponse`, htmx partial swaps, CSRF) is
the pattern to lift. The non-negotiable is scale: a corpus can hold 186k files, so the file list
is always server-paginated and searched.

## Decisions

### D1 — Tokens as CSS custom properties + component CSS (no Node/Tailwind build)
The project is Python-only and meant to run offline/self-hosted; a Node Tailwind build pipeline is
out of place. We copy the Slate `:root[data-mode=light|dark]` oklch variables from
`docs/design/cairn/theme.css` verbatim and hand-author a component stylesheet (`panel.css`)
implementing the handoff components (card, pill, StatusBadge, OtsBadge, dot/pulse, button
variants, field/input/select/textarea, toggle, segmented control, SegBar, page header). This is a
deliberate deviation from the handoff's "Tailwind" wording; the rendered pixels match the locked
tokens (radii, spacing, weights, the semantic color mapping). Fonts (Hanken Grotesk, JetBrains
Mono) and htmx are self-hosted under `static/` for offline use.

### D2 — Mode (light/dark) via cookie + `data-mode`
A tiny `GET /panel/mode/toggle` (or `hx-post`) flips a `cairn_mode` cookie and returns a redirect/
no-op; `base.html` reads the cookie to set `<html data-mode>`. No flash: the attribute is rendered
server-side from the cookie. Body transitions per the handoff.

### D3 — Server-side search + filter + pagination (mandatory)
The corpus files table and the verify search are htmx `hx-get` with
`hx-trigger="keyup changed delay:200ms"` hitting endpoints that return a `<tr>`/list **partial**.
Queries are `LIMIT/OFFSET` (page size ~50) with a `WHERE relpath LIKE :q` (indexed prefix where
possible) and a status filter (All/Issues/New/OK; Issues = modified|missing). The footer reports
"Showing N of TOTAL" (or "N matching"). The full list is never materialized. Verify search is
scoped to `ots_state='complete'`-or-`incomplete` anchored files.

### D4 — htmx mutation endpoints return partials
- Acknowledge event → `hx-post /panel/events/{id}/ack` → returns the updated row partial; the
  sidebar/dashboard counts are refreshed via `hx-swap-oob` (out-of-band) fragments.
- Accept changes → `hx-post /panel/corpus/{id}/accept` → re-renders the stat row + table partial.
- Scan now → `hx-post /panel/corpus/{id}/scan` (or dashboard `/panel/scan`) → runs a scan, returns
  refreshed status. (Synchronous for small corpora; a progress affordance is acceptable.)
- Root validation → `hx-get /panel/corpus/validate-root?path=...` → returns the ok/✗ indicator and
  toggles the submit button (also re-validated on the server at create/save).
All mutating endpoints require a CSRF token (lifted from obsidian_mcp's `csrf.py`).

### D5 — Single-user scoping now; multi-user later
The panel resolves the implicit single user and scopes all queries by `user_id`, so the same code
becomes multi-user-correct when login is added — `add-multi-user` only adds the login wall, the
admin Users & mounts tab, and the per-request user resolution. The login template is authored now
but unused in single mode.

### D6 — Health pill bound to `/healthz`
The topbar pill polls `/healthz` (htmx `hx-get` on an interval, or a small fetch) and reflects
`ok`/`degraded`/`error`, matching the dead-man's-switch semantics from `add-scheduler`.

### D7 — CairnMark + icons
CairnMark (three stacked offset ellipses, `--accent`, decreasing opacity) is an inline SVG partial.
Line icons are a small inline-SVG set (the handoff lists the needed glyphs); reuse a single
`{% macro icon(name) %}` partial.

## Risks / Trade-offs

- **No Tailwind**: future contributors expecting Tailwind utility classes won't find them; the
  component CSS is documented and token-driven instead. Trade-off accepted for a Node-free stack.
- **Synchronous "scan now"**: a huge corpus scan would block the request. For the panel we scan
  small/affected sets or surface "scan queued"; the scheduler remains the workhorse. Documented.
- **oklch support**: modern browsers only (the handoff chose oklch deliberately); acceptable for a
  self-hosted admin panel.
