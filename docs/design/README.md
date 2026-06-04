# Handoff: Cairn Web Panel

## Overview

Cairn is a **self-hosted file-integrity monitor + OpenTimestamps notary** with a web panel. It watches configured file sets ("corpora") for deletion / modification / silent corruption, and (optionally, per corpus) anchors each file's SHA-256 hash to the Bitcoin blockchain via OpenTimestamps — giving a portable "this file existed, unaltered, by date X" proof. Multi-user, SQLite-backed, pluggable alerts.

This bundle is the **UI design** for that panel: dashboard, corpus detail, add/edit corpus, verify, settings, and login.

## About the Design Files

The files in this bundle are **design references built as an HTML + React prototype** — they show the intended look, layout, and behavior. They are **not** the production code.

**Production stack (from the project's `DESIGN.md`): FastAPI + uvicorn, server-rendered Jinja2 templates + htmx + Tailwind CSS.** The task is to **recreate these designs as Jinja2 templates styled with Tailwind, with htmx handling the interactive bits** (acknowledge events, run scan, accept changes, verify, live search, theme/mode toggle). Do not port the React component tree literally — translate each screen into the server-rendered idiom:

- Page navigation = real routes / full-page loads (the prototype's client-side router is just for demo).
- Buttons that mutate state (Acknowledge, Accept changes, Scan now, Verify) = htmx `hx-post` / `hx-get` returning partials.
- Live file search and the verify file-picker = htmx `hx-get` with `hx-trigger="keyup changed delay:200ms"` against a search endpoint returning a `<tr>`/list partial. **Server-side search/pagination is mandatory** — a corpus can hold 186k files; never render the full list.
- Theme tokens = CSS custom properties (see Design Tokens) set on `<html>`; Tailwind configured to read them.

## Fidelity

**High-fidelity.** Final colors, typography, spacing, radii, and interactions are all specified below. Recreate pixel-accurately using the exact tokens. The visual direction is **locked to "Slate"** (cool neutral SaaS, blue signal accent, Hanken Grotesk + JetBrains Mono). Light **and** dark modes are both required.

---

## Design Tokens

All colors are defined as CSS custom properties on `<html>`, switched by a `data-mode="light|dark"` attribute. Colors are in **oklch** — keep them as-is (modern browsers support oklch; it keeps the light/dark palettes harmonious). Wire these into `tailwind.config.js` as `theme.extend.colors` referencing the vars (e.g. `accent: 'var(--accent)'`).

### Typography
- **UI + headings:** `'Hanken Grotesk', system-ui, sans-serif`
- **Monospace** (paths, hashes, endpoints, sizes): `'JetBrains Mono', ui-monospace, monospace`
- Load from Google Fonts: `Hanken Grotesk` weights 400/500/600/700; `JetBrains Mono` 400/500/600.
- Headings: weight **600**, letter-spacing **-0.015em**.
- Base body: 14px / line-height 1.5. `-webkit-font-smoothing: antialiased`.

### Radii
- `--radius: 10px` (buttons, inputs, small cards, pills-of-controls)
- `--radius-sm: 7px` (segmented-control thumbs)
- `--radius-lg: 16px` (cards / panels)
- Pills/badges: `999px`

### Color scale — Slate / Light
```
--bg:            oklch(0.985 0.004 250)   /* app background (sidebar rail uses --bg-2) */
--bg-2:          oklch(0.965 0.005 250)   /* sidebar background */
--surface:       oklch(1 0 0)             /* cards, inputs */
--surface-2:     oklch(0.975 0.004 250)   /* inset / hover / footers */
--border:        oklch(0.915 0.006 250)
--border-strong: oklch(0.86 0.008 250)    /* input borders, dashed buttons */
--text:          oklch(0.26 0.024 256)    /* primary text */
--text-2:        oklch(0.46 0.02 256)     /* secondary */
--text-3:        oklch(0.62 0.018 256)    /* tertiary / labels / icons */
--accent:        oklch(0.55 0.16 256)     /* blue — links, primary btn, active nav */
--accent-fg:     oklch(0.99 0.01 256)     /* text on accent */
--accent-soft:   oklch(0.95 0.03 256)     /* accent tint backgrounds */
--accent-border: oklch(0.86 0.07 256)
--ok:            oklch(0.6 0.12 158)       /* green — OK / verified / healthy / anchored */
--ok-soft:       oklch(0.95 0.04 158)
--warn:          oklch(0.7 0.13 70)        /* amber — modified / pending */
--warn-soft:     oklch(0.95 0.05 75)
--danger:        oklch(0.57 0.19 25)       /* red — missing / alert */
--danger-soft:   oklch(0.95 0.04 25)
--shadow:        0 1px 2px oklch(0.5 0.02 256 / 0.06), 0 4px 16px oklch(0.5 0.02 256 / 0.05)
--shadow-lg:     0 8px 40px oklch(0.4 0.03 256 / 0.12)
```

### Color scale — Slate / Dark
```
--bg:            oklch(0.185 0.014 256)
--bg-2:          oklch(0.16 0.014 256)
--surface:       oklch(0.225 0.016 256)
--surface-2:     oklch(0.26 0.017 256)
--border:        oklch(0.31 0.016 256)
--border-strong: oklch(0.4 0.018 256)
--text:          oklch(0.95 0.006 256)
--text-2:        oklch(0.74 0.014 256)
--text-3:        oklch(0.58 0.016 256)
--accent:        oklch(0.7 0.14 256)
--accent-fg:     oklch(0.16 0.02 256)
--accent-soft:   oklch(0.3 0.06 256)
--accent-border: oklch(0.42 0.09 256)
--ok:            oklch(0.72 0.13 158)
--ok-soft:       oklch(0.32 0.06 158)
--warn:          oklch(0.78 0.13 75)
--warn-soft:     oklch(0.34 0.06 75)
--danger:        oklch(0.68 0.17 25)
--danger-soft:   oklch(0.33 0.08 25)
--shadow:        0 1px 2px oklch(0 0 0 / 0.3), 0 4px 16px oklch(0 0 0 / 0.3)
--shadow-lg:     0 8px 40px oklch(0 0 0 / 0.5)
```

**Semantic mapping (memorize this — it's used everywhere):**
| Meaning | Token | Example |
|---|---|---|
| OK / Verified / Healthy / Anchored | `--ok` / `--ok-soft` | file unchanged, proof confirmed |
| New / link / primary action | `--accent` / `--accent-soft` | newly-added file, primary buttons |
| Modified / OTS Pending | `--warn` / `--warn-soft` | file changed, proof awaiting confirmation |
| Missing / Alert | `--danger` / `--danger-soft` | file vanished |
| Not stamped / disabled | `--text-3` / `--surface-2` | tripwire-only files, planned features |

---

## Global Layout

A two-part shell:

- **Sidebar** — fixed `width: 248px`, `background: var(--bg-2)`, right border `1px var(--border)`, `position: sticky; top:0; height:100vh`, flex column.
  - **Brand** (top, padding `20px 18px 16px`): the Cairn logo mark (28–30px) + "Cairn" (Hanken Grotesk, 19px, weight 700) and an uppercase eyebrow "INTEGRITY & NOTARY" (10.5px, `--text-3`, letter-spacing 0.06em).
  - **Primary nav** (padding `8px 12px`, vertical gap 3px): items Dashboard, Corpora, Verify proof. Each item: icon (18px) + label (13.5px), padding `9px 12px`, radius `--radius`. Active = `background: var(--accent-soft); color: var(--accent); weight 600`. Hover (inactive) = `background: var(--surface-2)`. Dashboard item shows a red count badge (missing-file alerts) right-aligned: min 18px circle, `--danger` bg, white text, 11px/700.
  - **Divider**, then an uppercase "CORPORA" label, then one row per corpus: a status **dot** (7px; pulsing if status=alert), corpus name (ellipsis), and a right-aligned file count (e.g. `186k`, 11px `--text-3`). Below them an "Add corpus" button with a `1px dashed var(--border-strong)` border.
  - **Footer** (top border): a Settings nav item, then the current user block — 30px circle avatar (`--accent` bg, white initial), username (13.5px/600) with an "ADMIN" chip if admin (`--accent` on `--accent-soft`, 10px/700, radius 4px), and email (11px `--text-3`).
- **Main column** — flex column, fills remaining width.
  - **Topbar** — `height: 60px`, bottom border, `background: var(--bg)`, `position: sticky; top:0; z:20; backdrop-filter: blur(8px)`, padding `0 28px`. Left: a search box (`var(--surface-2)` fill, `--border`, radius `--radius`, padding `7px 12px`, max-width 380px, magnifier icon + placeholder "Search files, paths, hashes…"). Right cluster: a **health-status pill** (`--ok-soft` bg, dot pulsing `--ok`, "Healthy" 12px/600 `--ok`, then mono `/healthz` 11px `--text-3`; tooltip "Exposed at /healthz for external monitors"), a **mode-toggle** icon button (sun/moon, 38px square, `--surface`/`--border`), and a **logout** icon button (same style).
  - **Content** — padding `28px 32px 60px`, `max-width: 1240px`, centered (`margin: 0 auto`).

### Reusable components (build these once)

- **Card** — `background: var(--surface); border: 1px var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow)`. Default inner padding 20px. Hover variant (used on dashboard corpus cards): lift `translateY(-2px)`, `box-shadow: var(--shadow-lg)`, border → `--border-strong`, transition 0.16s.
- **Pill / Badge** — inline-flex, gap 5px, `padding: 3px 10px` (md) or `2px 8px` (sm), `border-radius: 999px`, font 12.5px/600 (sm 11.5px). Optional leading icon. Two fills: **soft** (`color: <token>; background: <token>-soft`) or **outline** (`color`, transparent bg, `1px var(--border)`).
  - `StatusBadge`: ok→checkCircle/`--ok`; modified→alert/`--warn`; missing→minusCircle/`--danger`; new→plus/`--accent`; restored→refresh/`--ok`.
  - `OtsBadge` (notarization): complete→**"Anchored"** bitcoin icon `--ok`; pending/incomplete→**"Pending"** clock `--warn`; none→**"Not stamped"** minusCircle `--text-3`.
- **Dot** — solid circle (default 8px). `pulse` variant renders an absolutely-positioned expanding clone (scale 1→2.4, opacity 0.5→0, 2s ease-out infinite).
- **Button** — radius `--radius`, weight 600, `white-space: nowrap`, transition 0.14s, optional leading/trailing icon. Sizes: sm `6px 11px`/13px, md `9px 15px`/13.5px, lg `12px 20px`/15px. Variants:
  - `primary`: `--accent` bg, `--accent-fg` text; hover brightness 0.96 + `--shadow`.
  - `default`: `--surface` bg, `1px --border-strong`, `--text`.
  - `subtle`: `--surface-2` bg, `1px --border`.
  - `ghost`: transparent, `--text-2`.
  - `danger`: `--danger` bg, white text.
- **Field** — label row (13px/600) + control + optional hint (12px `--text-3`, line-height 1.45). Required marker is a `--danger` asterisk.
- **Input / Select / textarea** — full width, `padding: 9px 12px`, radius `--radius`, `1px --border-strong`, `--surface` bg, 13.5px. Focus: border → `--accent`. Mono variant for paths/globs (12–12.5px, JetBrains Mono).
- **Toggle (switch)** — 40×23px track, radius 999; off `--border-strong`, on `--accent`; 19px white knob translateX 17px; transition 0.16s.
- **Segmented control** — wrapper `var(--surface-2)`, padding 4px, radius `--radius`; selected segment = `--surface` bg + `--shadow`, `--accent` text; unselected `--text-3`. Used for filter tabs, WORM/Churn, and email provider.
- **SegBar** — horizontal stacked proportion bar, height 7px, radius 999, `--surface-2` track, 1.5px gaps between colored segments. Used on dashboard corpus cards to show ok/new/modified/missing proportions.
- **Icons** — simple 24×24 line icons, `stroke: currentColor`, stroke-width ~1.75, round caps/joins. Use any icon set in production (e.g. Lucide/Tabler). Icons referenced: dashboard, stack, verify(shield-check), settings, bell, users, file, folder, check, checkCircle, x, alert(triangle), minusCircle, clock, link, bitcoin, upload, search, plus, chevron-right/down, arrow-left, sun, moon, mail, webhook, pulse(activity), heart, logout, lock, refresh, external, info, copy, download, calendar.
- **Logo mark ("CairnMark")** — three stacked, slightly-offset horizontal **ellipses** in `--accent` at decreasing opacity top→bottom (0.95 / 0.78 / 0.6), evoking a stone cairn. Top ellipse smallest, bottom widest. Provided as inline SVG in `icons.jsx` — reuse or have a designer finalize.

---

## Screens / Views

### 1. Login  (multi-user mode only)
- Full-viewport centered column, max-width 380px. Mode-toggle icon button pinned top-right.
- Centered brand: CairnMark (48px), "Cairn" (27px/700), subtitle "File-integrity monitor & notary" (13px `--text-3`).
- A Card (padding 26px) with Username and Password fields and a full-width **primary** "Sign in" button (lg, lock icon).
- Below the card: an info strip (`--surface-2`, radius `--radius`, info icon) — "This instance runs in **multi-user** mode. Single-user installs skip login entirely."
- Single-user installs (`CAIRN_AUTH_MODE=single`) bypass this screen entirely.

### 2. Dashboard
- **Page header**: H1 "Dashboard" (25px), subtitle "Integrity status across every corpus you monitor.", and a right-aligned **subtle** "Run scan now" button (refresh icon).
- **Summary tiles** — a row of 4 equal Cards (flex, gap 16, wrap). Each: small label row (icon 16px + 12px/600 label, `--text-3`), a big number (Hanken Grotesk, 30px/700, letter-spacing -0.02em), and a sub-line (12px `--text-3`). Tiles: **Files monitored** (total, sub "N corpora · 1.77 TiB"); **Open issues** (missing+modified count, colored `--danger` if >0 else `--ok`, sub "X missing · Y modified"); **Proofs anchored** (sum complete, sub pending count); **Last activity** (e.g. "4 min", sub which corpus scanned).
- **Main grid** — `grid-template-columns: minmax(0,1.55fr) minmax(0,1fr)`, gap 22, align-items start.
  - **Left = corpus cards.** Section label "CORPORA" (11.5px/700 uppercase `--text-3`), then one **CorpusCard** per corpus (hover-lift, padding 0, overflow hidden):
    - Header (`18px 20px 16px`): folder icon + corpus name (16.5px, ellipsis) on the left; a status Pill on the right ("All clear"/`--ok`, "Attention"/`--warn`, "Alert"/`--danger` — alert/checkCircle icon).
    - Below name: the root path in mono (11.5px `--text-3`, ellipsis, max-width ~320px).
    - A meta row (flex, gap 18, wrap): Files / Size / Owner / Last scan, each a tiny label (11px `--text-3`) over a 13.5px/600 value (`white-space: nowrap`).
    - A **SegBar** of ok/new/modified/missing counts, with a legend line under it (dots + "N missing", "N modified", "N new", or "All files verified" when clean).
    - **OTS footer** (`11px 20px`, `--surface-2` bg, top border): left — if `ots=none` show minusCircle + "Tripwire only — no notarization" (`--text-3`); else bitcoin icon `--accent` + "**N** anchored" and, if pending>0, " · N pending" in `--warn`. Right — small alert-channel icons (mail).
    - **Whole card is a link** to that corpus's detail page.
  - **Right rail = "Recent events" Card.** Header: "Recent events" + a `--danger`-soft pill "N need action" when there are unacknowledged events. Then a list of **EventRow**s (each: a 30px round tinted icon by kind, a title row of `kind` label in its semantic color + optional "stamped" pill + right-aligned relative time, the relpath in mono (ellipsis), the corpus name (11px `--text-3`), and — if unacknowledged — an **Acknowledge** button (sm; `danger` variant for missing, else `subtle`)). Acknowledging removes the call-to-action (htmx swap).

> Note: the "dead-man's switch" card was intentionally removed from the dashboard. Health is surfaced only via the topbar pill and the `/healthz` endpoint (Settings → Health monitoring).

### 3. Corpus detail
- **Page header**: a back link "← All corpora", H1 = corpus name; right-aligned buttons: **Edit** (default, settings icon), **Scan now** (subtle, refresh), and — only when there are issues — **Accept changes** (primary, check; re-baselines modified/new files to OK).
- **Meta strip** — a single Card (padding 0, overflow hidden) split into vertical cells separated by `1px --border`: Status (badge), Root path (mono), Policy (uppercase WORM/CHURN), Notarization ("Per-file · new files only" / "Tripwire only"), Scan cadence, Owner. Each cell: 11px label over 13.5px value.
- **Stat row** — `grid auto-fit minmax(150px,1fr)`, gap 14: **Total files** (+ size sub), **Verified OK** (`--ok`), **Changed / missing** (colored `--danger` if >0; sub "X missing · Y modified"), and **Anchored to chain** (`--accent`, sub pending) — or **Last scan** when the corpus is tripwire-only. Each is a small Card: label row (icon + 11.5px/600), value 23px/700 Hanken Grotesk.
- **Files table** — a Card (padding 0):
  - Header row: "Files" title, a **search input** (`--surface-2`, magnifier, placeholder "Search N files by path…", clearable ✕; `flex:1`, max-width 320px), and the **filter segmented control** (All / Issues / New / OK) pushed right.
  - Column header (grid `1fr 90px 130px 140px 110px`, 11px/700 uppercase `--text-3`): Path / Size / Status / Notarization / Last checked.
  - Rows (same grid, `12px 18px`, row hover = `--surface-2`): file icon + relpath (mono 12px, ellipsis); size; **StatusBadge** (sm); **OtsBadge** (sm) — **if state=complete the badge is a button** that navigates to Verify for this file (opacity 0.7 on hover); right-aligned relative "last checked". Tripwire corpora hide the notarization column.
  - Footer: "Showing N of TOTAL files" (or "N matching" when searching/filtered) + "Sampled · full list paginated in production".
  - **Search + filter must be server-side** (htmx) over the real file table — corpora can be enormous.

### 4. Add / Edit corpus
- Max-width 760px. Back link + H1 ("Add a corpus" / "Edit <name>") + subtitle about jailed read-only roots.
- A stack of section Cards (each opens with an uppercase SectionLabel):
  - **Identity & location**: "Corpus name" field; "Root path" field (mono) with **live validation** — a trailing checkCircle (`--ok`) when the path resolves under the user's mounted base, or an ✕ (`--danger`) + red border + "Path is outside your allowed base — rejected." when not. Hint repeats the allowed base and notes mounts are read-only.
  - **Integrity policy**: a 2-col grid — "Change policy" segmented (WORM / Churn) with hint, and "Scan cadence" select (Every 5 min / 15 min / Hourly / Nightly / Weekly) with a "stagger large corpora" hint.
  - **Notarization (OpenTimestamps)**: a radio-card group with exactly **two** options (Manifest was removed): **Per-file** (bitcoin icon) — "Each file independently anchored to Bitcoin — a portable proof you can hand off."; **Tripwire only** (shield icon) — "Detect changes, but don't notarize. Best for sets that never change, like ROMs." Selected card = `1.5px --accent` border, `--accent-soft` bg, `--accent` title + a trailing checkCircle.
  - **Exclusions & alerts**: 2-col — "Exclude globs" mono textarea (one pattern per line; hint mentions skipping caches/temp and the Obsidian vault); "Alert routing" with an **Email** toggle row (active) plus three disabled, dashed rows — **Webhook**, **Telegram**, **Signal** — each carrying a "Planned" pill.
  - Footer actions (right-aligned): **Cancel** (ghost) and **Create corpus / Save changes** (primary, disabled until name + valid root).

### 5. Verify a proof
- Max-width 820px. H1 "Verify a proof", subtitle: "Search for any file Cairn already tracks. It re-hashes the bytes in the read-only store, loads the OpenTimestamps proof it holds for that file, and checks it against the Bitcoin blockchain. Nothing is uploaded."  **There is no external file upload** — Cairn already stores the file's `.ots` proof internally.
- **Idle state** — a Card containing:
  - A prominent **search box** (`--surface-2`, magnifier 18px, placeholder "Search N anchored files by path or corpus…", clearable ✕).
  - A label that reads "RECENTLY ANCHORED" when empty, or "N matches" when searching.
  - A result list (max-height ~420px, scroll). Each row (button): file icon, filename (13px/600) over "corpus · relpath" (mono 11.5px `--text-3`), an **"Anchored"** ok-pill, and a chevron. **Empty query shows only a few recent files; results appear as you type** — server-side search, never the whole corpus.
  - Footer hint: "Or open any file from a corpus and click its **Anchored** badge to verify it directly."
- **Checking state** — a Card with a centered 44px spinner (3px ring, `--accent` top), "Verifying against the blockchain…", and a sub line "Re-hashing <file> · loading proof · checking explorer". (~1.3s in prototype; real call hits the configured block source.)
- **Result state** —
  - A verdict banner (`--ok-soft` bg, `1px --ok`): 48px round `--ok` check, "Proof verified" (19px/700 `--ok`), and "<file> existed, unaltered, by **<date UTC>**." A right-aligned ghost "Verify another" (arrow-left) resets.
  - A detail Card (rows separated by `1px --border`, grid `150px 1fr auto`): **File** (mono relpath), **Corpus**, **SHA-256** (mono, wraps, with a copy icon), **Existed by** (calendar icon, bold date), **Bitcoin block** (`#NNN,NNN` + truncated block hash, bitcoin icon), **Calendars** (the OTS calendar hostnames, mono), **Verified via** (e.g. "blockstream.info (explorer lookup)", link icon).
  - Actions: **Export proof bundle (.ots + file)** (default, download icon) and **Copy verification report** (ghost, copy icon).
  - Info strip: explorer-lookup caveat + pointer to configure your own Bitcoin node, and that export bundles file + `.ots` for third-party verification.
- **Entry from corpus**: clicking a file's "Anchored" badge deep-links here with that file preselected and runs verification immediately.

### 6. Settings
- H1 "Settings" + subtitle. A **tab bar** (underline-style: active tab has a 2px `--accent` bottom border): **Notifications**, **Verification**, and **Users & mounts** (admin only).
- **Notifications tab**:
  - SectionLabel "ALERT CHANNEL" → an **Email** Card: header (mail icon tile, "Email" + green "Verified" pill, sub "Each user routes alerts to their own provider and address.", a master Toggle on the right). Below (dims when off): a "Provider" **segmented control** — **Local SMTP / Resend / AWS SES** — and a config preview block whose rows change per provider:
    - Local SMTP → Host `smtp.localhost:25`, From `cairn@example.com`, Encryption `STARTTLS`.
    - Resend → API key `re_••••`, From `alerts@example.com`, Region `global`.
    - AWS SES → Region `us-east-1`, From `alerts@example.com`, Access key `AKIA••••`.
    - For Resend/SES, an info line: "Good for users without a local mail server — Cairn sends through <provider>."
  - SectionLabel "HEALTH MONITORING" → a Card: pulse-icon tile, "Health endpoint" + green "Healthy" pill, sub "Point Uptime Kuma or any monitor at this URL — it returns scan freshness as a dead-man's switch.", and a mono URL row `GET https://cairn.home.example.com/healthz` with a copy button. (This replaced the old push-heartbeat model: external tools **poll** Cairn now.)
  - SectionLabel "PLANNED CHANNELS" → three dashed, dimmed Cards (Webhook, Telegram, Signal) each with a "Planned" pill.
- **Verification tab**:
  - "Block source" Card — two radio-cards: **Block explorer** (selected, "Default" pill) — "blockstream.info — works out of the box. You trust the explorer's lookup."; **Your own Bitcoin node** — "Point at a node's RPC for fully trustless verification."
  - "Calendar servers" Card — list of OTS aggregator hostnames (mono) each with an `--ok` dot + "reachable".
- **Users & mounts tab** (admin): a Card with header ("Users", sub "Each user is scoped to their own read-only mounted base.", right-aligned primary "Invite user"); column header (User / Mounted base (read-only) / Corpora / Role); rows with avatar + username + email, the mounted base (mono), corpus count, and a Role badge (Admin = lock icon `--accent`; Member = neutral). Below: an info strip reiterating that watched folders are mounted **read-only** and the DB + proof store live on a separate writable volume.

---

## Interactions & Behavior

- **Navigation**: sidebar items and corpus rows route to their pages; corpus cards on the dashboard are full links; back-links return to the list. In production use real routes.
- **Acknowledge event** (dashboard): removes the row's CTA and decrements the sidebar Dashboard badge / "N need action" pill. htmx `hx-post` → returns updated row + counts.
- **Accept changes** (corpus detail): re-baselines modified/new files to OK status (the nag-until-accept lifecycle from the spec). htmx `hx-post`, re-render the table + stat row.
- **Run scan now / Scan now**: triggers an out-of-cadence scan; show progress / refresh on completion.
- **Verify**: search (debounced, server-side) → select file → "checking" state (spinner) → "result" state. Clicking an "Anchored" badge anywhere deep-links into the checking→result flow for that file.
- **Live file search** (corpus detail) and **verify search**: `hx-trigger="keyup changed delay:200ms"`, server returns the row partial. Clear (✕) resets.
- **Mode toggle**: flips `data-mode` light/dark on `<html>`; persist per user (cookie/localStorage). Body transitions background/color over 0.25s.
- **Form validation** (add corpus): root path checked against the user's mounted base on input; submit disabled until name + valid root. Server must re-validate (reject path traversal / out-of-base).
- **Hover states**: nav items tint to `--surface-2`; cards lift; table rows tint; buttons brighten 0.96.
- **Pulse animation**: status dots with `alert` status and the health pill animate (keyframes `cairnPulse`: scale 1→2.4, opacity 0.5→0, 2s infinite). Spinner: `cairnSpin` 0.8s linear.

## State / Data model (from DESIGN.md)

The panel reads from SQLite tables (the scanner is the single writer):
- `users(id, username, password_hash, is_admin, is_active, created_at, last_login_at)` — single-user mode = one implicit row.
- `corpora(id, user_id, name, root, mode[worm|churn], hash_cadence_seconds, ots_mode[none|perfile], exclude_globs_json, alert_json, created_at)` — `root` must resolve under the owner's mounted base. **Note: `ots_mode` is now just `none` | `perfile` (manifest removed from the UI).**
- `files(id, corpus_id, relpath, size, mtime, sha256, first_seen, last_checked, last_changed, status[ok|new|modified|missing], ots_path, ots_state[none|pending|incomplete|complete], ots_stamped_at)`.
- `runs(id, corpus_id, started, finished, added, modified, missing, stamped, upgraded, result)` — feeds `/healthz` freshness.
- `events(id, corpus_id, file_id, kind[added|modified|missing|restored], detected_at, acknowledged_at, acknowledged_by)` — the dashboard feed + nag-until-accept.

Per-screen state needed: current user + admin flag; selected corpus; file search query + status filter (server-side); verify flow state (idle/checking/result) + selected file; settings tab; email provider selection; theme mode (persisted).

## Assets

- **No bitmap assets.** All iconography is line SVG (use Lucide/Tabler or similar in production). The Cairn logo is three stacked ellipses (inline SVG, `cairn/icons.jsx` → `CairnMark`); have a designer finalize a real mark if desired.
- **Fonts**: Hanken Grotesk + JetBrains Mono via Google Fonts (self-host for an offline/self-hosted tool).
- All sample data (sample Photos, tax, and ROM collections, the `/healthz` URL, calendar hostnames) is illustrative — see `cairn/data.js`.

## Files in this bundle

The prototype (React + Babel, in-browser — **reference only**):
- `Cairn Panel.html` — entry; loads everything. Open this to view the design.
- `cairn/theme.css` — **the source of truth for tokens.** Contains Slate (chosen) plus two unused directions (Archive, Granite) you can ignore/delete.
- `cairn/data.js` — mock data shapes (mirror the SQLite schema above).
- `cairn/icons.jsx` — icon set + `CairnMark` logo.
- `cairn/components.jsx` — Pill, StatusBadge, OtsBadge, Dot, Button, Field, Input, Select, Toggle, Card, SegBar, PageHeader.
- `cairn/shell.jsx` — Sidebar + Topbar.
- `cairn/page-dashboard.jsx`, `cairn/page-corpus.jsx` (detail + add/edit), `cairn/page-verify.jsx`, `cairn/page-settings.jsx` (settings + login).
- `cairn/app.jsx` — router + theme/mode state + Tweaks panel (demo-only chrome; not part of the product).

Open `Cairn Panel.html` to interact with the reference. Use the toolbar **Tweaks** panel to flip light/dark and preview the login screen.
