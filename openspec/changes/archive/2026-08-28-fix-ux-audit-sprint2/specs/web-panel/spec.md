# Delta: web-panel (fix-ux-audit-sprint2)

## ADDED Requirements

### Requirement: The global search control performs a search

The top-bar search control SHALL submit its query and land the operator on the verify page with
that query applied to the tracked-file search, rendering the same bounded, owner-scoped results
the verify page's own search produces — including its empty state for a query with no matches. A
control rendered in the page chrome SHALL NOT be inert: a search box that accepts typing and does
nothing on submit is a silent failure of the most prominent control on the page, and either works
or is not rendered.

The navigation SHALL preserve the query so the verify page's search input arrives pre-filled with
it, and refining the search from there SHALL behave exactly as searching on the verify page
directly.

The control's placeholder and accessible name SHALL advertise only the search the backend
performs (file names and paths). Advertising a hash search that the backend does not perform is
the same silent failure in a different place: the operator pastes a digest and reads "no
matches" as "this digest is not tracked".

#### Scenario: The placeholder does not promise an unsupported search

- **WHEN** the top-bar search control renders
- **THEN** its placeholder and accessible name SHALL describe a file-name/path search and SHALL
  NOT advertise hash search unless a digest lookup is actually implemented

#### Scenario: Submitting a query from the top bar

- **WHEN** the operator types a query into the top-bar search box on any page and submits
- **THEN** the panel SHALL navigate to the verify page with the query applied, showing the
  tracked-file search results for that query with the query visible in the search input

#### Scenario: A query with no matches

- **WHEN** the submitted query matches no tracked file the operator owns
- **THEN** the verify page SHALL render its existing no-matches empty state, not a blank region
  and not another user's files

#### Scenario: An empty submission

- **WHEN** the operator submits the top-bar search with an empty query
- **THEN** the panel SHALL land on the verify page in its default state and SHALL NOT render an
  error

### Requirement: Every proof state's badge reaches the verify surface

The file browser's proof-state badge SHALL link to the per-file verify surface in **every** proof
state — never-stamped, queued for stamping, awaiting confirmation, and confirmed alike. The
verify surface already renders an honest, state-appropriate card for each of these; a badge that
links only in the confirmed state makes the guidance for every other state unreachable except by
hand-typed URL, which means the card's advice is invisible to exactly the operator it addresses.

The verify page's tracked-file search SHALL likewise include files in every proof state, showing
each result's proof state so an unstamped file is visibly unstamped in the list. A search scoped
to already-anchored files silently hides the files whose verify card carries actionable guidance.
The verify page's *recent proofs* listing keeps its existing population (files with a submitted
proof — awaiting confirmation or confirmed) — the state filter belongs to that list's stated
purpose, not to search. That listing's heading and copy SHALL describe it as recent **proofs**,
not as anchored files: its population includes proofs not yet confirmed, and sprint-1's
vocabulary rule forbids presenting an unconfirmed proof as anchored. A **blank or
whitespace-only query SHALL render that default recent listing**, never the widened search: the
widened population is reachable only through a non-blank query, so clearing the search input
restores exactly the page's default state.

Search results SHALL be **ordered by a unique deterministic key** — path first, then collection —
and a result set truncated by the row cap SHALL say so, naming the true total match count and
inviting a narrower query. With a recency-of-stamping order and a silent cap, an unstamped file
sharing a searchable name with enough stamped files is unreachable no matter what the operator
types — a silent cap on a search whose purpose is finding a specific file is the "silently hides
files" defect reintroduced one level down. *Accepted limitation:* a query whose matches are the
same path replicated across more collections than the cap cannot be disambiguated by narrowing
the path; the truncation notice SHALL direct the operator to the per-collection file browser
(which paginates) for that case.

Search copy SHALL match the widened population on **every render path** (full-page and
partial-refinement alike): the search heading, the searchable-file count, and the no-match copy
SHALL describe tracked files, not anchored files or proofs. Proof-oriented wording (recent
proofs, submitted proofs) is permitted only on the default recent-proofs listing, whose
population it correctly describes; anchored wording is permitted nowhere an unconfirmed proof
can appear.

#### Scenario: A never-stamped file's badge

- **WHEN** the operator clicks the proof badge of a file that has never been stamped
- **THEN** the panel SHALL open the per-file verify surface for that file, showing the
  never-notarized card with its guidance

#### Scenario: Queued and awaiting-confirmation badges

- **WHEN** the operator clicks the proof badge of a file whose proof is queued for stamping or
  awaiting Bitcoin confirmation
- **THEN** the panel SHALL open the per-file verify surface for that file rather than rendering
  an inert pill

#### Scenario: Searching for an unstamped file

- **WHEN** the operator searches the verify page for a tracked file that has never been stamped
- **THEN** the file SHALL appear in the results with its proof state visible, and selecting it
  SHALL reach its verify card

#### Scenario: A blank query does not widen the listing

- **WHEN** the verify page's search input is cleared (or submitted blank / whitespace-only)
- **THEN** the page SHALL render the default recent-proofs listing in its existing form, and
  SHALL NOT render unstamped files under it

#### Scenario: The recent listing is not captioned as anchored

- **WHEN** the recent-proofs listing contains a proof still awaiting Bitcoin confirmation
- **THEN** the listing's heading and copy SHALL NOT describe its contents as anchored

#### Scenario: A capped result set discloses its truncation

- **WHEN** a search query matches more tracked files than the result cap
- **THEN** the results SHALL be ordered by the unique path-then-collection key, SHALL state the
  true total match count and that the list is truncated, and SHALL direct the operator to
  narrow the query or use the per-collection file browser

#### Scenario: Identical paths across collections order deterministically

- **WHEN** a query's matches include the same path in several collections
- **THEN** the rows SHALL appear in a stable collection order with each row's collection named,
  so repeated searches render identically

#### Scenario: Search copy describes the widened population

- **WHEN** an operator whose only tracked files are unstamped uses the verify search (full page
  or refinement)
- **THEN** the searchable-file count, headings, and no-match copy SHALL describe tracked files —
  never a zero count of anchored files or proofs

### Requirement: The collection detail page discloses the new-file count

The collection detail page's summary statistics SHALL state the count of files that are watched
but not yet baselined, using the same vocabulary as the dashboard's new-files tile, whenever such
files exist — and SHALL render the count (as zero) rather than omit the concept when none do. The
detail page is where the operator acts on a collection; a population that is invisible there but
required to explain the page's own arithmetic (total ≠ baselined + issues) forces the operator to
discover it inside a confirmation dialog.

#### Scenario: A collection with new files

- **WHEN** the operator opens the detail page of a collection with files in the new state
- **THEN** the summary statistics SHALL include the new-file count, described as watched but not
  yet baselined, consistent with the dashboard tile's wording

#### Scenario: The counts explain the total

- **WHEN** the detail page renders its summary statistics for any collection
- **THEN** the displayed populations (baselined, new, changed/missing) SHALL account for the
  displayed total without requiring a number that appears on no surface

### Requirement: A fleet-wide action states its scope before acting

A dashboard control that acts on every collection SHALL say so in its label, and SHALL obtain a
lightweight confirmation that names the number of collections it is about to act on before
proceeding. An unqualified action label on a fleet-wide control invites the operator to expect a
scoped action; the correction is a statement of scope, not a change of behavior.

#### Scenario: The scan-all control states its scope

- **WHEN** the operator views the dashboard's scan control
- **THEN** its label SHALL state that it scans all collections

#### Scenario: Confirmation names the count

- **WHEN** the operator activates the dashboard's scan-all control
- **THEN** the panel SHALL ask for confirmation naming how many collections the dashboard
  currently shows, and SHALL proceed only on confirmation

#### Scenario: The count is a render-time statement, not a binding

- **WHEN** the operator's collection set changes between the dashboard render and the confirmed
  submission
- **THEN** the action SHALL scan the collections owned at execution time; the confirmation's
  count is explicitly the render-time snapshot (the action is read-only detection, so acting on
  a drifted fleet is harmless and re-confirmation is not required)

### Requirement: The status bar and the coverage ratio each name what they measure

The collection card's file-status bar SHALL carry an accessible label naming it as a breakdown of
file statuses and stating the counts it renders, and the notarization coverage line SHALL name
the population its denominator counts (the collection's present files). The two figures sit
adjacent and measure different things; unlabelled, a fully-green status bar over a low anchored
ratio reads as a contradiction on the product's headline claim. The labels SHALL derive from the
same counts the visuals render from, so the label and the picture cannot drift apart.

#### Scenario: The segbar is labelled

- **WHEN** a collection card renders its file-status bar
- **THEN** the bar SHALL expose a label (accessible name and hover text) identifying it as file
  status and stating the ok/new/modified/missing counts it depicts

#### Scenario: The anchored ratio names its denominator

- **WHEN** a collection card renders a notarization coverage ratio
- **THEN** the wording SHALL state that the denominator counts the collection's present files, so
  the deliberate exclusion of missing files from the denominator is stated rather than implied

#### Scenario: The completeness claim also names its population

- **WHEN** a collection's stampable files are all confirmed while the collection also has
  missing files (so the confirmed count is below the total file count)
- **THEN** the completeness wording SHALL name the population it covers ("all N present files
  anchored" rather than a bare "all confirmed"), so a count that visibly disagrees with the
  card's total is explained on the card itself

#### Scenario: Labels agree with the visual

- **WHEN** the file-status bar renders segments for a collection's counts
- **THEN** the label's counts SHALL be the same values the segments are sized from

#### Scenario: A nonzero status is never rendered invisible

- **WHEN** a nonzero status count's proportional segment width would round to zero (one modified
  file in a two-hundred-thousand-file collection)
- **THEN** the bar SHALL still render a visible marker for that status (a minimum segment
  width), so the visual can never contradict its own label by showing a fully-green bar over a
  stated issue
