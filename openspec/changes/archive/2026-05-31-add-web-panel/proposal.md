## Why

Everything underneath now works headlessly (scan, stamp, upgrade, schedule, healthz), but Cairn
is "web-panel-first" (DESIGN §3): users self-serve their monitored paths, see status, acknowledge
issues, and verify proofs from a browser. This change builds that panel — the server-rendered
Jinja2 + htmx UI for the six screens in the locked "Slate" design — for single-user mode.

References: DESIGN.md §5 (web panel pages, per-screen state), the design handoff at
`docs/design/README.md` (high-fidelity spec: tokens, components, every screen) and
`docs/design/cairn/theme.css` (the token source-of-truth). Server-side search/pagination is
**mandatory** — a corpus can hold 186k files; the full list is never rendered.

## What Changes

- **Design system** (`src/control_panel/static/`): the Slate tokens from `theme.css` (light+dark
  oklch custom properties on `<html data-mode>`) plus a hand-authored component stylesheet
  matching the handoff (card, pill/StatusBadge/OtsBadge, dot/pulse, button variants, field/input,
  toggle, segmented control, SegBar). Self-hosted Hanken Grotesk + JetBrains Mono. htmx vendored.
  Theme mode toggled via a cookie + `data-mode` on `<html>`, persisted per user.
- **Shell + base layout**: `base.html` with the 248px sidebar (brand + CairnMark, primary nav with
  the missing-files alert badge, per-corpus list with status dots, "Add corpus", footer with the
  user block) and the 60px topbar (search box, health pill bound to `/healthz`, mode toggle,
  logout placeholder). Content column max-width 1240px.
- **Routes** (`src/control_panel/routes.py`, mounted on the app): page routes + htmx partial
  endpoints. Reads corpora/files/events/runs scoped to the (implicit single) user; calls the
  existing services for mutations.
- **Screens**:
  - **Dashboard** — summary tiles (files monitored, open issues, proofs anchored, last activity),
    per-corpus CorpusCards (status pill, meta, SegBar, OTS footer, whole-card link), and a Recent
    Events feed with an **Acknowledge** action (htmx `hx-post` swaps the row + decrements counts).
  - **Corpus detail** — meta strip, stat row, and a **files table** with a server-side **search**
    box (`hx-get`, `keyup changed delay:200ms`), a **filter** segmented control (All/Issues/New/OK),
    and **pagination** (footer "Showing N of TOTAL"). **Scan now** and **Accept changes** are htmx
    actions that re-render the affected partials. Tripwire corpora hide the notarization column;
    a complete OtsBadge links to Verify for that file.
  - **Add / edit corpus** — sectioned form with **live root validation** (htmx: a trailing check
    when the path resolves under the allowed base, an error + disabled submit otherwise), change
    policy (WORM/Churn), cadence, the two-option OTS radio (Per-file / Tripwire), exclude globs,
    and an Email alert toggle (webhook/telegram/signal shown disabled "Planned").
  - **Verify a proof** — a server-side search of *anchored* files (no upload; Cairn already holds
    the `.ots`); selecting one runs verification (re-hash + `ots verify -d` against the stored
    proof) and shows the verdict banner + detail (SHA-256, existed-by date, Bitcoin block,
    calendars, verified-via) with **Export proof bundle**. The "Anchored" badge anywhere deep-links
    here and verifies immediately.
  - **Settings** — Notifications tab (Email channel with provider preview Local SMTP/Resend/SES;
    the planned channels dimmed), Health monitoring (the `/healthz` URL with copy), Verification
    tab (block-explorer default vs Bitcoin node; calendar list). Users & mounts tab is admin-only
    and deferred to multi-user.

### Out of scope (deferred)

- **Login / registration / multi-user scoping / admin Users & mounts** — `add-multi-user`. The
  panel runs in single mode (no login wall); the login template is built but auth enforcement and
  per-user scoping land later. CSRF is wired on mutating endpoints now.
- **Wiring alert channels** (the toggles are UI-only here) — `add-notifiers`.
- **No Node/Tailwind build step**: per the Python-only, offline self-hosted constraint, the design
  tokens are implemented as CSS custom properties + a hand-authored component stylesheet (faithful
  to the handoff pixels) rather than a compiled Tailwind config. This is a deliberate deviation
  from the handoff's "Tailwind" wording; the visual result matches the locked tokens.

## Capabilities

### New Capabilities

- `web-panel`: the server-rendered control panel — dashboard, corpus detail, add/edit corpus,
  verify, and settings — with the locked Slate design, light/dark mode, htmx-driven mutations
  (acknowledge / accept / scan-now / verify), and mandatory server-side file search + pagination.

### Modified Capabilities

None (the panel reads/acts through existing capabilities without changing their requirements).

## Impact

- **Code**: `src/control_panel/routes.py` (new), `src/control_panel/templates/*` (base + shell +
  five screens + login + htmx partials), `src/control_panel/static/*` (css, fonts, htmx, CairnMark
  SVG), `src/main.py` (mount the panel router; CSRF/session seam), `src/services/corpora.py` (a
  root-validation helper + paginated/filtered file query helpers).
- **Database**: reads corpora/files/events/runs; writes via existing services (acknowledge event,
  accept, create/update corpus). No schema change.
- **Tests**: `tests/test_panel.py` (TestClient) — pages render 200; server-side file search +
  filter + pagination return the right rows and never the whole list; acknowledge/accept/scan-now
  htmx endpoints mutate + return partials; root-validation endpoint accepts in-base / rejects
  out-of-base; verify flow renders a result; mode toggle sets the cookie.
