# Tasks — collection issue-review page with recovery guidance

## 1. Review route
- [x] 1.1 Add `GET /collection/{collection_id}/review` to `src/control_panel/routes.py`, owner-scoped
  via the existing `_get_owned_collection` guard.
- [x] 1.2 Build the row data by reusing `query_files(status_filter="issues")` for missing + modified
  files and `_event_view` for the per-file event story; reuse `humanize_delta` for timestamps. Do
  not add new query primitives.

## 2. Review template
- [x] 2.1 Create `collection_review.html` and design it with the frontend-design skill within the
  existing Slate token system (`tokens.css`/`panel.css`) — hand-authored CSS + htmx, no new
  framework. Reuse the `status_badge`/pill macros.
- [x] 2.2 Render each issue row: what-happened badge + human story, last-seen/first-seen, size,
  notarized indicator, per-file Acknowledge button.
- [x] 2.3 Header controls: bulk Accept + Acknowledge-all + the "need action" pill (reuse
  `partials/_events_controls.html` semantics); empty state when the collection is all clear.

## 3. Clickable issue affordance
- [x] 3.1 In `partials/_collection_card.html`, make the issue legend (missing/modified counts) a
  link to `/collection/{id}/review` with a visible affordance (cursor/hover/"Review →").
- [x] 3.2 In `collection_detail.html`, make the "Changed / missing" stat tile deep-link to the
  review page.

## 4. Recovery guidance (instructions only)
- [x] 4.1 "Copy file list" button copying newline-joined affected paths (relpath + absolute
  `root/relpath`) to the clipboard (small inline JS using the clipboard API).
- [x] 4.2 Collapsible tool-neutral "How to recover" panel; a note for notarized files that their
  `.ots` proof of prior existence survives, linking to verify/export. No backup-tool assumption.

## 5. Wiring
- [x] 5.1 Ensure single-ack / ack-all / accept invoked from the review page refresh the review view
  and the "need action" pill + sidebar badge via the existing OOB-swap pattern.

## 6. Tests & verification
- [x] 6.1 `tests/test_panel.py`: the review page lists missing + modified files with their story.
- [x] 6.2 Acknowledging from the review page marks the event acked and refreshes the counts.
- [x] 6.3 The recovery file list contains the expected paths; empty state renders when all clear.
- [x] 6.4 `openspec validate add-issue-review-and-recovery --strict` passes.
