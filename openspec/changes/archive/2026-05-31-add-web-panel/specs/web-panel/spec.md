## ADDED Requirements

### Requirement: Server-rendered panel in the locked Slate design with light/dark mode

The system SHALL serve a control panel rendered server-side (Jinja2) styled with the locked Slate
design tokens, supporting both light and dark mode selectable by the user and persisted across
requests. The panel SHALL run without a login wall in single-user mode.

#### Scenario: Pages render

- **WHEN** a user opens the dashboard, a corpus detail page, the add-corpus form, the verify page,
  or settings in single-user mode
- **THEN** each SHALL return HTTP 200 with the shell (sidebar + topbar) and the screen's content

#### Scenario: Mode toggle persists

- **WHEN** the user toggles light/dark mode
- **THEN** the choice SHALL be stored (cookie) and subsequent pages SHALL render with that
  `data-mode` without a flash

### Requirement: Dashboard shows status and acknowledges events

The dashboard SHALL show summary tiles, a per-corpus card for each corpus, and a recent-events
feed. Unacknowledged events SHALL offer an Acknowledge action that, on use, removes the
call-to-action and decrements the open-issue counts without a full page reload.

#### Scenario: Acknowledge an event

- **WHEN** the user clicks Acknowledge on an unacknowledged event
- **THEN** the event SHALL be marked acknowledged, its row SHALL update in place, and the sidebar
  alert badge / "need action" count SHALL decrease

### Requirement: Corpus file list is searched, filtered, and paginated server-side

The corpus detail page SHALL list files via server-side search, status filtering, and pagination —
it SHALL NOT render the entire file set (corpora can hold ~186k files). Search SHALL match the
relative path; the filter SHALL offer All / Issues / New / OK; the footer SHALL report how many of
the total are shown. A tripwire (`ots_mode='none'`) corpus SHALL hide the notarization column.

#### Scenario: Only a page of results is returned

- **WHEN** a corpus has more files than one page and the user loads or searches the file list
- **THEN** the response SHALL contain at most one page of rows plus a "showing N of TOTAL"
  indicator, never the full list

#### Scenario: Filter to issues

- **WHEN** the user selects the Issues filter
- **THEN** only files with status `modified` or `missing` SHALL be listed

### Requirement: Accept and scan actions are available from the panel

The corpus detail page SHALL offer "Scan now" and (when there are issues) "Accept changes"
actions that mutate via the existing services and refresh the affected view without a full reload.

#### Scenario: Accept changes from the panel

- **WHEN** the user clicks Accept changes on a corpus with modified/new/missing files
- **THEN** the corpus SHALL be re-baselined (new/modified → ok, missing removed, events
  acknowledged) and the stat row + table SHALL refresh

### Requirement: Add/edit corpus validates the root path

The add/edit-corpus form SHALL validate the entered root path as the user types (server-side
htmx), indicating acceptance when the path is allowed and rejecting it with a clear message
otherwise, and SHALL keep the submit action disabled until the name and a valid root are present.
The server SHALL re-validate the root on submit.

#### Scenario: Out-of-bounds or missing root is rejected

- **WHEN** the user enters a root path that does not resolve to an allowed existing directory
- **THEN** the form SHALL show a rejection indicator and SHALL NOT allow submission

#### Scenario: Valid root accepted

- **WHEN** the user enters a name and a root that resolves to an allowed existing directory
- **THEN** the form SHALL indicate acceptance and submission SHALL create/update the corpus

### Requirement: Verify a tracked file's proof from the panel without upload

The verify page SHALL let the user search files Cairn already tracks (no file upload) and verify a
selected file by re-hashing it from the read-only store and checking the stored `.ots` proof. The
result SHALL present the verdict and, when complete, the SHA-256, the existed-by date, and the
Bitcoin block, plus an option to export the portable bundle. A complete "Anchored" badge elsewhere
SHALL deep-link here and verify immediately.

#### Scenario: Verify renders a verdict

- **WHEN** the user selects an anchored file on the verify page
- **THEN** the panel SHALL run verification server-side and render the verdict (and, when complete,
  the block and existed-by date) without uploading the file
