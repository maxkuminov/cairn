# web-panel Specification (delta)

## MODIFIED Requirements

### Requirement: Add/edit corpus validates the root path

The add/edit-collection form SHALL validate the entered root path as the user types (server-side
htmx), indicating acceptance when the path is allowed and rejecting it with a clear message
otherwise, and SHALL keep the submit action disabled until the name and a valid root are present.
The server SHALL re-validate the root on submit. The form and its actions SHALL be served under the
`/collection` route prefix (e.g. `/collection/new`, `/collection/validate-root`,
`/collection/{collection_id}/edit`). Legacy `/corpus/...` URLs SHALL 308-redirect to the
corresponding `/collection/...` URL so existing bookmarks keep working.

#### Scenario: Out-of-bounds or missing root is rejected

- **WHEN** the user enters a root path that does not resolve to an allowed existing directory
- **THEN** the form SHALL show a rejection indicator and SHALL NOT allow submission

#### Scenario: Valid root accepted

- **WHEN** the user enters a name and a root that resolves to an allowed existing directory
- **THEN** the form SHALL indicate acceptance and submission SHALL create/update the collection

#### Scenario: Legacy corpus URL redirects to the collection URL

- **WHEN** a client requests an old `/corpus/{id}` (or any `/corpus/...`) URL
- **THEN** the panel SHALL respond with a 308 redirect to the equivalent `/collection/...` URL
