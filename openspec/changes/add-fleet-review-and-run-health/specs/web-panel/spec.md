# web-panel Specification (delta)

## MODIFIED Requirements

### Requirement: Dashboard shows status and acknowledges events

The dashboard SHALL show summary tiles, a per-collection card for each collection, and a
recent-events feed. Unacknowledged events SHALL offer a "mark reviewed" action that, on use, removes
the call-to-action and decrements the unreviewed-event count — the count of events awaiting a
reading, not the file-derived issue counts — without a full page reload.

The vocabulary SHALL distinguish the two state machines the panel tracks. Marking an event reviewed
writes only to the reading log: the file's own state is unchanged, so the collection's status and
the file-derived counts SHALL remain as they were. Every control that performs this action SHALL be
labelled as reviewing/noting rather than resolving, and SHALL carry hint copy stating that the file
stays on record, keeps any existence proof, and that the collection keeps its alert status until the
file is restored or retired.

The dashboard SHALL additionally offer a bulk action, shown only while at least one open event
exists, that marks every unacknowledged event belonging to the current user's collections
acknowledged (recording who and when) and refreshes the recent-events feed, the "need action" count,
and the sidebar alert badge in place without a full page reload. Because it reaches events that are
not visible in the capped feed, that control SHALL state the number of events it will affect and
SHALL require an explicit confirmation before acting. It SHALL be scoped to the current user's own
collections and SHALL NOT acknowledge events of other users' collections. It SHALL set
acknowledgement only — it SHALL NOT re-baseline files (that remains the `accept` operation).

The **Open issues** tile SHALL be a working link to the place where those issues can be acted on
whenever the count is greater than zero, presented with the same clickable affordance as the
equivalent collection-page stat. When exactly one collection is affected it SHALL link to that
collection's review view; when more than one is affected it SHALL link to the **fleet-wide review
page**, which lists every affected collection's issues together. It SHALL NOT link to a route that
does not exist. At a count of zero it SHALL remain a non-interactive element.

The recent-events feed SHALL be **ordered so that the events the unreviewed count is counting are
the ones the operator sees**: unacknowledged events first, then most-recently-detected first within
each group. The feed SHALL NOT be filtered to unacknowledged events, because informational kinds are
written already acknowledged and an open-only feed would render an empty rail on a healthy system.
Ordering, not filtering, is what keeps the bulk mark-reviewed control honest: a control offering to
clear N events while the feed beneath it shows none of them asks for a blind acknowledgement of
alerts the page never displayed.

The sidebar alert badge SHALL carry an accessible label naming what it counts, and SHALL count the
same population as the Open issues tile beside it (files that are missing or modified) so the two
cannot disagree. That count SHALL be computed in one place and used by every render of the badge,
including each out-of-band refresh, so a background scan keeps a long-open page's badge current.

The dashboard tiles SHALL account for every tracked file, including files that are watched but not
yet baselined, so that the total does not silently exceed the sum of the tiles that explain it.
Status indicators SHALL use visually distinct icons for the "attention" and "alert" states.

#### Scenario: Mark an event reviewed

- **WHEN** the user marks an unacknowledged event reviewed
- **THEN** the event SHALL be recorded acknowledged, its row SHALL update in place, and the "need
  action" count and sidebar alert badge SHALL refresh — while the file's own status and the
  collection's status SHALL be unchanged

#### Scenario: Bulk mark-reviewed states its scope and confirms

- **WHEN** the user triggers the dashboard's bulk mark-reviewed action while open events exist
- **THEN** the control SHALL have stated how many events it affects and SHALL have required an
  explicit confirmation, and on confirmation every unacknowledged event in the user's collections
  SHALL be acknowledged and the feed, "need action" count and sidebar badge SHALL refresh without a
  full page reload

#### Scenario: Bulk mark-reviewed is scoped to the user

- **WHEN** a user triggers the bulk action in multi-user mode
- **THEN** only events belonging to that user's collections SHALL be acknowledged, and another
  user's unacknowledged events SHALL be left untouched

#### Scenario: Bulk mark-reviewed when nothing is open

- **WHEN** there are no unacknowledged events for the user
- **THEN** the bulk control SHALL NOT be shown (and the route SHALL be a no-op if invoked directly)

#### Scenario: Open issues tile links to a single affected collection

- **WHEN** exactly one collection has missing or modified files
- **THEN** the Open issues tile SHALL be a link to that collection's review view, with a visible
  call-to-action

#### Scenario: Open issues tile with several affected collections

- **WHEN** two or more collections have missing or modified files
- **THEN** the Open issues tile SHALL link to the fleet-wide review page

#### Scenario: Unreviewed events are visible in the feed that offers to clear them

- **WHEN** a user's collections hold unacknowledged events that are older than twenty
  already-acknowledged informational events
- **THEN** the feed SHALL render those unacknowledged events first, so every event the bulk
  mark-reviewed control names is displayed before the informational rows

#### Scenario: A healthy system's feed is not emptied

- **WHEN** every event in a user's collections is acknowledged
- **THEN** the feed SHALL still render the most recent events, and SHALL NOT render an empty state

#### Scenario: Open issues tile at zero

- **WHEN** no collection has missing or modified files
- **THEN** the tile SHALL render as a non-interactive element with no link

#### Scenario: The badge and the tile agree

- **WHEN** a user's collections contain both missing and modified files
- **THEN** the sidebar badge SHALL show the same total as the Open issues tile and SHALL expose an
  accessible label naming what the number counts

#### Scenario: Watched-but-not-baselined files are explained

- **WHEN** a collection contains files that are watched but not yet baselined
- **THEN** the dashboard SHALL show their count in its own tile, so the monitored-file total is
  accounted for by the tiles beside it

## ADDED Requirements

### Requirement: Fleet-wide review of every collection's open issues

The panel SHALL serve a fleet-wide review page listing every missing or modified file across all of
the collections the current user owns, grouped by collection, so that a fleet-wide issue count has a
destination that can act on it. Collections with no missing or modified files SHALL NOT be listed.

Each group SHALL name its collection, state that collection's missing and modified counts, show its
count of unreviewed events, and offer a link to that collection's own review view. Within a group,
missing files SHALL be listed before modified files. Groups SHALL be ordered worst-first (by missing
count, then modified count, then name).

Every number a group shows and every action a row authorises SHALL be derived from **one read** of
that collection's review population — the same single-snapshot property the per-collection review
view depends on, because a control authorised against a population the page did not display is the
failure that guard exists to prevent.

Each row SHALL offer the same **per-file** actions as the collection's own review view: marking the
file's open event reviewed, and the scoped per-file accept, each carrying the fingerprint minted from
that same snapshot. Marking a row reviewed SHALL update the row in place, refresh that group's
unreviewed count and the sidebar alert badge, and leave the group's file-derived counts unchanged,
because acknowledgement writes to the reading log and not to the file's state.

The page SHALL NOT offer any **collection-spanning bulk action**. Each bulk accept verb is
authorised over a single collection's whole population and is irreversible, so a fleet-wide bulk
would be an unscoped irreversible verb over several collections at once — the defect the scoped
verbs replaced, rebuilt one level up. The bulk verbs SHALL remain on each collection's own review
view, reachable from the group's link.

The page SHALL bound the rows it renders, both per collection and in total, so that one collection
with a very large issue set cannot crowd the others out. Where a group's rows are truncated the page
SHALL say how many more that collection holds and link to its review view; a collection over the
total budget SHALL still be listed with its counts and its link rather than omitted, because a
collection SHALL NOT disappear from a fleet-wide issue list because of a render budget.

The page SHALL be scoped to the current user's own collections, and SHALL render a distinct empty
state when the user has collections but no open issues, separate from the state where the user has
no collections at all.

#### Scenario: Issues in several collections are listed together

- **WHEN** a user with missing files in one collection and modified files in another opens the
  fleet-wide review page
- **THEN** the page SHALL render one group per affected collection, each naming its collection and
  its missing/modified counts, with its rows listed missing-first

#### Scenario: A row acts on one file without leaving the page's snapshot

- **WHEN** the user accepts a single file from a fleet-wide review row
- **THEN** the accept SHALL be authorised by the fingerprint minted from the same population read
  the row was rendered from, and SHALL act on that one file only

#### Scenario: A stale fleet-wide row is refused and returns to the fleet page

- **WHEN** the user submits a per-file accept from the fleet-wide page whose population has since
  changed
- **THEN** the request SHALL be refused without mutating anything, and the operator SHALL be
  returned to the fleet-wide page with the changed-since-loaded notice rather than to a different
  page

#### Scenario: No collection-spanning bulk verb is offered

- **WHEN** the fleet-wide review page renders any number of affected collections
- **THEN** it SHALL offer no control that accepts, retires or acknowledges across more than one
  collection

#### Scenario: Marking a row reviewed does not change the file counts

- **WHEN** the user marks a fleet-wide review row's event reviewed
- **THEN** the row and that group's unreviewed count and the sidebar badge SHALL refresh, and the
  group's missing and modified counts SHALL be unchanged

#### Scenario: One very large collection does not hide the others

- **WHEN** one collection holds more issues than the per-collection render budget while other
  collections also have issues
- **THEN** that group SHALL render its budgeted rows plus a count of how many more it holds and a
  link to its review view, and every other affected collection SHALL still be listed

#### Scenario: The page is scoped to the viewer's collections

- **WHEN** a user opens the fleet-wide review page in multi-user mode
- **THEN** only that user's own collections SHALL be listed, and no other user's files SHALL appear

#### Scenario: No open issues

- **WHEN** a user with collections has no missing or modified files anywhere
- **THEN** the page SHALL render an empty state saying so, distinct from the state shown to a user
  with no collections

### Requirement: The health pill names what is degraded and links to it

The panel's health indicator SHALL be an interactive link to the collections list, carrying a
**visible** label that states how many collections are degraded — not a tooltip, because a tooltip is
never shown on a touch device and the panel hides the pill's hint at phone widths, which leaves a
single word with no way to learn more.

The indicator SHALL NOT assert a health verdict it has not computed. Before the first health poll of
a page has answered, it SHALL render a neutral "checking" state; it SHALL NOT render a healthy state
as a placeholder. There SHALL be exactly one health indicator implementation in the panel: no page
SHALL hand-render a duplicate of it, and no page SHALL show a static health status that is computed
from nothing.

Each collection whose scan freshness is stale SHALL be marked as such on its own collection card, in
words rather than by colour alone, so that following the pill's link identifies the collection the
pill was talking about. The per-collection freshness records the panel reads SHALL carry the
collection's identifier, so a card can be matched to its freshness without matching on a name.

#### Scenario: A degraded pill names the number and links to the list

- **WHEN** one collection's scan freshness is stale
- **THEN** the health indicator SHALL render as a link to the collections list with a visible label
  naming that one collection is degraded

#### Scenario: The pill claims nothing before it has been computed

- **WHEN** a panel page is rendered before its health poll has answered
- **THEN** the health indicator SHALL render a neutral checking state and SHALL NOT render a healthy
  verdict

#### Scenario: The stale collection is identified where the link lands

- **WHEN** the operator follows the degraded health indicator to the collections list
- **THEN** the stale collection's card SHALL carry a marker naming its scan as overdue

#### Scenario: No page shows a fabricated health status

- **WHEN** the settings page's health-endpoint documentation is rendered
- **THEN** it SHALL NOT display a health status pill that is not derived from a health computation

### Requirement: A scan's real result is visible, and an interrupted run is not an alarm

The panel SHALL surface the result of a collection's scans, not only their timing. Where the most
recent completed scan was `partial`, the panel SHALL say so wherever it reports that scan, state how
many files were skipped, and offer a bounded sample identifying them — a scan that silently skipped
files SHALL NOT be presented identically to a scan that covered everything, because "I checked" shown
where the truth is "I checked most of them" is the false assurance this product cannot afford.

Because a skipped file may be one the datastore could not store at all, the sample SHALL be presented
as a diagnostic rendering of the offending names rather than as usable paths, and SHALL NOT be offered
as a copyable path list.

Scan freshness SHALL continue to be derived from **completed** scan runs only (`ok` or `partial`), so
that a run which never finished cannot refresh the panel's "last scan" claim. Separately from that,
where a collection's newest scan run ended `interrupted` or `error` more recently than its last
completed scan, the panel SHALL disclose it.

An `interrupted` run SHALL be rendered **neutrally and never as a failure or an alarm**. It is the
ordinary record of an operation claim that was abandoned and reclaimed — the routine outcome of
restarting the application during a scan — and rendering the routine consequence of a deploy as a
fault is the false alarm that teaches an operator to ignore the indicator that means something. A run
that ended in `error` SHALL be rendered as the failure it is. A collection whose only scan runs are
interrupted SHALL report that it has no completed scan, rather than reporting one of those runs as
its last scan.

#### Scenario: A partial scan says what it skipped

- **WHEN** a collection's most recent completed scan finished `partial` after skipping files
- **THEN** every place the panel reports that scan SHALL state that it was partial, name how many
  files were skipped, and offer a bounded sample identifying them

#### Scenario: A partial scan is not presented as a clean one

- **WHEN** a collection sits in a permanently partial state because a file's name cannot be stored
- **THEN** the panel SHALL NOT present its scan status as a clean scan

#### Scenario: An interrupted run is disclosed neutrally

- **WHEN** a collection's newest scan run ended `interrupted` because its claim was reclaimed after
  an application restart
- **THEN** the panel SHALL disclose that a later scan was interrupted and will re-run, in a neutral
  style, and SHALL NOT render it as a failure or an alarm

#### Scenario: An interrupted run does not refresh the last-scan claim

- **WHEN** a collection's newest scan run ended `interrupted` after an earlier scan completed
- **THEN** the panel's "last scan" SHALL still report the earlier completed scan

#### Scenario: A collection with no completed scan says so

- **WHEN** every scan run a collection has ever recorded ended `interrupted`
- **THEN** the panel SHALL report that the collection has no completed scan

#### Scenario: A failed run is rendered as a failure

- **WHEN** a collection's newest scan run ended `error`
- **THEN** the panel SHALL render it as a failure, distinct from the neutral interrupted rendering

### Requirement: A verification result that could change on a retry offers one

The verify result SHALL offer a retry control for the outcomes where running the same check again
can produce a different answer — a transport failure reaching the verification backend, and an
inconclusive result from a backend that cannot separate unreachability from its other outcomes. The
retry SHALL re-run the same file's verification and replace the result in place.

The retry control SHALL NOT be offered on outcomes that a retry cannot change — a file that has never
been stamped, a proof queued for stamping, a proof that could not be parsed, or any digest
disagreement. Offering a retry there presents a settled finding as provisional, and invites the
operator to re-run a check whose answer is already known.

Where an outcome tells the operator that the backend could not be reached, the result SHALL state the
transport reason it recorded. It SHALL report that reason as the typed transport failure it is, and
SHALL NOT print the backend's general result message under a verdict whose wording was chosen to
state exactly what the check established.

#### Scenario: An unreachable backend offers a retry and names the reason

- **WHEN** verification fails because the block explorer or node could not be reached
- **THEN** the result SHALL name the transport reason and SHALL offer a retry control that re-runs
  the same file's verification

#### Scenario: A settled outcome offers no retry

- **WHEN** the verified file has never been stamped, has a proof queued for stamping, has a proof
  that could not be parsed, or its digest disagrees with the digest its proof commits to
- **THEN** the result SHALL NOT offer a retry control

#### Scenario: The backend's general message is not printed under an attributed verdict

- **WHEN** the panel renders a verdict that attributes a digest disagreement to the file or to the
  proof
- **THEN** the card SHALL NOT print the verification backend's general result message alongside that
  attribution
