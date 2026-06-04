# Add a collection issue-review page with recovery guidance

## Why
Cairn's single most important job is to tell the operator that something happened to their files
and let them act on it. Today that path is weak:

- A dashboard collection card *is* a link to the collection detail page, but the "1 missing" legend
  doesn't **look** clickable, so the operator doesn't realize there's anywhere to go.
- The detail page drops you into a tree/list browser over (for Photos) 186k files — a file
  explorer, not a "here is what changed, review it" view. There is no focused home for the
  review-and-acknowledge workflow.
- When a file is **missing**, Cairn says nothing about **recovery**. The operator has to work out
  which file, where it lived, and pull it from a backup entirely by hand.

This change gives the issue-review workflow a real home and turns "a file is gone" into an
actionable next step — without coupling Cairn to any one backup tool (so the repo stays
open-sourceable).

## What Changes
- **Make the issue count clickable.** On the dashboard collection card legend and the
  collection-detail "Changed / missing" stat tile, the issue counts become a clear link/button
  into the new review page (cursor, hover, a "Review →" affordance). The card still links to the
  detail page; the issue count deep-links to review.
- **New review page** `GET /collection/{collection_id}/review` (template `collection_review.html`):
  a focused list of every `missing` file and every WORM-`modified` file. Each row shows:
  - **what happened** — a Missing / Modified badge plus a human story (Missing: "last seen
    {last_checked}, detected gone {event.detected_at}"; Modified: "content changed {last_changed}");
  - **last-seen / first-seen**, size, and whether the file **was notarized** (so the operator knows
    a proof of prior existence survives even though the bytes are gone);
  - a per-file **Acknowledge** action (reusing `POST /events/{id}/ack`);
  - **bulk Accept** (reusing `POST /collection/{id}/accept`) and **Acknowledge all** (reusing
    `POST /events/ack-all`), with the existing htmx out-of-band swap that refreshes the "need
    action" pill and the sidebar alert badge in place.
  The page reuses `query_files(status_filter="missing"/"issues")`, `_event_feed`/`_event_view`,
  `humanize_delta`, and the existing pill/badge macros — no new query primitives.
- **Recovery guidance (instructions only).** Backup-tool-agnostic and public-repo-safe:
  - a **"Copy file list"** button that copies the newline-joined affected paths (offered as both
    `relpath` and absolute `root/relpath`) to the clipboard, ready to paste into whatever backup
    tool the operator uses;
  - a short, collapsible **"How to recover"** panel with tool-neutral steps (locate these paths in
    your backup, restore them under the collection root, then re-scan or Accept);
  - a note that notarized files keep their `.ots` proof of prior existence, linking to the verify /
    export affordance.

## Non-goals
- Restic or any specific backup-tool integration, and any live "find in backup" / automated
  restore. (Restic stays the documented Phase-2 follow-up in DESIGN.md.)
- Cross-collection review (the page is scoped to one collection's issues).
- New persistence — the page is a read + reuse of existing acknowledge/accept routes.

## Impact
- **Affected specs:** `web-panel` (new "Review and recover changed or missing files" requirement).
- **Affected code:** `src/control_panel/routes.py` (new review route + clickable affordance data),
  `src/control_panel/templates/collection_review.html` (new),
  `partials/_collection_card.html` + `collection_detail.html` (clickable issue count),
  `src/control_panel/static/css/panel.css` (review-page styles), `tests/test_panel.py`.
- **Dependency:** builds on `rename-corpus-to-collection` (uses `/collection` routes and the
  `collection_*` templates/services).
