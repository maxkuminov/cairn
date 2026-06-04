## Context

`events` rows drive two things: the dashboard's recent-activity feed and the "nag-until-accept"
lifecycle (DESIGN.md §5). Four kinds exist — `added`, `modified`, `missing`, `restored`. Only two
are genuine nags: `missing` (both modes) and `modified` in `worm` mode. The scanner already
encodes this asymmetry — `_record_alarm` fires only for `missing`/worm-`modified`, alert dispatch
and the issues tile count only those, and churn `modified` silently re-baselines. But the dashboard
derives "needs action" from `acknowledged_at IS NULL` across *all* kinds, so every routine `added`
event (and `restored`) shows up as "N need action" with an Acknowledge button. Adding files is the
normal case for a notary; the panel nags for it.

Acknowledgement today is per-event only (`POST /events/{id}/ack`, sets `acknowledged_at`/`by`
without touching file status). The heavier `accept` flow re-baselines files *and* acks events, but
that is a corpus-level destructive re-baseline, not a "clear the feed" gesture.

Constraints: SQLite single-writer; multi-user isolation (a user must never ack another user's
events, DESIGN.md §4); htmx in-place updates (no full reload); no `events` schema change desired.

## Goals / Non-Goals

**Goals**
- `added`/`restored` events stop nagging while staying visible in the feed and audit trail.
- One click clears all of the current user's open events and refreshes the feed + counts in place.
- Existing routine backlog clears on upgrade.

**Non-Goals**
- No `events` schema change (no new columns/kinds).
- No change to alerting, the issues tile, or `accept`/re-baseline.
- No auto-acceptance of *files* — file `status` lifecycle is untouched; only *event*
  acknowledgement is automated, and only for informational kinds.

## Decisions

### D1 — Auto-acknowledge `added` and `restored` at creation (system ack)

When the scanner creates an `added` or `restored` `Event`, it sets `acknowledged_at = detected_at`
and `acknowledged_by = NULL`. The event is born acknowledged: still recorded (audit trail), still
rendered in the recent-events feed labelled "New"/"Restored", but it does not count toward
`open_events`/"need action" and renders no Acknowledge button.

- **Why NULL `acknowledged_by`**: it already means "no user" (the column is nullable and `SET NULL`
  on user delete). Reusing it as the "system/automatic" sentinel needs no schema change. The feed
  already keys the button purely off `acked`, so a NULL actor is invisible to the UI.
- **Alternatives rejected**:
  - *Suppress the event entirely* — loses the first-seen / restored audit record and the
    recent-activity visibility the feed is for.
  - *Filter `added`/`restored` out of the open-event query but leave them unacknowledged* — leaves
    rows in a permanent "unacknowledged" state that `accept` and any future report would still
    sweep up; muddies the meaning of `acknowledged_at`. Setting it at creation keeps the column
    honest: unacknowledged ⇔ needs action.

The nag set therefore narrows to exactly `missing` (both modes) + worm `modified` — the same set
that already alarms.

### D2 — `POST /events/ack-all` for bulk acknowledgement

A new CSRF-protected route marks every unacknowledged event whose corpus belongs to the current
user acknowledged (`acknowledged_at = now`, `acknowledged_by = user.id`), in one `UPDATE` scoped by
`corpus_id IN (user's corpora)`. It then re-renders the recent-events feed plus OOB swaps for the
"need action" pill and the sidebar alert badge — mirroring the single-event ack's refresh.

- Lightweight ack semantics only (no file re-baseline) — same as the per-event button, just
  fanned out. `accept` remains the way to re-baseline files.
- **Alternatives rejected**: reusing `accept` (too heavy — deletes missing rows, flips file
  status); a client-side loop of per-event acks (N requests, racy, no atomic count update).
- Scoping by the user's corpus ids enforces DESIGN.md §4 isolation; an event id outside the user's
  set is simply not matched.

### D3 — Data-only backfill migration

A new Alembic revision runs `UPDATE events SET acknowledged_at = detected_at WHERE kind IN
('added','restored') AND acknowledged_at IS NULL`. No schema change. This clears the current
routine backlog precisely — it never touches `missing`/`modified`, so live nags survive the
upgrade. Downgrade is a no-op (the pre-state — which routine events were unacknowledged — is not
recoverable, and re-nagging on downgrade has no value).

## Risks / Trade-offs

- **An auto-acked `restored` could mask a suspicious re-appearance** → `restored` is already
  non-alarming (no alert, benign missing→present direction) and stays visible in the feed labelled
  "Restored"; the at-risk signal (`missing`) still nags until the file returns or is accepted. If
  this proves wrong, auto-ack can be narrowed to `added` only with no schema impact.
- **"Acknowledge all" could ack a missing/modified nag the user hasn't read** → it is an explicit,
  opt-in, CSRF-protected click that only affects the user's own corpora; identical in effect to
  clicking each row. The events remain in the feed (acknowledged), and file status is unchanged, so
  `accept` still surfaces what is pending.
- **Backfill touches many rows on a large install** → a single indexed `UPDATE`; trivial for
  SQLite at Cairn's scale, runs once at `make migrate`.

## Migration Plan

1. Ship code (scanner auto-ack + route + templates) and the new Alembic revision together.
2. Deploy (`make deploy`), then `make migrate` (`alembic upgrade head`) — idempotent; backfills
   existing `added`/`restored` acks.
3. Verify: dashboard "N need action" reflects only `missing`/worm-`modified`; "Acknowledge all"
   clears the rest; a fresh scan of a perfile corpus adds files without raising the counter.
- **Rollback**: revert the code; the migration downgrade is a no-op (auto-acked rows stay
  acknowledged, which is harmless). No data loss either direction.

## Open Questions

- Should system-acked events carry a subtle "auto" marker in the feed? Decided **no** for now —
  keep the feed minimal; the kind label ("New"/"Restored") already conveys it is informational.
