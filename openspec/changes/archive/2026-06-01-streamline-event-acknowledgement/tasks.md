## 1. Auto-acknowledge informational events (scanner)

- [x] 1.1 In `src/services/scanner.py`, set `acknowledged_at=now` and `acknowledged_by=None` on the
  `added` `Event` created in `_drain` (the `added_buffer` flush, ~line 142).
- [x] 1.2 In `src/services/scanner.py`, set `acknowledged_at=now` and `acknowledged_by=None` on the
  `restored` `Event` created in the reappeared-file branch (~line 189).
- [x] 1.3 Confirm no change is needed to `missing`/`modified` event creation (they stay
  unacknowledged) and that `accept_corpus` still acks remaining unacknowledged events unchanged.

## 2. Backfill migration

- [x] 2.1 Add Alembic revision `0004_ack_informational_events.py` (down_revision `0003_app_settings`)
  with a data-only `upgrade()` running `UPDATE events SET acknowledged_at = detected_at WHERE
  kind IN ('added','restored') AND acknowledged_at IS NULL`; `downgrade()` is a no-op.
- [x] 2.2 Run `alembic upgrade head` against a scratch DB and confirm it applies cleanly and is
  idempotent (re-running touches 0 rows).

## 3. Bulk "Acknowledge all" route

- [x] 3.1 In `src/control_panel/routes.py`, add `POST /events/ack-all` (CSRF-protected,
  `current_user` dependency) that updates every `Event` with `acknowledged_at IS NULL` whose
  `corpus_id` is in the user's corpora, setting `acknowledged_at=now`, `acknowledged_by=user.id`.
- [x] 3.2 Factor the recent-events feed render (last 20 events + `open_events` + missing/alert
  count) so both `dashboard` and the ack-all route reuse it; return a feed partial plus OOB swaps
  for the "need action" pill (`#open-events-pill`) and sidebar badge (`#sidebar-alert-badge`).
- [x] 3.3 Verify scoping: an event id outside the user's corpora is never matched (multi-user
  isolation).

## 4. Dashboard UI

- [x] 4.1 Give the recent-events feed container a stable id (e.g. `#events-feed`) in
  `src/control_panel/templates/dashboard.html` and add the "Acknowledge all" button in the rail
  header, shown only when `open_events > 0`, posting to `/events/ack-all` via htmx targeting the
  feed.
- [x] 4.2 Add the new feed partial (e.g. `partials/events_feed.html`) rendering the `_event_row`
  loop + empty state + OOB pill/badge swaps; reuse it from the ack-all response.
- [x] 4.3 Confirm `partials/_event_row.html` already hides the per-event Acknowledge button when
  `e.acked` (so auto-acked `added`/`restored` rows show no button) — no change expected.

## 5. Tests & verification

- [x] 5.1 Extend `tests/test_scanner.py`: a scan that adds a new file (and the missing→restored
  path) writes the event with `acknowledged_at` set and `acknowledged_by` NULL, and the corpus has
  zero unacknowledged events; `missing`/worm-`modified` remain unacknowledged.
  (`test_informational_events_autoacked`; `test_accept_*` count updated.)
- [x] 5.2 Extend `tests/test_panel.py`: `POST /events/ack-all` acks all of the user's open events,
  returns the refreshed feed with zeroed counts, and (multi-user) leaves another user's events
  untouched; the button is absent when no events are open. (4 new tests, incl. CSRF + scoping.)
- [x] 5.3 Run the suite (`pytest` → 93 passed, 2 skipped) and `openspec validate
  streamline-event-acknowledgement --strict` (valid). Dashboard render + ack-all partial + perfile
  auto-ack are exercised by the panel/scanner tests; live-deploy smoke pending `make deploy`.
