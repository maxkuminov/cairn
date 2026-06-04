## MODIFIED Requirements

### Requirement: Dashboard shows status and acknowledges events

The dashboard SHALL show summary tiles, a per-corpus card for each corpus, and a recent-events
feed. Unacknowledged events SHALL offer an Acknowledge action that, on use, removes the
call-to-action and decrements the open-issue counts without a full page reload.

The dashboard SHALL additionally offer a bulk "Acknowledge all" action, shown only while at least
one open event exists. Using it SHALL mark every unacknowledged event belonging to the current
user's corpora acknowledged (recording who and when) and SHALL refresh the recent-events feed, the
"need action" count, and the sidebar alert badge in place without a full page reload. The bulk
action SHALL be scoped to the current user's own corpora and SHALL NOT acknowledge events of other
users' corpora. It SHALL set acknowledgement only — it SHALL NOT re-baseline files (that remains
the `accept` operation).

#### Scenario: Acknowledge an event

- **WHEN** the user clicks Acknowledge on an unacknowledged event
- **THEN** the event SHALL be marked acknowledged, its row SHALL update in place, and the sidebar
  alert badge / "need action" count SHALL decrease

#### Scenario: Acknowledge all open events

- **WHEN** the user clicks "Acknowledge all" while one or more open events exist
- **THEN** every unacknowledged event in the user's corpora SHALL be marked acknowledged, the feed
  SHALL re-render with no remaining Acknowledge actions, and the "need action" count and sidebar
  alert badge SHALL drop to zero — all without a full page reload

#### Scenario: Acknowledge all is scoped to the user

- **WHEN** a user triggers "Acknowledge all" in multi-user mode
- **THEN** only events belonging to that user's corpora SHALL be acknowledged, and another user's
  unacknowledged events SHALL be left untouched

#### Scenario: Acknowledge all when nothing is open

- **WHEN** there are no unacknowledged events for the user
- **THEN** the "Acknowledge all" control SHALL NOT be shown (and the route SHALL be a no-op if
  invoked directly)
