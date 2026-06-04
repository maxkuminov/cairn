# web-panel Specification (delta)

## ADDED Requirements

### Requirement: Review and recover changed or missing files

The panel SHALL provide a per-collection **review** view, reachable directly from the dashboard,
that focuses the operator on exactly the files that need attention and tells them what to do next.
The dashboard collection card's issue count (and the collection-detail "changed / missing" stat)
SHALL be a visible, clickable link to that collection's review view. The review view SHALL list
each `missing` file and each WORM `modified` file for the collection, and for each SHALL show what
happened (missing vs modified) with its last-seen / detected time, its size, and whether the file
was notarized. The review view SHALL let the operator acknowledge an individual file's event,
acknowledge all open events, and accept (re-baseline) the collection, reusing the existing
acknowledge/accept behavior and refreshing the "need action" count and sidebar alert badge in place
without a full page reload. The review view SHALL provide **recovery guidance that assumes no
particular backup tool**: a copyable list of the affected file paths and tool-neutral recovery
instructions; for files that were notarized it SHALL note that their OpenTimestamps proof of prior
existence survives. All review and recovery actions SHALL be scoped to the current user's own
collections.

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with a missing/modified indicator, its last-seen or
  detected time, its size, and whether it was notarized

#### Scenario: Acknowledge a file from the review view

- **WHEN** the user acknowledges a file's event from the review view
- **THEN** the event SHALL be marked acknowledged and the "need action" count and sidebar alert
  badge SHALL refresh in place without a full page reload

#### Scenario: Bulk accept and acknowledge-all from the review view

- **WHEN** the user triggers Accept or Acknowledge-all from the review view
- **THEN** the action SHALL reuse the existing accept/acknowledge behavior scoped to the user's own
  collections, and the view SHALL refresh to reflect the cleared issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  survives

#### Scenario: Nothing to review

- **WHEN** the user opens the review view for a collection with no missing or modified files
- **THEN** the view SHALL render an "all clear" empty state and SHALL offer no acknowledge/accept
  actions
