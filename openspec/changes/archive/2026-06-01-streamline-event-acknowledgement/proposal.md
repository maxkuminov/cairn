## Why

The dashboard treats *every* unacknowledged event as "needs action", but only `missing` (both
modes) and worm `modified` events are real nags — `added` and `restored` are already non-alarming
everywhere else (no alert dispatch, not in the issues tile). So routine activity — a new file is
seen, stamped, and recorded as an `added` event — lights up the "N need action" badge and forces a
manual Acknowledge per file. Worse, there is no way to clear the feed in bulk: each event must be
acknowledged one at a time. Both make the panel nag for ordinary, expected operations (DESIGN.md
§5 — the events table is the "nag-until-accept lifecycle + the panel's alert feed").

## What Changes

- **Auto-acknowledge informational events at creation.** A scan that writes an `added` or
  `restored` event SHALL record it already acknowledged (system-acknowledged, `acknowledged_by`
  NULL). These events still appear in the recent-events feed for visibility but no longer count as
  "need action" or show an Acknowledge button. Only `missing` (both modes) and worm `modified`
  remain unacknowledged nags — aligning the events feed with the alert/issue semantics already in
  place.
- **Add a bulk "Acknowledge all" action to the dashboard.** A single control marks every
  remaining unacknowledged event for the current user's corpora acknowledged, then refreshes the
  feed, the "need action" pill, and the sidebar alert badge without a full page reload. Scoped to
  the user's own corpora (DESIGN.md §4 multi-user isolation). Lightweight ack semantics (sets
  `acknowledged_at`/`by` only) — it does NOT re-baseline files; that stays with `accept`.
- **Backfill existing routine noise.** A data-only migration acknowledges the already-recorded
  `added`/`restored` events that are currently unacknowledged, so the deployed dashboard clears its
  routine backlog on upgrade rather than waiting for the next per-file click.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `integrity-scanning`: `added` and `restored` events are written already acknowledged
  (informational), not as unacknowledged nags; the nag set is narrowed to `missing` + worm
  `modified`.
- `web-panel`: the dashboard gains a bulk "Acknowledge all" action that clears all of the current
  user's open events and refreshes the feed/counts in place.

## Impact

- **Code**: `src/services/scanner.py` (set `acknowledged_at`/`acknowledged_by` on `added` and
  `restored` `Event` creation); `src/control_panel/routes.py` (new `POST /events/ack-all` route +
  refreshed feed render); `src/control_panel/templates/dashboard.html`,
  `partials/_event_row.html`, `partials/event_ack.html` (+ a new feed partial) for the button and
  in-place refresh.
- **Data**: one new Alembic revision — a data-only backfill (no schema change; reuses existing
  `events.acknowledged_at`/`acknowledged_by` columns). Run `make migrate` after deploy.
- **Behaviour**: `added`/`restored` no longer inflate the open-event count. `accept` is unchanged
  (it already acks all unacknowledged events as part of re-baselining). No config, no new
  dependencies.

## Non-goals

- No change to alerting, the issues tile, or which events are "alarming" — `added`/`restored` were
  already non-alarming; this only stops them from nagging in the feed.
- No change to `accept`/re-baseline semantics or to the `events` table schema (kinds, columns).
- No new "auto-accept files" behaviour — files keep their `new`/`modified`/`missing` status and
  lifecycle; only the *event* acknowledgement is automated for informational kinds.
- No retroactive un-stamping or proof changes.
