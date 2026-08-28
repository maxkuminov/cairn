# web-panel Specification

## Purpose
TBD - created by archiving change add-web-panel. Update Purpose after archive.
## Requirements
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

The dashboard's **last activity** tile summarises the newest finished run of **any** kind across the
user's collections. It SHALL therefore name the operation it is describing — the collection and the
kind of run (`scan`, `stamp`, `upgrade`) — and SHALL state that run's result whenever the result is
anything other than a clean completion. It SHALL NOT describe every finished run as a scan: a
`stamp` or `upgrade` run says nothing about when the files were last checked, and presenting one as
a scan is the same false assurance in a smaller box. A `partial` run SHALL be named partial, a run
that ended in `error` SHALL be named a failure, and an `interrupted` run SHALL be named neutrally
(it is the routine record of a reclaimed claim, never an alarm).

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

#### Scenario: The last-activity tile names a non-scan run for what it is

- **WHEN** the newest finished run across the user's collections is a `stamp` or an `upgrade` run
- **THEN** the last-activity tile SHALL name that operation's kind and SHALL NOT describe it as a
  scan

#### Scenario: The last-activity tile names a partial or failed run

- **WHEN** the newest finished run ended `partial` or `error`
- **THEN** the tile SHALL state that result alongside the collection and the kind, rather than
  presenting the run as a clean completion

#### Scenario: The last-activity tile treats an interrupted run neutrally

- **WHEN** the newest finished run ended `interrupted`
- **THEN** the tile SHALL name it neutrally and SHALL NOT present it as a failure

### Requirement: Accept and scan actions are available from the panel

The collection detail page SHALL offer "Scan now", and SHALL offer a re-baseline action only in the
state where that action is harmless. Both mutate via the existing services and leave the operator on
an up-to-date view of the collection; the re-baseline action MAY do so by an ordinary form
submission followed by a redirect to a freshly rendered page rather than an in-place refresh.

Re-baselining is irreversible: it rewrites the expected version of modified files and removes the
records of missing ones. The collection detail page SHALL NOT present it as the page's primary
action while the collection has missing or modified files. In that state the primary action SHALL be
a link to the review view, which explains the choice between noting a change and adopting it; the
re-baseline actions SHALL be reachable only from there. When the collection has no missing or
modified files, **no open (unreviewed) events**, and does have files that are merely not yet
baselined, the page MAY offer that harmless baseline action directly, and it SHALL require a
confirmation naming what it will do. Any re-baseline form rendered on this page SHALL carry a
confirmation.

Both conditions are still required, though the reason has narrowed with the verb. The baseline
action no longer marks the collection's other alerts reviewed — a scoped accept acknowledges only
the events of the files it touches, and the events recording a merely-added file are born reviewed,
so it now clears nothing the operator has not seen. What remains is the ordering: a file that was
missing and has since been restored is healthy again while its alert stays open, so a collection can
have no missing or modified files and still have an unread alarm. Offering a baseline action there
puts a "nothing to see" control in front of an alert that has never been read. Where the collection
has open events, the primary action SHALL remain the link to the review view, which is where those
events can be read and cleared deliberately.

Choosing what to render is not sufficient, because the collection can change between the page being
rendered and the form being submitted. **Every endpoint that re-baselines or removes a file record
SHALL be bound to the population its form was rendered for** — the collection-detail baseline
action, each of the review view's scoped actions, and the per-file action alike. None is exempt: a
rendered list is a statement about the past, so a scan that records another missing file after the
render makes "these are the files this button will adopt" false, and the operator removes a record
they never saw.

Each such form SHALL carry a **fingerprint of the population it claims to act on**, and the endpoint
SHALL recompute that fingerprint from the datastore and refuse unless it still matches.

The fingerprint SHALL identify each file by a **durable identity, not by a row identifier alone**:
it SHALL cover the file's path and its status, its content digest wherever one is recorded, and the
time the record itself was created, in addition to its row identifier. A row identifier can be
reused by the datastore after the row it named is deleted, so a fingerprint built from identifiers
and statuses alone can match a population that shares no file with the one the form was rendered for
— precisely the replay this guard exists to prevent. The record's creation time is what separates
one **generation** of a record from the next: a file record removed by an accept and later
re-created at the same path with the same content — even reusing the freed identifier — is a
different record that the operator has never been shown, and it SHALL NOT validate a form minted for
its predecessor. That creation time SHALL be set when the record is inserted and SHALL NOT be
rewritten while the record survives. For the same reason the fingerprint SHALL be scoped by the
collection's creation time as well as its identifier, so that a recreated collection reusing an
identifier cannot validate a fingerprint issued for its predecessor. The fingerprint SHALL also be
scoped so that a fingerprint issued for one form cannot validate another, and its encoding SHALL be
unambiguous for every legal file path, so that two different populations cannot be encoded to the
same input.

Because the accept verb is now **scoped** — a distinct action per file state, plus a per-file action
— that form-scoping is what keeps the actions apart: each form SHALL name the action it was minted
for, so a fingerprint minted for "adopt the changed files" can never validate a submission that
would remove the missing ones, and a fingerprint minted for one row can never validate a submission
at another row's address.

The fingerprint SHALL cover the **whole population its own action acts on**, rather than any
truncated list the page displayed and rather than the whole of what the page lists: for each of the
review view's scoped actions, every file in the collection in that action's state; for the
collection-detail baseline action, its entire not-yet-baselined set together with the assertion that
the collection has no missing or modified files **and no open events**; for the per-file action,
that single record. An absent or empty fingerprint SHALL be treated as a mismatch, so the check
fails closed.

The population a form's fingerprint is computed over and the population the page **renders** SHALL
be derived from a **single, consistent read** — one snapshot covering the file records, the open-event
set and any count the form asserts — rather than from separate reads that a concurrent writer can
interleave. A page that reads its visible rows in one query and computes the fingerprint in another
can publish a fingerprint for a population it never displayed, and an unchanged submission of that
form would then validate and destroy a record the operator was never shown — the exact accident the
guard exists to prevent, reintroduced inside the guard's own mint. **Where one page renders several
actions, all of their fingerprints — and every per-row fingerprint — SHALL be derived from that one
snapshot**, so no two controls on the same page can describe different states. The same single-read
derivation SHALL be used when the endpoint recomputes the fingerprint, so the two sides cannot encode
the same state differently. Any other claim the same page makes **about that population's
existence** — in particular the review view's "this collection indexes no files" state — SHALL be
derived from that same snapshot rather than from an earlier count, so that a concurrent re-baseline
committed between the two reads cannot leave the page reporting a healthy "all clear" for a
collection it has just emptied.

Any **count the action itself states** — a button label, and in particular a confirmation that names
how many files the submit will change — SHALL be derived from that same snapshot, never from an
earlier count query. This applies to the collection-detail baseline action as much as to the review
view's scoped ones: a page that counts the not-yet-baselined files in one statement and mints the
fingerprint in another lets a file committed between them be hashed without being counted, so the
operator confirms a number smaller than the set the form authorizes and an unchanged submit
baselines a file the confirmation never named. The number displayed, the population hashed and the
population eventually mutated SHALL be the same one. Counts the page uses for **pure display**
elsewhere — status tiles, totals, the file browser — are not claims the action makes and MAY come
from separate reads.

That single-read derivation SHALL hold for a row **re-rendered on its own** as well as for a whole
page. Where marking a file reviewed swaps that row back in with its accept control, the state the
row **displays** — its file status, and therefore the verb its control names — its open-event state
and the fingerprint that control carries SHALL all be derived from one read taken after the
acknowledgement is committed. A row rendered from a record read before that read can name one verb
while its fingerprint authorizes the other: a scan committing a change of status in between leaves
the swapped row offering to adopt a change while its fingerprint validates the removal of the
record, and the operator submits, unchanged, a form that performs a consequence the row never
displayed. Where that one read shows the row is in **no** action's population any more, the row
SHALL be rendered with no accept control and no fingerprint, rather than with a fingerprint for a
verb it does not show.

The fingerprint SHALL additionally cover a set of open (unreviewed) events **by identity** — each
event's identifier, its kind and the time it was detected — recomputed inside the same guarded
transaction as the file records. Covering that set by **count** is not sufficient: an open alert may
be acknowledged and a different one opened with the same kind, returning the count to what it was
while the incident the operator is being asked to clear, or being told does not exist, is a
different one. Event identifiers may themselves be reused after a record is deleted, so identity
alone is not sufficient either — hence the detection time, which separates one generation of an
alert from the next.

**Which** events that set contains SHALL depend on the action, because the term answers a different
question for each:

For the review view's scoped actions and the per-file action, the set SHALL be the open events
**belonging to the files in that action's own population** — the alerts the action itself will mark
reviewed — so that any change to *which* of them the action would clear is a refusal. That term
SHALL follow the action's scope rather than the collection: an action acknowledges only the events
of the files it touches, so binding it to every open event on the collection would refuse
submissions over drift the action cannot reach — an alert opening on a file in a different state
would refuse an action whose own population never moved — and refusals the operator cannot account
for are how a guard gets worked around.

For the collection-detail baseline action the set SHALL be the **collection's entire open-event
set**, un-narrowed, and the endpoint SHALL refuse when it is non-empty. This is a deliberate
exception to the scoping rule above, and it SHALL NOT be narrowed to the not-yet-baselined files'
own events. The baseline action clears no alert, so its event term is not a description of what it
will mutate; it is the **binding of the precondition that permits the action to be offered at all**
— that the operator is not baselining in front of an unread alarm — and that precondition is a claim
about the collection. Files that are merely not yet baselined essentially never carry open events of
their own, so a narrowed term would be empty for every input and would assert nothing, leaving the
render-time gate with no submit-time binding. It SHALL cover open events that belong to no file
record as well, since a detached alert is still unread.

Both of these are assertions about a population's alerts as it stood when the form was rendered, and
both SHALL be recomputed from the endpoint's own read rather than trusted from the submission.

Each action SHALL act on **only the population its own label names**. The earlier accepted
limitation — that a re-baseline from the review view also promoted not-yet-baselined files, which
were deliberately left outside the fingerprint so an actively growing collection would not refuse
every accept — is therefore **resolved rather than carried forward**: no action offered on the
review view touches the not-yet-baselined set at all, so a file appearing there between render and
submit is neither hashed nor acted on, and no such file can be promoted by an operator who was never
shown it.

The recomputation and the accept SHALL happen **within a single write transaction**, entered
before the recomputation reads anything it depends on, so that no concurrent scan can commit between
the check and the mutation. A check that is merely "recount, then call the service" is not a guard:
the recount and the deletion are separate statements, and a scan can claim, run and commit in the
gap. An in-flight-operation check SHALL also be made, but as a secondary precaution covering the
long window, not as the mechanism relied on for the short one.

Entering that write transaction can itself fail under contention — another writer holding the lock
past the connection's busy timeout, or having committed since this request's read snapshot. Such a
failure SHALL be handled as a **refusal**, identical to a fingerprint mismatch: nothing mutated, the
transaction rolled back, the same redirect and marker. It SHALL NOT surface as a server error, which
would leave the operator with a failed destructive POST, no account of why, and every reason to
retry it blind. Database failures that are **not** lock contention SHALL NOT be converted into a
refusal — reporting a corrupt or misconfigured datastore as "the collection changed since the page
loaded" hides a fault the operator must act on.

On refusal the endpoint SHALL mutate nothing — no file record removed, no baseline rewritten, no
event acknowledged — and SHALL redirect to the review view **carrying a marker that the view
renders as an explanation**. That explanation SHALL lead with the consequence — the operator's
action was **not** applied and nothing was deleted or acknowledged — before it describes the
collection, and only then state that the collection changed since the page loaded. A refusal with no
visible explanation is indistinguishable from a broken button and invites the operator to click it
again; one that opens by describing the collection reads as a status note and leaves the operator
believing the destructive action went through.

Where the refusal lands on a view with **nothing left to review**, the explanation SHALL still lead
with the action not having been applied, and SHALL account for the empty view by saying the state
may already have been resolved. It SHALL NOT tell the operator that the list shown is current when
there is no list, which reads as if their action had emptied it.

"Scan now" SHALL run the scan **asynchronously** — it SHALL start the scan in the background and
return immediately rather than blocking the request until the scan completes, so the panel can show
live operation status. A scan SHALL NOT be started for a collection that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: The destructive action is not offered beside the issues it would erase

- **WHEN** the user opens a collection that has missing or modified files
- **THEN** the page header SHALL offer a review link as its primary action and SHALL NOT contain an
  accept/re-baseline form

#### Scenario: Baselining new files is offered, and confirmed

- **WHEN** the user opens a collection with no missing or modified files, no open events, but with
  files not yet baselined
- **THEN** the page MAY offer a baseline action, which SHALL require a confirmation before
  submitting

#### Scenario: The baseline confirmation names the population its fingerprint covers

- **WHEN** the collection-detail page is rendered for a collection whose only non-baselined files are
  new, and a scan commits a further new file **between** the count that decides to offer the action
  and the read the fingerprint is minted from
- **THEN** the confirmation SHALL name the number of files in the snapshot the fingerprint was minted
  from — including the file that landed in that window — and submitting the form unchanged SHALL
  baseline exactly the files the confirmation named, so the displayed count, the hashed population
  and the mutation cannot disagree
- **AND WHEN** the further new file instead lands **after** that read, the confirmation SHALL name
  only the files that read saw, and the submission SHALL be refused rather than silently baselining
  the newcomer as well

#### Scenario: The baseline action is withheld while an alert is unread

- **WHEN** the user opens a collection that has files not yet baselined and no missing or modified
  files, but still has an open event — a file that was missing and has since been restored
- **THEN** the page SHALL NOT offer the baseline action, and SHALL offer the review view link as its
  primary action instead, so the outstanding alert is read on the view that shows it rather than
  cleared by a new-file promotion

#### Scenario: A stale baseline form is refused after the collection changes

- **WHEN** the user opens a collection whose only non-baselined files are new, a scan then records a
  file missing, and the user submits the already-rendered baseline form
- **THEN** the endpoint SHALL refuse, SHALL NOT re-baseline or remove any file record and SHALL
  leave the new missing file's event unacknowledged, and SHALL redirect to the review view with the
  marker that makes the refusal visible

#### Scenario: A stale baseline form is refused by an alert opening on a file it does not name

- **WHEN** the baseline form is rendered for a collection whose only non-baselined files are new,
  with no missing or modified files and no open events, and an **unrelated** file then goes modified,
  then missing, and is then restored — so that it ends healthy, the collection again has no missing
  or modified files and the not-yet-baselined set is untouched, while the alert raised when it was
  modified remains open because restoring a file clears only its missing alert — and the user submits
  the already-rendered baseline form
- **THEN** the endpoint SHALL refuse, SHALL baseline nothing and SHALL leave the open alert
  unacknowledged, and SHALL redirect to the review view with the staleness marker — the baseline
  action's no-open-alert assertion SHALL be bound to the whole collection, so an alert on a file the
  action would never touch SHALL still refuse it

#### Scenario: A scoped action is refused after its own population goes stale

- **WHEN** the user opens the review view listing one missing file, a scan then records a second
  file missing, and the user submits the already-rendered stop-tracking form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove either missing file's record or acknowledge
  either event, and SHALL redirect to the review view with the marker that makes the refusal visible

#### Scenario: Stop tracking is not refused by drift it cannot reach

- **WHEN** the user opens the review view, a scan then records a further file **modified** and opens
  its alert, and the user submits the already-rendered stop-tracking form whose own missing
  population is unchanged
- **THEN** the endpoint SHALL perform the removal, and SHALL NOT acknowledge the newly opened
  modified alert

#### Scenario: Adopt is not refused by drift it cannot reach

- **WHEN** the user opens the review view, a scan then records a further file **missing** and opens
  its alert, and the user submits the already-rendered adopt form whose own modified population is
  unchanged
- **THEN** the endpoint SHALL adopt the changed files, and SHALL NOT acknowledge the newly opened
  missing alert or remove the newly missing record

#### Scenario: A per-file action is not refused by drift on another row

- **WHEN** the user opens the review view, an alert is then opened on a **different** file — and
  that file's own state changes — and the user submits the already-rendered per-file action for a row
  that has not moved
- **THEN** the endpoint SHALL accept that one row, and SHALL neither acknowledge the other file's
  alert nor touch its record

#### Scenario: No accept form validates any action but its own

- **WHEN** the fingerprint minted for any one of the four accept actions — baseline, adopt, stop
  tracking, per-file — is submitted to any of the other three endpoints, for every such ordered pair
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing, in every pair, including where the
  two actions' populations happen to be identical

#### Scenario: A per-file form does not validate a submission at another row

- **WHEN** the fingerprint minted for one row's per-file action is submitted at a different row's
  per-file address, including where the two rows share a status, a content digest and a size
- **THEN** the endpoint SHALL refuse, SHALL mutate neither row, and SHALL redirect to the review
  view with the staleness marker

#### Scenario: A row swapped in after being marked reviewed states the verb it authorizes

- **WHEN** a file is marked reviewed from the review view and a scan changes that file's status
  between the point its record would have been read and the read the row's fingerprint is minted
  from
- **THEN** the verb the swapped-in row names and the fingerprint it carries SHALL describe the same
  state, so that submitting that row's form unchanged can perform no consequence other than the one
  the row displayed — and where the row is no longer in any action's population, it SHALL be
  rendered with no accept control and no fingerprint, and a submission at its address SHALL be
  refused

#### Scenario: A refused submission is explained on the review view

- **WHEN** the review view is opened with the staleness marker a refused accept redirects to
- **THEN** the view SHALL render a dismissable notice that states first that the action was not
  applied and that nothing was deleted or acknowledged, then that the collection changed since the
  page loaded and that the list shown is current, and SHALL NOT render that notice on an ordinary
  visit or for an unrecognized marker value

#### Scenario: A refusal landing on an all-clear view still says the action was not applied

- **WHEN** the review view is opened with the staleness marker and the collection has no missing or
  modified files and no open alerts left to review
- **THEN** the notice SHALL still state that the action was not applied and nothing was deleted or
  acknowledged, SHALL say the state may already have been resolved, and SHALL NOT claim that a list
  shown below is current

#### Scenario: A submission with no fingerprint is refused

- **WHEN** an accept-family action is submitted without the population fingerprint, or with an empty
  one
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing

#### Scenario: A reused row identifier does not validate a stale fingerprint

- **WHEN** the review view is rendered for a collection, the missing file it listed is then removed
  from the datastore and a scan records a **different** file, at a different path, whose row reuses
  the removed record's identifier and which is itself missing, and the operator submits the
  originally rendered form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove the replacement file's record or acknowledge
  its event, and SHALL redirect to the review view with the staleness marker — a reused identifier
  SHALL NOT be sufficient for a fingerprint to still match

#### Scenario: Replacing one open alert with another does not validate a stale stop-tracking form

- **WHEN** the review view is rendered for a collection with one missing file and one open alert on
  it, that alert is then acknowledged and a **new** alert of the same kind is opened on the same
  file — so the count of open alerts returns to exactly what it was while the file records never
  move — and the operator submits the already-rendered stop-tracking form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  remove the file's record

#### Scenario: Replacing one open alert with another does not validate a stale adopt form

- **WHEN** the review view is rendered for a collection with one modified file and one open alert on
  it, that alert is then acknowledged and a **new** alert of the same kind is opened on the same
  file, so the count of open alerts returns to what it was while the file records never move, and the
  operator submits the already-rendered adopt form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  re-baseline the file

#### Scenario: Replacing one open alert with another does not validate a stale per-file form

- **WHEN** a row's per-file action is rendered for a file with one open alert, that alert is then
  acknowledged and a **new** alert of the same kind is opened on that same file, so the count returns
  to what it was while the row itself never moves, and the operator submits the already-rendered
  per-file form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  accept the row

#### Scenario: A record that appears after the page's read is in neither the list nor the fingerprint

- **WHEN** an accept-family page is rendered and a scan commits a further missing file, and its
  alert, immediately after the page has read the population it renders
- **THEN** the page SHALL neither display that record nor include it in the fingerprint it
  publishes, and submitting the action whose population that record joined SHALL be refused because
  the endpoint's own read now sees it

#### Scenario: A re-created record does not validate a form minted for its predecessor

- **WHEN** an accept-family form is rendered listing a missing file, that record is then removed and
  a file is re-created at the **same path with the same content**, whose new record reuses the
  removed record's identifier and is itself recorded missing, and the operator submits the
  originally rendered form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove the re-created record or acknowledge its
  event, and SHALL redirect to the review view with the staleness marker — an identical path,
  digest and identifier SHALL NOT be sufficient for a fingerprint to still match across record
  generations

#### Scenario: A recreated collection does not validate its predecessor's fingerprint

- **WHEN** an accept-family form is rendered, its collection is then deleted and a new collection is
  created that reuses the same identifier, and the originally rendered form is submitted against it
- **THEN** the endpoint SHALL refuse and SHALL mutate nothing in the replacement collection

#### Scenario: Lock contention is a refusal, not a server error

- **WHEN** an accept-family action is submitted while another writer holds the datastore's write
  lock, or has committed since this request's read snapshot, so that the endpoint cannot take the
  write lock its check-and-act depends on
- **THEN** the endpoint SHALL refuse exactly as it does on a fingerprint mismatch — mutating
  nothing and redirecting to the review view with the staleness marker — and SHALL NOT return a
  server error

#### Scenario: A datastore failure that is not contention is not reported as staleness

- **WHEN** an accept-family endpoint's datastore access fails for a reason that is not write-lock
  contention
- **THEN** that failure SHALL surface as the error it is, and SHALL NOT be presented to the operator
  as the collection having changed since the page loaded

#### Scenario: A file appearing in a population no action names does not refuse anything

- **WHEN** a scoped action is submitted after a scan has added a file that is merely not yet
  baselined, while the action's own population is exactly what the view listed
- **THEN** the endpoint SHALL perform the action, and that file SHALL be neither promoted nor
  otherwise touched

#### Scenario: An accept is refused while an operation is in flight

- **WHEN** the user submits an accept-family form while a scan or stamp operation is running on that
  collection
- **THEN** the endpoint SHALL refuse rather than act on a population that is still changing

#### Scenario: A scoped action from the review view

- **WHEN** the user triggers one of the review view's scoped actions and that action's population
  has not changed since the page was rendered
- **THEN** that population SHALL be accepted — changed files re-baselined, or missing records
  removed — the events of those files SHALL be acknowledged, and the view SHALL refresh; the guard
  SHALL NOT stand in the way of an action that is still acting on what it displayed

#### Scenario: A per-file action is bound to its own row

- **WHEN** the user submits a row's accept control after that row has been accepted from another
  session, or after it has been restored and is no longer in the state the row described
- **THEN** the endpoint SHALL refuse, SHALL mutate nothing, and SHALL redirect to the review view
  with the staleness marker

#### Scenario: Scan now starts in the background and returns immediately

- **WHEN** the user clicks "Scan now" on a collection that has no operation in progress
- **THEN** the scan SHALL begin in the background, the request SHALL return without waiting for the
  scan to finish, and the collection's status SHALL begin reflecting an in-progress scan

#### Scenario: A second concurrent operation on the same collection is refused

- **WHEN** the user clicks "Scan now" on a collection that already has an operation (scan or stamp)
  in progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Add/edit corpus validates the root path

The add/edit-collection form SHALL validate the entered root path as the user types (server-side
htmx), indicating acceptance when the path is allowed and rejecting it with a clear message
otherwise, and SHALL keep the submit action disabled until the name and a valid root are present.
The server SHALL re-validate the root on submit. The form and its actions SHALL be served under the
`/collection` route prefix (e.g. `/collection/new`, `/collection/validate-root`,
`/collection/{collection_id}/edit`). Legacy `/corpus/...` URLs SHALL 308-redirect to the
corresponding `/collection/...` URL so existing bookmarks keep working. The form SHALL expose an
"auto-baseline new files" control whose state is persisted to the collection's `auto_baseline_new`
flag and pre-filled from it when editing.

#### Scenario: Out-of-bounds or missing root is rejected

- **WHEN** the user enters a root path that does not resolve to an allowed existing directory
- **THEN** the form SHALL show a rejection indicator and SHALL NOT allow submission

#### Scenario: Valid root accepted

- **WHEN** the user enters a name and a root that resolves to an allowed existing directory
- **THEN** the form SHALL indicate acceptance and submission SHALL create/update the collection

#### Scenario: Legacy corpus URL redirects to the collection URL

- **WHEN** a client requests an old `/corpus/{id}` (or any `/corpus/...`) URL
- **THEN** the panel SHALL respond with a 308 redirect to the equivalent `/collection/...` URL

#### Scenario: Auto-baseline toggle persists

- **WHEN** the user turns the "auto-baseline new files" control on (or off) and submits the form
- **THEN** the collection's `auto_baseline_new` flag SHALL be saved accordingly, and re-opening the
  edit form SHALL show the saved state

### Requirement: Verify a tracked file's proof from the panel without upload

The verify page SHALL let the user search files Cairn already tracks (no file upload) and verify a
selected file by re-hashing it from the read-only store and checking the stored `.ots` proof. The
result SHALL present the verdict and, when verification succeeded, the SHA-256, the existed-by date,
and the Bitcoin block, plus an option to export the portable bundle. A complete "Anchored" badge
elsewhere SHALL deep-link here and verify immediately.

**Confirmed provenance SHALL be presented only on a verified verdict.** On any other verdict the
block height and date available to the panel are what the *proof declares*, read out of the proof
file and confirmed against nothing; the page displays the file's live fingerprint, so presenting
them together as the record of "this fingerprint" asserts a link that was not established, and is
simply false where the file has changed — that block belongs to the digest the proof was made from.
Where such proof-declared metadata is shown at all it SHALL be explicitly labelled as recorded in
the proof and unverified, SHALL NOT be captioned as where the displayed fingerprint is recorded, and
SHALL carry the same qualification into any copyable report the page offers.

The same labelling rule governs the **fingerprint** the page displays. Where the live bytes were
hashed, that is the file's fingerprint. Where they could not be read, the page SHALL NOT present an
empty or unknown value in that slot: it SHALL show the digest the last scan recorded, labelled as
the last recorded fingerprint and stated as not having been compared with anything in this check.
The recorded digest is the one fact Cairn still holds about a file that has gone, and it is the
value an operator needs in order to look for the file elsewhere.

The page's statement of **which backend was used to check** SHALL be rendered only where a lookup
was actually made and answered. Where nothing was ever looked up — no proof exists, the proof is
queued or unparseable, the live file could not be read, the digests disagreed before any attestation
was fetched, or the backend could not be reached — naming a backend reports a check that did not
run, and SHALL be omitted from the result, from the closing guidance and from any copyable report.

Where the page tells the operator how to obtain fully trustless verification it SHALL name the
**environment settings** that select the node backend, and SHALL NOT direct them to the settings
page, which renders the verification backend read-only.

The verdict SHALL be chosen by *why* verification did not succeed, not by the proof's stored state.
The panel SHALL evaluate the outcomes in this order: the live file being unavailable, then a
**digest mismatch**, then a **verified** result, then a **proof mismatch**, then a **transport
failure**, then an **inconclusive** result, then a proof **awaiting Bitcoin confirmation**, then a
proof **queued for stamping**, then anything else. Mismatch SHALL be evaluated before transport: a
mismatch that was established before the backend became unreachable is knowledge, and SHALL NOT be
discarded in favour of the transport outcome.

A **verified result SHALL outrank a proof mismatch**. Timestamp verification is existential: a
proof may carry several Bitcoin attestations and one of them confirmed against its real block is
sufficient proof, so a mismatched sibling attestation SHALL NOT turn a verified proof into a
failure. It MAY be reported as diagnostic detail alongside the verified verdict. Rendering a red
"this proof does not check out" over a genuinely anchored proof is a false alarm on the same signal
a digest mismatch reports truthfully, and false alarms are what teach an operator to dismiss the
real one.

A file whose digest disagrees with the digest its proof commits to SHALL be rendered as a failure
that names that fact — it SHALL NOT be rendered as a proof awaiting confirmation, and SHALL NOT be
accompanied by copy telling the operator to wait. This is the product's core detection; presenting
it as a young proof is a false negative on the one claim Cairn exists to make.

**The panel SHALL attribute such a disagreement using the file's recorded baseline digest**, and
SHALL NOT attribute it from the disagreement alone. Where the live bytes no longer hash to the
baseline Cairn recorded at its last scan, the *file* changed and the panel SHALL say so.

Where the live bytes still hash to that baseline, the file is not what moved, and the panel SHALL
NOT say the file changed. What it may say about the **proof** depends on whether the file records
the digest its stored proof was placed committing to.

**Where that provenance is recorded, the attribution is established, not inferred.** The recorded
provenance is what the system itself wrote when it placed the proof. These readings SHALL be
evaluated **in the order given**, and the panel SHALL reach the "not the proof recorded for this
file" reading **before** any reading that describes the stored proof as merely older than the file.
Where the recorded provenance is one digest, the live and baseline bytes are a second, and the stored
proof commits to a third, the proof is not an earlier proof of this file at all — it is a proof of
something else sitting at this file's path — and describing it as an old proof of this file is a
false reassurance on the very page an operator opens to ask whether their evidence is sound:

- where the digest the stored proof commits to is known and **differs** from the recorded
  provenance, the proof at that path is not the one the system recorded placing there: the panel
  SHALL report as established that the stored proof is not the proof recorded for this file —
  corrupted, swapped or misfiled — and SHALL NOT offer the proof-predates-this-version explanation.
  This SHALL hold whether or not the recorded provenance also differs from the live digest;
- where the recorded provenance **equals** the live digest, the panel SHALL report that same
  established finding even where the digest the stored proof commits to is unavailable: the system
  recorded placing a proof for exactly these bytes and the proof at that path disagrees with them;
- where the recorded provenance **differs** from the live digest **and** the stored proof is known
  to commit to exactly that recorded provenance, the panel SHALL report **only what that comparison
  established**: that the proof stored at this path commits to the fingerprint previously recorded
  for this file rather than to its current one, that this is not evidence against the current file,
  and — explicitly — that the proof's Bitcoin attestations were **not** validated in this check.
  It SHALL NOT report that artifact as the proof the system placed, nor that it still covers,
  attests or is good evidence for the earlier version. A committed digest identifies bytes, not an
  artifact: any `.ots` built over the same earlier bytes commits to the same digest, so a
  fabricated, unanchored or substituted proof dropped at that path satisfies the comparison exactly
  as the real one does, and verification exits on the digest disagreement before any attestation is
  checked. Asserting identity or continued good standing there would launder a swapped proof into a
  reassurance on the one page an operator opens to ask whether their evidence is sound. It SHALL
  claim a re-stamp is pending only where the file record's **proof state** says one is queued —
  never from the file's status, which stays `modified` forever in a collection whose stamping has
  since been turned off;
- where the recorded provenance differs from the live digest but the digest the stored proof commits
  to is **not** available, neither finding is established: the panel SHALL fall back to the wording
  it uses where no provenance is recorded, and SHALL NOT report the proof as predating this version
  on the strength of the recorded provenance alone.

Neither of those readings SHALL say anything against the file, whose bytes still match their
recorded baseline.

**Where no such provenance is recorded** — a proof stored before the system began recording it — the
panel SHALL NOT state as established that the proof is corrupted or misfiled: the recorded baseline
is **not** the digest the stored proof was made from — a scan
overwrites it with the newly observed bytes before any replacement proof exists — so during the
window between a modification and its re-stamp a perfectly good proof legitimately commits to
different bytes. The panel SHALL therefore distinguish two readings of that case from the file
record's own state:

- where the record indicates a re-stamp is owed — its proof state is queued for stamping, or its
  status is modified or new — the panel SHALL report that the file matches its current recorded
  baseline while the stored proof commits to different bytes because the proof **predates this
  version** and a re-stamp is pending, and SHALL state that this is not evidence against the current
  file;
- otherwise the panel SHALL report the disagreement and attribute it to **neither** artifact,
  stating that the proof may be from an earlier version of the file or may be corrupted and that
  Cairn cannot tell which without a record of the digest that proof was made from.

Where no baseline digest is recorded, the panel SHALL name both possibilities and blame neither. In
no case SHALL the panel state that the proof is intact or still attests an earlier version of the
file, because no verdict on this path validated the proof. Accusing an intact file of changing is a
false alarm on the product's core signal; certifying an unvalidated proof is a false assurance about
the evidence itself; and accusing a valid proof of corruption for the ordinary consequence of
editing a file is the same false alarm pointed at the evidence.

A result where the **live file could not be read** — gone from disk, or unreadable — SHALL be
rendered in its own words: the file could not be read, so its bytes were never hashed and nothing
was compared. It SHALL state that this says nothing about whether the file was altered, and it
SHALL NOT be rendered with the generic wording that offers a possible change of contents as an
explanation. No comparison was made, so a change of contents is not a finding this outcome supports
— it is speculation about tampering built on a check that never ran, on the one page an operator
consults to answer exactly that question. Where the file's own record already states the file is
missing from the collection, the panel SHALL say so plainly and SHALL link to the collection's
review view, so the outcome reads as the already-known state it is rather than as a new fault.

A result whose stored proof **could not be parsed** SHALL be rendered in its own words: the proof
file could not be read, and no conclusion was reached about the file. It SHALL NOT be rendered with
the generic wording that offers a possible file change or an unconfirmed proof as explanations —
neither was established, and the one thing that *was* established (the proof is unusable) is the
part the operator can act on.

A **proof mismatch** SHALL be rendered as a failure of the *proof*, with copy stating that the
proof's chain attestation does not check out and that this is not evidence the file changed. It
SHALL NOT reuse the digest-mismatch copy: the file's bytes may be exactly what was stamped, and
telling an operator their file changed when it did not is a false alarm on the same signal the
product exists to make trustworthy.

A failure to *reach* the verification backend (block explorer or Bitcoin node) SHALL be reported as
verification being **unavailable**, with copy stating that Cairn could not check and that this says
nothing about the file. Such a failure SHALL NOT inherit the file's stored proof state, and SHALL
NOT be reported as a pending proof or as a verified one. It SHALL be presented in a neutral style —
neither the pending style nor the failure style used for a mismatch: an unreachable network is not
evidence against the file, and rendering it as a failure teaches the operator to dismiss the styling
that means a real mismatch. The panel SHALL derive this outcome from the verification result itself,
not only from a raised error, because the backends report most unreachability as an ordinary
returned result.

Where a transport failure is present on a result whose verdict was decided by something that
outranks it — a **verified** result or a **proof mismatch** — the panel SHALL still disclose it, as a
diagnostic line beneath the verdict naming that some attestation lookups failed and that the verdict
rests on the attestations that could be reached; on a proof mismatch that line SHALL qualify the
mismatch as established only over those attestations. Precedence decides the headline, not what the
card is permitted to say: a categorical "this proof does not check out" over a proof half of which
was never checked, or a clean bill over a proof only partly reachable, tells the operator more than
was established.

Where the configured backend cannot distinguish "not yet confirmed" from "the file no longer
matches" from "the backend itself could not be reached", the panel SHALL render an **inconclusive**
verdict that names **every** possibility that backend cannot separate — including its own
unreachability — and says which backend cannot separate them. It SHALL NOT present such a result as
a proof awaiting confirmation, and it SHALL NOT narrow the list by inferring a cause from the
backend's output text: an inferred cause is a guess, and naming only "not yet confirmed or changed"
raises a file-change alarm for what is most often an unreachable node.

A proof **awaiting Bitcoin confirmation** SHALL be reported with the date it was submitted, and its
reassurance SHALL depend on how long it has been waiting. Past the same age at which the upgrade
command warns about a stuck proof, the panel SHALL drop the "usually settles within a few hours"
reassurance, state how long the proof has been waiting and that this is unusually long, and name the
upgrade command and the calendar servers as what to check. Repeating a few-hours reassurance over a
proof submitted months ago tells the operator to keep waiting for something that is not coming.

The two not-yet-confirmed proof states SHALL produce **two different verdicts**, never one: a proof
submitted and awaiting Bitcoin confirmation, and a proof queued for stamping that has not been
submitted at all. The verdict for the queued state SHALL NOT contain awaiting-confirmation wording,
because there is nothing yet to await.

A file whose bytes are readable but for which **no proof exists yet** SHALL NOT be rendered as a
verification failure. Where the file's recorded proof state is "queued for stamping" it SHALL read
as queued, in the same words as a queued result; where no proof has ever been made for it, the panel
SHALL say so neutrally, and SHALL NOT say that the file could not be verified or that its contents
may have changed. There is no proof, so nothing was checked and nothing failed; a red verdict there
blames the file for an absence of evidence nobody has created, and teaches the operator to discount
the red card that means a real mismatch. The panel SHALL NOT offer a proof download for a file that
has no stored proof.

Every list of files with proofs SHALL render each row's **actual** notarization state. A row
SHALL NOT be given a fixed "Anchored" badge: the badge is what an operator reads to decide whether a
proof is usable as evidence, and the list is ordered newest-first, so a hardcoded badge shows the
least-confirmed proofs as the most confirmed. **The list's own heading SHALL use vocabulary that
covers every proof state the list contains**: the list deliberately includes submitted-but-unconfirmed
proofs, so a heading claiming they are anchored contradicts the very badges beneath it, and a
container read as a summary of its rows overrides them. The page SHALL render a distinct empty state
when nothing has been stamped yet, separate from the empty state for a search that matched nothing.

The result page SHALL describe how the proof was checked from the backend actually used, rather than
asserting a fixed backend.

#### Scenario: Verify renders a verdict

- **WHEN** the user selects an anchored file on the verify page
- **THEN** the panel SHALL run verification server-side and render the verdict (and, when complete,
  the block and existed-by date) without uploading the file

#### Scenario: A changed file is never reported as pending

- **WHEN** the user verifies, through the default explorer backend, a stamped file whose bytes have
  changed since it was stamped
- **THEN** the panel SHALL render a failure verdict naming the mismatch, and SHALL NOT render a
  "pending confirmation" title or any copy suggesting the result will settle on its own

#### Scenario: The node backend's unverified result reads inconclusive, not pending

- **WHEN** the node backend returns a not-verified, not-yet-anchored result, which on that backend
  may equally mean the file no longer matches or that the node was unreachable
- **THEN** the panel SHALL render an inconclusive verdict naming **all three** possibilities — not
  yet confirmed, the file no longer matching, and the node being unreachable — and stating that the
  Bitcoin-node backend cannot tell them apart, and SHALL NOT render a "pending confirmation" title
  or copy suggesting the result will settle on its own

#### Scenario: A valid attestation is not overruled by a mismatched sibling

- **WHEN** a proof carries two Bitcoin attestations for the file's current digest, one of which
  confirms against its real block while the other does not
- **THEN** the panel SHALL render the verified verdict, and SHALL NOT render the proof-mismatch
  failure

#### Scenario: A long-unconfirmed proof drops the reassurance

- **WHEN** the user verifies a file whose proof has been awaiting Bitcoin confirmation for longer
  than the configured stuck-proof alarm age
- **THEN** the result SHALL name the submission date and how long it has been waiting, SHALL say
  that this is unusually long, SHALL name the upgrade command and the calendar servers as what to
  check, and SHALL NOT say that it usually settles within a few hours

#### Scenario: A recently submitted proof keeps the reassurance and gains its date

- **WHEN** the user verifies a file whose proof was submitted within the stuck-proof alarm age and
  is not yet confirmed
- **THEN** the result SHALL name the submission date and SHALL say that this usually settles within
  a few hours

#### Scenario: A queued proof is not described as awaiting confirmation

- **WHEN** the verified file's proof is queued for stamping and has not been submitted to a
  calendar, with no mismatch, transport failure or inconclusive outcome on the result
- **THEN** the verdict SHALL say the proof is queued to stamp, and SHALL NOT use the
  awaiting-confirmation wording reserved for a submitted proof

#### Scenario: A file with no proof yet is not a verification failure

- **WHEN** the user verifies a readable file whose proof is queued for stamping, or that has never
  been stamped, so that no proof exists to check
- **THEN** the panel SHALL render the queued reading for the queued file and a neutral
  never-notarized reading for the unstamped one, SHALL NOT render a failure verdict or copy
  suggesting the file may have changed, and SHALL NOT offer a proof download

#### Scenario: A proof mismatch does not blame the file

- **WHEN** verification fails because a Bitcoin attestation's commitment does not match the block's
  merkle root, while the file's digest is still the one the proof commits to
- **THEN** the panel SHALL render a failure naming the proof as what does not check out, and SHALL
  NOT state that the file's bytes changed

#### Scenario: A verified verdict discloses the lookups that failed

- **WHEN** a proof's verification confirms one attestation against its real block while the lookup
  for another attestation fails, so the result is verified **and** carries a transport failure
- **THEN** the panel SHALL render the verified verdict **and** a diagnostic line saying that
  attestation lookups failed and that the verdict is based on the attestations reached

#### Scenario: A proof mismatch discloses the lookups that failed

- **WHEN** a proof's only fetched attestation mismatches its block while another attestation's
  lookup fails, so the result carries both the proof mismatch and a transport failure
- **THEN** the panel SHALL render the proof-mismatch failure **and** a diagnostic line saying that
  attestation lookups failed, qualifying the mismatch as based on the attestations reached, and
  SHALL NOT present the mismatch as if the whole proof had been checked

#### Scenario: An unreachable verification backend is not a pending proof

- **WHEN** verification does not succeed because the block explorer or node could not be reached —
  whether that is raised as an error or returned as an ordinary result carrying the transport reason
- **THEN** the panel SHALL report that verification is unavailable, in the neutral style, with the
  transport reason, and SHALL NOT present the file's stored proof state as the verdict

#### Scenario: A missing live file still refuses to fall back to the stored digest

- **WHEN** the user verifies a file that is gone from disk
- **THEN** the panel SHALL report that the file is unavailable and cannot be verified, and SHALL NOT
  verify against the stored digest

#### Scenario: An unreadable live file is explained, not speculated about

- **WHEN** the user verifies a file whose bytes could not be read, so that nothing was hashed and
  nothing was compared
- **THEN** the result SHALL say that the file could not be read and that nothing was compared, SHALL
  state that this says nothing about whether the file was altered, and SHALL NOT offer that the
  file's contents may have changed as an explanation

#### Scenario: A file already recorded missing is named as such and linked to review

- **WHEN** the user verifies a file whose own record already carries the missing status
- **THEN** the result SHALL state that Cairn's record already lists the file as missing from its
  collection, and SHALL offer a link to that collection's review view

#### Scenario: A file that could not be read shows its last recorded fingerprint

- **WHEN** the user verifies a file whose bytes could not be read and for which a baseline digest is
  recorded
- **THEN** the page SHALL display that recorded digest, labelled as the last recorded fingerprint
  and stated as not compared in this check, and SHALL NOT display an unknown fingerprint

#### Scenario: The backend is named only where a lookup happened

- **WHEN** the user verifies a file for which no lookup was made — it has never been stamped, its
  proof is queued or unreadable, or its live bytes could not be read
- **THEN** the result SHALL NOT name a verification backend as having been used

#### Scenario: The anchored list shows real proof states

- **WHEN** the verify page lists recent proofs that include a not-yet-confirmed one
- **THEN** that row SHALL show the not-yet-confirmed badge, not "Anchored"

#### Scenario: The list heading does not overclaim its rows

- **WHEN** the verify page's list contains a submitted-but-unconfirmed proof
- **THEN** the list heading SHALL NOT describe its contents as anchored, while each row's badge
  SHALL continue to name that row's own state

#### Scenario: A digest disagreement over an unchanged file blames neither artifact

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  which records no provenance for its stored proof, and whose record indicates no re-stamp is owed,
  and the proof commits to a different digest
- **THEN** the panel SHALL state that Cairn cannot tell whether the proof predates the file's current
  version or is corrupted, and SHALL NOT state that the file's bytes changed, that the proof is
  intact, or that the proof is corrupted or misfiled

#### Scenario: Recorded provenance establishes that the stored proof is not this file's proof

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  whose recorded proof provenance equals that same digest, and whose stored proof commits to a
  different digest
- **THEN** the panel SHALL report as established that the stored proof is not the proof recorded for
  this file, SHALL NOT say the file's bytes changed, and SHALL NOT offer the
  proof-predates-this-version explanation

#### Scenario: Recorded provenance establishes which fingerprint the stored proof commits to

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  whose recorded proof provenance differs from that digest, and whose stored proof commits to exactly
  that recorded provenance
- **THEN** the panel SHALL report that the stored proof commits to the fingerprint previously
  recorded for this file rather than to its current one, SHALL state that its Bitcoin attestations
  were not validated in this check, SHALL state that this is not evidence against the current file,
  and SHALL NOT describe that artifact as the proof Cairn placed or as still covering the earlier
  version

#### Scenario: An established staleness reading claims no re-stamp that is not queued

- **WHEN** the panel verifies such a file whose record carries a stamping-complete proof state and a
  modified status — a collection whose per-file stamping was turned off after the modification
- **THEN** the panel SHALL NOT state that a re-stamp is pending or queued, and SHALL claim one only
  where the file record's proof state says one is queued

#### Scenario: A proof matching neither the file nor the record is not shown as merely old

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  whose recorded proof provenance is a **different, earlier** digest, and whose stored proof commits
  to a **third** digest matching neither
- **THEN** the panel SHALL report as established that the stored proof is not the proof recorded for
  this file, and SHALL NOT report that the proof merely predates this version of the file

#### Scenario: A digest disagreement while a re-stamp is owed reads as a proof that predates the file

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  whose record indicates a re-stamp is owed, and whose proof commits to a different digest
- **THEN** the panel SHALL report that the proof predates this version of the file and a re-stamp is
  pending, SHALL state that this is not evidence against the current file, and SHALL NOT render the
  result as a failure of the proof or of the file

#### Scenario: A digest disagreement with no recorded baseline blames neither

- **WHEN** the panel verifies a file that has no recorded baseline digest and the digests disagree
- **THEN** the panel SHALL name both possibilities and SHALL NOT attribute the disagreement to the
  file or to the proof

#### Scenario: An inconclusive verdict shows no confirmed block

- **WHEN** the panel renders an inconclusive or otherwise unverified result for a proof that
  declares a Bitcoin block
- **THEN** the page SHALL NOT present that block as where the displayed fingerprint is recorded; if
  it is shown at all it SHALL be labelled as recorded in the proof and unverified, in the card and
  in any copyable report

#### Scenario: An unreadable proof reaches no conclusion about the file

- **WHEN** the panel verifies a file whose stored proof cannot be parsed
- **THEN** the card SHALL say the proof could not be read and that no conclusion was reached about
  the file, and SHALL NOT say the file's contents may have changed or that the proof is not
  confirmed yet

#### Scenario: A verified result with no parsed block details still renders

- **WHEN** the configured backend returns a verified result carrying no block height or date
- **THEN** the card SHALL render the verified verdict without claiming provenance it does not have

#### Scenario: Nothing anchored yet

- **WHEN** the user opens the verify page and no file has been stamped
- **THEN** the page SHALL render an empty state saying so, distinct from the "no search matches"
  message

### Requirement: Stamp-all control in the corpus view

The corpus view SHALL offer an owner/admin control to stamp all currently-unstamped files in that
corpus (the on-demand backfill). The control SHALL be subject to the same authorization scoping as
the rest of the panel (in `multi` mode a user SHALL only stamp corpora they own; an admin MAY act on
any). The control SHALL NOT be offered for `none` (tripwire) corpora, which are never stamped.

Stamp-all SHALL run **asynchronously** — it SHALL start the backfill in the background and return
immediately rather than blocking the request until every file is stamped, so the panel can show live
stamping status. Stamp-all SHALL NOT be started for a corpus that already has an operation in
progress; the panel SHALL indicate that an operation is already running instead of starting a second
one.

#### Scenario: Owner triggers stamp-all from the corpus view

- **WHEN** the corpus owner (or an admin) activates the stamp-all control for a `perfile` corpus with
  no operation in progress
- **THEN** the backfill SHALL begin in the background, the request SHALL return without waiting for it
  to finish, and the corpus's status SHALL begin reflecting an in-progress stamping operation that
  stamps every currently-unstamped file via the batched path

#### Scenario: Stamp-all is not offered for tripwire corpora

- **WHEN** the corpus's OTS mode is `none`
- **THEN** the stamp-all control SHALL NOT be shown for that corpus

#### Scenario: Stamp-all is refused while another operation runs

- **WHEN** the user activates stamp-all on a corpus that already has a scan or stamp in progress
- **THEN** a second operation SHALL NOT be started and the panel SHALL report that an operation is
  already running

### Requirement: Corpus file list is searched, filtered, sorted, and paginated server-side

The corpus detail page SHALL offer two browse views of the corpus contents — a **folder tree**
(default) and a **flat list** — with a control to switch between them. Both views SHALL be rendered
server-side and SHALL NOT materialize the entire file set (corpora can hold ~186k files).

The **folder tree** SHALL present the corpus as a lazily expanded directory hierarchy derived from
each file's relative path, fetching **one directory level per request**. Expanding a folder SHALL
return that folder's immediate subfolders and the files directly within it; subfolders SHALL be
fetchable on demand and SHALL NOT be pre-expanded recursively. Each subfolder row SHALL show its file
count and a roll-up indicator when any file beneath it has status `modified` or `missing`. Files at a
level SHALL themselves be paginated when they exceed one page, so a single large folder is never
rendered in full.

The **flat list** SHALL list files via server-side search, status filtering, sorting, and pagination.
Search SHALL match the relative path; the filter SHALL offer All / Issues / New / OK.

Sorting (flat list) SHALL be server-side over a fixed whitelist of columns — relative path, size,
modified time (`last_changed`), notarization time (`ots_stamped_at`), and last-checked time — each
toggleable ascending or descending. An unrecognized sort or direction SHALL fall back to the default.
The default order SHALL be newest-activity-first (`last_changed` descending) so the most recently
changed files appear first on load. Every sort SHALL apply a stable secondary tiebreak (relative
path) so pagination is deterministic. The active sort column and direction SHALL be indicated in the
table header.

Pagination SHALL expose navigation (previous / next) and a current-page-of-total-pages indicator,
returning at most one page of rows per request. The active search query, status filter, and sort
SHALL be preserved across page changes and across one another.

Each file row SHALL prominently display a timestamp. For a notarized file the row SHALL show the
OTS stamp date (`ots_stamped_at`) together with the notarization-state badge; a `complete` proof's
notarization cell SHALL deep-link to the verify page for the block-confirmed existed-by date. The
list SHALL NOT fabricate or fetch the Bitcoin block date per row. For an unstamped file, or a
tripwire (`ots_mode='none'`) corpus that hides the notarization column, the row SHALL fall back to
showing the file's last-changed date. The footer SHALL report how many of the total are shown.

#### Scenario: Tree view is the default browser

- **WHEN** the user opens a corpus detail page
- **THEN** the folder-tree view SHALL be shown by default, listing the top-level folders and files of
  the corpus root, and a control SHALL be present to switch to the flat list view

#### Scenario: Expanding a folder fetches one level server-side

- **WHEN** the user expands a folder in the tree
- **THEN** the response SHALL contain only that folder's immediate subfolders and the files directly
  within it (not the whole subtree), fetched server-side

#### Scenario: Subfolder shows count and issue roll-up

- **WHEN** a folder in the tree contains a file with status `modified` or `missing` anywhere beneath
  it
- **THEN** that folder's row SHALL display its file count and an issue indicator

#### Scenario: Switching to the list view preserves flat-list behavior

- **WHEN** the user switches from the tree to the list view
- **THEN** the existing searched / filtered / sorted / paginated flat list SHALL be shown, defaulting
  to newest-activity-first

#### Scenario: Only a page of results is returned

- **WHEN** a corpus has more files than one page and the user loads or searches the flat list
- **THEN** the response SHALL contain at most one page of rows plus a "showing N of TOTAL"
  indicator, never the full list

#### Scenario: Filter to issues

- **WHEN** the user selects the Issues filter
- **THEN** only files with status `modified` or `missing` SHALL be listed

#### Scenario: Default order is newest activity first

- **WHEN** the user opens the flat list without choosing a sort
- **THEN** files SHALL be ordered by most recent change (`last_changed`) descending, with relative
  path as a stable tiebreak

#### Scenario: Sort by a chosen column toggles direction

- **WHEN** the user activates a sortable column header (e.g. size or notarized date)
- **THEN** the list SHALL re-query server-side ordered by that column, the chosen direction SHALL be
  indicated in the header, and re-activating the same column SHALL reverse the direction

#### Scenario: Page through results preserving search, filter, and sort

- **WHEN** the user advances to the next page with an active search, filter, and/or sort
- **THEN** the next page of the same filtered, sorted result set SHALL be returned, the
  page-of-total indicator SHALL update, and previous SHALL be disabled on the first page and next
  on the last

#### Scenario: Notarized file shows its stamp date and deep-links to verify

- **WHEN** a notarized file is listed in a `perfile` corpus
- **THEN** its row SHALL show the OTS stamp date with the notarization-state badge, and a `complete`
  proof's notarization cell SHALL link to the verify page for the block-confirmed existed-by date

#### Scenario: Unstamped or tripwire file falls back to last-changed date

- **WHEN** a file has no proof, or the corpus is tripwire (`ots_mode='none'`) and hides the
  notarization column
- **THEN** the row SHALL still display a meaningful timestamp by showing the file's last-changed date

### Requirement: Live operation status is surfaced on the dashboard and corpus view

The panel SHALL surface whether a corpus currently has a background operation in progress and which
kind it is. When a corpus has a run in progress (result `running`), the dashboard corpus card and the
corpus detail status indicator SHALL show an in-progress badge **labelled by the operation kind** —
scanning for an integrity scan, stamping for a stamp backfill, and upgrading proofs for an OTS upgrade
pass.

When the run carries a known or estimable total, the badge SHALL show progress as items processed out
of that total with a corresponding percentage and a progress bar; for a scan the total MAY be an
estimate and the percentage SHALL NOT reach 100% before the scan finishes. When no total is available
(e.g. a first-ever scan with no baseline), the badge SHALL show an indeterminate in-progress state
with the elapsed time and the running processed count, without a misleading percentage.

While an operation is in progress, the indicator SHALL refresh on its own (without a manual page
reload) and SHALL stop refreshing once the operation finishes, at which point the indicator SHALL
resolve to the corpus's normal status. A corpus with no operation in progress SHALL NOT poll. The
indicator SHALL be read-only and SHALL NOT alter scan, accept, stamp, or upgrade behavior.

#### Scenario: A scanning corpus shows a labelled progress badge

- **WHEN** a corpus has an integrity scan in progress and a prior completed scan provides a baseline
- **THEN** its dashboard card and detail status SHALL show a "Scanning…" badge with items processed of
  an estimated total and a percentage that does not reach 100% before the scan finishes

#### Scenario: A stamping or upgrading corpus shows the matching label and exact progress

- **WHEN** a corpus has a stamp backfill or an OTS upgrade pass in progress
- **THEN** the badge SHALL be labelled accordingly ("Stamping…" / "Upgrading proofs…") and SHALL show
  exact progress (processed out of the known total) since those operations know their total up front

#### Scenario: First-ever scan shows an indeterminate badge

- **WHEN** a corpus is being scanned for the first time with no completed scan to estimate from
- **THEN** the badge SHALL show an indeterminate "Scanning…" state with elapsed time and the running
  count, and SHALL NOT display a percentage

#### Scenario: The badge updates itself and stops when the operation finishes

- **WHEN** an operation is in progress and is being shown in the panel
- **THEN** the indicator SHALL update without a manual reload while it runs, and once it finishes the
  indicator SHALL stop updating and resolve to the corpus's normal status

#### Scenario: An idle corpus does not poll

- **WHEN** a corpus has no operation in progress
- **THEN** its status indicator SHALL render statically and SHALL NOT poll for updates

### Requirement: Review and recover changed or missing files

The panel SHALL provide a per-collection **review** view, reachable directly from the dashboard,
that focuses the operator on exactly the files that need attention and tells them what to do next.
The dashboard collection card's issue count (and the collection-detail "changed / missing" stat)
SHALL be a visible, clickable link to that collection's review view. The review view SHALL list
each `missing` file and each WORM `modified` file for the collection, and for each SHALL show what
happened (missing vs modified) with its last-seen / detected time, its size, and whether the file
was notarized. The review view SHALL let the operator mark an individual file's event reviewed, mark
all of the collection's open events reviewed, **accept one file on its own from its row**, and
**accept each of the states it lists as its own action** — adopting the changed files, or stopping
tracking the missing ones — reusing the existing acknowledge/accept behavior. It SHALL NOT offer a
single control that does both, nor one that also acts on files the view does not list. Marking one
or all events reviewed SHALL refresh the "need action" count and the sidebar alert badge in place
without a full page reload; an accept action MAY instead complete as an ordinary form submission
followed by a redirect to a freshly rendered review view. The review view SHALL provide **recovery
guidance that assumes no particular backup tool**: a copyable list of the affected file paths and
tool-neutral recovery instructions; for files that were notarized it SHALL note that their
OpenTimestamps proof of prior existence survives. All review and recovery actions SHALL be scoped to
the current user's own collections.

The controls SHALL be styled by consequence, not by the colour of the condition they refer to: the
mark-reviewed actions, which change nothing about any file, SHALL be the quiet controls, and the
action that removes records SHALL be the loud one. This SHALL hold per row as well as in bulk: a
row's accept control SHALL NOT be visually interchangeable with the mark-reviewed control beside it,
since the two differ by everything that matters. The bulk mark-reviewed action SHALL state how many
alerts it will clear and that nothing about the files changes.

The control for clearing alerts SHALL be rendered whenever the collection has open events, whether
or not any file is currently missing or modified. A file that was missing and has since been
restored leaves its alert open while the file itself is healthy; suppressing the clearing control in
that state leaves a red count with nothing in the interface able to clear it. In that state the view
SHALL name the case and SHALL NOT offer any accept action — every accept action's population is
empty, and an action rendered over an empty population is an invitation to act on nothing.

Where recovery guidance tells the operator what to do about files they do not want back, it SHALL
name the action that stops tracking them, by the label that action actually carries.

Where the list of affected files is truncated, the link to the full set SHALL open the file browser
already switched to the list view and already filtered to issues, rather than a view that discards
both. Where the copyable path list is truncated, the view SHALL point at that same filtered browser
for the full set, and SHALL NOT direct the operator to a command that is not implemented.

Copy-to-clipboard controls SHALL handle a clipboard write being unavailable or rejected, and SHALL
report the failure rather than silently appearing to succeed. This SHALL hold for **every** copy
control the panel renders, including those on the verification result, and the behaviour SHALL be
implemented once and shared rather than reimplemented per surface. Recovery guidance SHALL refer to
the copied list by the control that produced it rather than by its position on the page.

The view's introduction, which describes files that went missing or changed and how to recover them,
SHALL be rendered only where there is something to review. On a view with no issues and no open
alerts it describes a situation the page itself then contradicts.

**Recovery guidance SHALL describe the check the scan actually performs, and both of its outcomes.**
A restored file is compared against the digest recorded for it, so the guidance SHALL say that a
rescan returns a file to a healthy state **only where the restored bytes match what was recorded**,
and that a file which comes back different **closes the now-obsolete missing alert and replaces it
with a new "came back changed" alert**. It SHALL NOT say that the new alert is raised *instead of*
clearing the old one: the scan acknowledges the missing event in the same transaction, so an
operator sent looking for a still-open missing alert is sent after something the scan already
closed.
It SHALL NOT tell the operator that restoring a file and rescanning returns it to a healthy state
unconditionally, and SHALL NOT describe a set of restored files as matching what was recorded unless
that comparison established it. Promising a verification the product does not perform is the failure
class this page exists to close; promising one it does perform, without its negative outcome, is the
same failure with a smaller blast radius.

#### Scenario: Recovery guidance names both outcomes of a rescan

- **WHEN** the review view renders its recovery guidance
- **THEN** it SHALL state that a restored file returns to a healthy state only where its bytes match
  the digest recorded for it, and that a file restored with different bytes closes its obsolete
  missing alert and raises a new "came back changed" alert in its place

#### Scenario: Recovery guidance names the action that stops tracking

- **WHEN** the recovery guidance tells the operator what to do about files they do not want back
- **THEN** it SHALL name the stop-tracking action by its rendered label, and SHALL NOT name a
  control the view no longer offers

#### Scenario: The restored-alerts card does not assert an unperformed match

- **WHEN** the review view renders the card for alerts left open by files that have since been
  restored
- **THEN** its copy SHALL NOT assert that the restored files match what was recorded beyond what the
  scan established

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: The keyboard-activated issue count responds to both activation keys

- **WHEN** the user focuses a dashboard card's issue count and presses Space
- **THEN** the review view SHALL open, as it does for Enter

#### Scenario: The review view's introduction is withheld when there is nothing to review

- **WHEN** the user opens the review view for a collection with no missing or modified files and no
  open alerts
- **THEN** the page SHALL NOT render the introduction describing files that went missing or changed

#### Scenario: A truncated copy list points at the filtered browser

- **WHEN** the review view's copyable path list is truncated
- **THEN** the notice SHALL link to the file browser filtered to issues for the full set, and SHALL
  NOT name an unimplemented command

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with a missing/modified indicator, its last-seen or
  detected time, its size, and whether it was notarized

#### Scenario: Mark a file reviewed from the review view

- **WHEN** the user marks a file's event reviewed from the review view
- **THEN** the event SHALL be recorded acknowledged and the "need action" count and sidebar alert
  badge SHALL refresh in place without a full page reload

#### Scenario: Accept one file from its own row

- **WHEN** the user accepts a single row from the review view
- **THEN** only that file SHALL be acted on, its own events SHALL be acknowledged, and the view
  SHALL refresh to reflect the resolved row

#### Scenario: Scoped accept and mark-all-reviewed from the review view

- **WHEN** the user triggers one of the scoped accept actions or mark-all-reviewed from the review
  view
- **THEN** the action SHALL reuse the existing accept/acknowledge behavior scoped to the user's own
  collections and to that action's own population, and the view SHALL refresh to reflect the cleared
  issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  survives

#### Scenario: Alerts left open by a restored file can be cleared

- **WHEN** the user opens the review view for a collection with no missing or modified files but
  with open events
- **THEN** the view SHALL render the mark-all-reviewed control, naming the case, and SHALL NOT
  render any accept action

#### Scenario: Nothing to review and nothing open

- **WHEN** the user opens the review view for a collection that has indexed files, none of them
  missing or modified, and no open events
- **THEN** the view SHALL render an "all clear" empty state and SHALL offer no review/accept actions

#### Scenario: The truncation link lands filtered

- **WHEN** the affected-file list is truncated and the user follows the link to the full set
- **THEN** the file browser SHALL open in its list view with the issues filter already applied and
  reflected in the filter control

#### Scenario: A failed clipboard write is reported

- **WHEN** a copy-paths action cannot write to the clipboard
- **THEN** the panel SHALL attempt a fallback and, if that also fails, SHALL tell the user the copy
  did not happen rather than showing the success confirmation

### Requirement: Panel address is configurable from the Settings page

The Settings page SHALL present an admin-only "Panel address" field holding the panel's
externally-reachable base URL, saved to the app-settings overlay and used to build the review links
carried by outbound alerts. Non-admin users SHALL see the configured value read-only, consistent
with how the shared SMTP configuration is presented.

Saving SHALL validate the value against the canonical base-URL grammar (see the `configuration`
capability) and SHALL report a clear inline error on rejection, leaving the stored value unchanged.
This is the fail-loud boundary: a human is present to read the error, so an invalid value is
refused here rather than silently ignored. Saving an empty value SHALL **delete** the stored row,
so the environment value becomes visible again.

The page SHALL derive the health-monitoring URL it displays from this setting rather than a
hardcoded address, showing an illustrative example only while the setting is unconfigured, labelled
as an example.

#### Scenario: Admin sets the panel address

- **WHEN** an admin saves `https://cairn.example.com` as the panel address
- **THEN** it SHALL be persisted to the app-settings overlay and subsequent alerts SHALL link to
  `https://cairn.example.com/collection/{id}/review`

#### Scenario: An invalid address is rejected

- **WHEN** an admin saves a value that is not an absolute `http`/`https` URL
- **THEN** the page SHALL show a clear error, the value SHALL NOT be stored, and any previously
  stored value SHALL remain in effect

#### Scenario: Clearing the field falls back to the environment

- **WHEN** an admin saves an empty panel address
- **THEN** the stored row SHALL be deleted and the effective value SHALL come from
  `CAIRN_PUBLIC_URL`, or be unset if that is unset

#### Scenario: The saved address is shown back, normalized

- **WHEN** an admin saves `https://cairn.example.com/` and reloads the Settings page
- **THEN** the field SHALL show the normalized `https://cairn.example.com`, so the operator sees
  exactly what links will be built from

#### Scenario: Non-admins cannot edit it

- **WHEN** a non-admin user opens the Settings page
- **THEN** the panel address SHALL be shown read-only and any attempt to save it SHALL be refused

#### Scenario: The test email exercises the real link

- **WHEN** an admin sends a test email with a panel address configured
- **THEN** the test message SHALL contain a link built from that address, so the operator can
  confirm it is reachable before an incident

### Requirement: Proof coverage is reported as a ratio, never as an unearned completeness claim

The panel SHALL NOT state or imply that a collection's proofs are all confirmed unless there is at
least one confirmed proof and nothing outstanding — no proof queued for stamping, none awaiting
Bitcoin confirmation, and no file eligible for stamping that has not been stamped. Every
completeness claim in this product is read as a statement about the collection, so one computed over
only the files that happen to have been stamped is a false assurance about the rest.

Where a collection has files eligible for stamping that carry no proof, the panel SHALL show
coverage as a ratio of confirmed proofs to stampable files, together with a distinct warning line
naming how many files are not stamped, next to the control that stamps them.

**Every component of a coverage claim SHALL be counted over the same population**: files whose
status is not `missing`, which is the population the stamp-all operation actually queues. Confirmed,
queued, awaiting-confirmation and unstamped counts SHALL all carry that predicate, and the
completeness claim SHALL require the confirmed count to equal a **positive** stampable count.
Counting confirmed proofs over one population and stampable files over another produces a ratio
whose halves describe different sets: a single missing file with a confirmed proof would otherwise
report full coverage of a collection where nothing stampable is confirmed. Excluding missing files
from the unstamped warning is the same rule seen from the other side — including them would produce
a warning that no operator action can ever clear.

**Fleet-wide (dashboard) proof coverage SHALL be computed strictly over collections whose
notarization mode is per-file.** Tripwire-only collections stamp nothing and their stamp operation
refuses them, so their files SHALL appear in neither the numerator nor the denominator of any proof
coverage figure, and the fleet-wide figure SHALL name the population it summarises. Including them
would show an un-clearable "not stamped" count for files no control can act on; removing them from
only one half of the ratio would restore a false completeness claim.

A collection with no indexed files SHALL say so, and SHALL NOT report "all clear", "all files
verified" or "all confirmed". A root that is a typo or a failed mount scans clean forever; a
zero-file collection is a configuration failure to surface, not a healthy state to celebrate. This
SHALL hold on **every** surface that renders the collection's status, including the shared status
indicator used by the dashboard card, the collection detail header, the live-operation fragment and
the review view's nothing-to-review state: a zero-file collection SHALL render a distinct, non-green
"no files indexed" state there rather than the healthy label. Fixing only the tiles leaves the
reassurance where the operator actually looks first. On the review view in particular, "nothing is
missing or changed" is a claim about files that were checked, and a collection with no indexed files
has checked none.

#### Scenario: Unstamped files defeat the completeness claim

- **WHEN** a collection has confirmed proofs but also files eligible for stamping with no proof
- **THEN** the panel SHALL show the confirmed-to-stampable ratio and a warning naming the unstamped
  count, and SHALL NOT say "all confirmed"

#### Scenario: No confirmed proofs

- **WHEN** a collection has no confirmed proof at all
- **THEN** the panel SHALL NOT say "all confirmed"

#### Scenario: Missing files are not counted as unstamped

- **WHEN** a collection's only files without proofs are files recorded `missing`
- **THEN** the unstamped warning SHALL NOT be shown, because the stamp-all operation would not act
  on them

#### Scenario: A confirmed proof on a missing file does not fill the ratio

- **WHEN** a collection's only confirmed proof belongs to a file recorded `missing`, while a present
  file has no confirmed proof
- **THEN** the panel SHALL NOT say "all confirmed", and the ratio SHALL report zero confirmed of the
  present, stampable files

#### Scenario: Everything stamped and confirmed

- **WHEN** every stampable file in a collection has a confirmed proof and none are queued or
  awaiting confirmation
- **THEN** the panel MAY state that all proofs are confirmed

#### Scenario: Tripwire collections are outside the fleet-wide proof figures

- **WHEN** a user has a tripwire-only collection with many present files and a per-file-notarized
  collection whose stampable files are all confirmed
- **THEN** the dashboard's proof coverage SHALL count only the notarized collection, SHALL name the
  population it covers, and SHALL NOT report the tripwire collection's files as unstamped

#### Scenario: A collection with no files

- **WHEN** a collection has no indexed files
- **THEN** the card, the collection view, the review view and the shared status indicator SHALL
  report that no files are indexed yet, in a non-green state, and no surface SHALL report "all
  clear", that all files are verified, or that all proofs are confirmed

#### Scenario: A collection emptied while its review page was being read

- **WHEN** the review view has counted a collection's files and every file record is removed — by a
  re-baseline committed from elsewhere — before the view reads the population it renders
- **THEN** the page SHALL report that no files are indexed yet and SHALL NOT report "all clear"

### Requirement: The collection file browser honours view and filter deep links

The collection detail view SHALL accept optional parameters selecting the browser view (tree or
list) and the status filter, and SHALL render the browser in the requested state on first load —
including applying the requested filter to the initial file query and marking the corresponding
controls active. A link that sets a filter but lands on an unfiltered list with the filter control
showing as checked is worse than no deep link at all, because the operator reads the list as the
filtered set.

Because the filter control is not available in the tree view, requesting a filter other than "all"
without explicitly requesting a view SHALL open the list view. Unrecognized values SHALL fall back
to the defaults, which SHALL reproduce the view rendered when no parameters are supplied.

#### Scenario: Filtered deep link renders filtered

- **WHEN** the collection view is opened with the list view and the issues filter requested
- **THEN** the initial file list SHALL contain only files with issues, the Issues control SHALL be
  marked active, and the List view SHALL be marked active

#### Scenario: A filter alone implies the list view

- **WHEN** the collection view is opened with a filter other than "all" and no view specified
- **THEN** the list view SHALL be rendered, because the filter control is not reachable in the tree
  view

#### Scenario: No parameters reproduce today's view

- **WHEN** the collection view is opened with no parameters, or with unrecognized values
- **THEN** it SHALL render the tree view with no status filter applied

### Requirement: Proof-state vocabulary and verification instructions match what the operator can do

The panel SHALL use one name per notarization state across every surface — badge, tiles, dashboard,
verify verdict and the explanatory documentation — so that a single state is never described by two
different words, and two different states are never described by one.

There are two states before a proof is confirmed and they SHALL be named distinctly: a proof that is
**queued for stamping and not yet submitted** to a calendar, and a proof that has been **submitted
and is awaiting Bitcoin confirmation**. Only the second is waiting on Bitcoin; describing the first
as awaiting confirmation tells an operator to wait for something that has not been started, and
hides a stalled queue behind the wording for a healthy young proof. No surface SHALL apply the
awaiting-confirmation wording to the queued state.

Summaries SHALL NOT add the two together under one label. Where both are non-zero the summary SHALL
name each with its own count; where one is zero it MAY be omitted. The documentation SHALL name each
state as the interface words it.

The notarization vocabulary SHALL NOT be borrowed for the **integrity** result. A collection summary
reporting that its files match the baseline the last scan recorded SHALL NOT describe them as
*verified*: verification is the notary's word for a proof checked against the Bitcoin record, which
no scan performs, and reusing it credits the collection with evidence that was never gathered.

A count of unacknowledged alerts SHALL be named for what clearing it does. Acknowledging is a
reading log — it records that the operator has seen the alert and changes nothing about the file or
the baseline — so the count SHALL NOT be labelled as needing action, which promises a repair the
control does not perform.

The documentation SHALL describe only verification paths an operator can actually complete. It SHALL
lead with the public drag-and-drop verifier, SHALL state that verifying requires **both** the file
and its `.ots` proof — the panel's export serves only the proof, so the file must be supplied
separately — and SHALL state plainly that the command-line path requires a reachable Bitcoin Core
node, which is why the application itself defaults to an explorer lookup. It SHALL NOT offer a
command that exits without having verified anything.

#### Scenario: One name per state

- **WHEN** a proof in either not-yet-confirmed state is displayed anywhere in the panel
- **THEN** it SHALL be described with the wording that state's name uses everywhere else, including
  in the documentation, and the queued state SHALL NOT be described as awaiting confirmation

#### Scenario: A summary does not merge the two not-yet-confirmed states

- **WHEN** a collection has both proofs queued for stamping and proofs awaiting Bitcoin confirmation
- **THEN** the summary SHALL report the two counts under their own names rather than one combined
  "awaiting confirmation" total

#### Scenario: Verification instructions are completable

- **WHEN** the operator reads the documentation's verification section
- **THEN** it SHALL lead with the drag-and-drop verifier, SHALL state that both the file and the
  `.ots` proof are required, and SHALL state that the command-line path needs a Bitcoin node

#### Scenario: No command that verifies nothing

- **WHEN** the documentation describes command-line verification
- **THEN** it SHALL NOT suggest an invocation that skips the Bitcoin check and exits without
  verifying

### Requirement: Environment-only configuration is presented as description, not as a control

The Settings page SHALL NOT render a control that looks interactive for configuration it cannot
change. Where a setting is supplied only by the environment, the page SHALL present it as
descriptive text that names the environment variable(s) involved, marks which value is active, and
notes that changing it requires a restart.

The verification backend SHALL remain environment-only. It SHALL NOT be persisted through the panel
unless the command-line tools read the same override: the panel and the CLI disagreeing about how an
integrity claim was verified is the one disagreement this product cannot tolerate.

#### Scenario: The verification tab is descriptive

- **WHEN** an operator opens the Verification settings tab
- **THEN** the backends SHALL be described as text naming the governing environment variables and
  the restart requirement, with the active one marked, and nothing there SHALL carry an interactive
  affordance

#### Scenario: The verification backend is not persisted from the panel

- **WHEN** the panel presents the verification backend
- **THEN** it SHALL be read-only, sourced from the environment

### Requirement: The panel remains usable at phone widths

The panel SHALL remain legible and operable on a phone-sized viewport, and no element SHALL be
allowed to squeeze a collection's identity to an unreadable width. Alert notifications deep-link
operators straight into the panel, so a phone is a first-class entry point rather than an edge case.

Where a live progress indicator cannot fit alongside the content that identifies what it is
progressing, the indicator's non-essential detail SHALL be dropped rather than the identifying
content. Fixed-width metadata cells SHALL reflow rather than clip their values.

Where dropping non-essential detail is not sufficient — the indicator remains wider than the space
its row can give up — the indicator SHALL be moved out of that row entirely so that the identifying
content occupies a full line of its own. An indicator whose width cannot be negotiated must stop
sharing a row with the name, rather than continue to consume it.

A control whose label cannot fit its container SHALL be allowed to wrap rather than overflow the
container it sits in.

#### Scenario: A running operation does not crush the collection name

- **WHEN** a collection with a running operation is rendered on a viewport narrower than the mobile
  breakpoint
- **THEN** the collection's name SHALL remain readable, with the progress bar omitted if necessary,
  and the progress indicator SHALL occupy its own row rather than share the name's

#### Scenario: A bulk action's label does not push its card off screen

- **WHEN** a bulk acknowledgement control whose label carries a count and a scope is rendered on a
  viewport narrower than the mobile breakpoint
- **THEN** the control SHALL wrap within its container rather than overflow it

#### Scenario: A long root path is readable on a phone

- **WHEN** the collection detail page's root-path cell is rendered on a viewport narrower than the
  mobile breakpoint
- **THEN** the path SHALL wrap rather than be truncated, and SHALL also carry the full value as a
  pointer hint

#### Scenario: Detail metadata reflows instead of clipping

- **WHEN** the collection detail page is rendered on a viewport narrower than the mobile breakpoint
- **THEN** the status metadata cell SHALL reflow to fit its value rather than truncating it

### Requirement: A file that came back different is rendered as an alarm, not as an arrival

The panel SHALL render a `restored_changed` event — a file that was recorded missing and reappeared
with bytes that do not match the digest recorded for it — with its own label and with the visual
weight of the alarming kinds, never with the muted styling used for the informational kinds
(`added`, `restored`, `moved`). It SHALL show that event's recorded detail, which carries both
digests, **in full and at any viewport width** — the detail line is the only surviving record of
the digest the file had before it came back, so it SHALL NOT be truncated, clipped or ellipsized.
The event is the one surface that distinguishes "your file came back" from "something else came
back in its place"; rendering it as an arrival would restore the false reassurance the underlying
fix removes.

The affected file SHALL appear in the collection's review view and count toward its issue count and
status like any other `modified` file, so the collection SHALL NOT read as all clear while it is
unresolved.

#### Scenario: The event feed names a changed reappearance as such

- **WHEN** a scan writes a `restored_changed` event and the operator views the event feed
- **THEN** the row SHALL carry a label distinct from `restored`, SHALL be styled as an alarming
  kind, and SHALL show the recorded detail carrying both digests, with both digests fully visible
  (the detail line wraps rather than truncating)

#### Scenario: A collection with an unresolved changed reappearance does not read as all clear

- **WHEN** a collection's only non-`ok` file is one that reappeared with different bytes
- **THEN** the collection SHALL NOT be presented as all clear, and the file SHALL appear in its
  review view

### Requirement: Accept is offered as scoped actions named for their consequence

The panel SHALL NOT offer a single accept control that acts on more than one of the three
populations — files not yet baselined, files whose contents changed, files that are gone. It SHALL
offer a separate action per population, each labelled with **what it does and how many records it
does it to**, and each SHALL act on its own population only.

One control acting on three populations cannot be labelled honestly. The operator on the review
view, which lists missing and modified files, is shown a count of what is wrong and one button; that
button also promotes every not-yet-baselined file in the collection — files the view never displayed
— and there is no way back. Naming the verb after its consequence is what makes the count on the
button and the list on the page the same statement.

The three actions SHALL be, with their populations and their relative loudness:

| Population | Action | Loudness |
|---|---|---|
| files not yet baselined | baseline them — they become the expected version | quiet; a light confirmation |
| files whose contents changed | adopt the current contents as correct from now on | subdued, but its own confirmation |
| files that are gone | stop tracking them — their records are removed | the loud, dangerous one; its own confirmation |

Each SHALL carry a hint stating, in the operator's terms, what is kept and what is lost:

- Baselining folds files Cairn found but the operator has not vouched for into the expected set.
  They are already watched and already notarized; it clears the "new" marker and nothing else.
  There is no un-baseline.
- Adopting treats the current contents as correct from now on. Cairn recorded and notarized the new
  contents when it detected the change; this stops the alert, and this change will not be reported
  again.
- Stopping tracking permanently removes those files from Cairn's records — their paths, their
  first-seen dates, and the link to their timestamp proofs. **Nothing on disk is deleted and the
  proof files stay in the proof store**, but Cairn will no longer list those files under Verify. To
  get them back the operator restores from backup and runs a scan.

Two claims SHALL NOT be made anywhere in this vocabulary, because both are false and both are
plausible enough to be re-derived:

- That adopting a change is **alert-only or reversible by a rescan**. The scan already recorded the
  new digest, so a rescan matches it and re-raises nothing. The change is gone from the record.
- That stopping tracking **deletes the files' proofs**. No proof file is deleted by any path in the
  product. What is lost is the *link* from Cairn's records to the proof.

Each action's count SHALL be derived from the **same snapshot the page's list is rendered from**, so
the number on the button and the rows on the page cannot disagree. An action whose population is
empty SHALL NOT be rendered.

The panel SHALL additionally offer a **per-file** accept control on each reviewable row, so that a
scan reporting one legitimate edit and one suspicious deletion can be resolved one file at a time.
The per-file control SHALL be distinguishable at a glance from the row's mark-reviewed control,
which changes nothing about the file, and SHALL carry its own confirmation naming that one file.

#### Scenario: Each action names its own population and count

- **WHEN** the review view is opened for a collection with changed and missing files
- **THEN** it SHALL render one action per non-empty population, each naming what it does and the
  number of records it acts on, and SHALL NOT render a single control that acts on more than one of
  them

#### Scenario: An empty population is not offered an action

- **WHEN** the review view is opened for a collection that has missing files but no modified files
- **THEN** the adopt action SHALL NOT be rendered

#### Scenario: Button counts agree with the list

- **WHEN** the review view renders its actions
- **THEN** each action's count SHALL come from the same read as the rows it lists

#### Scenario: The forbidden reassurances are not rendered

- **WHEN** any accept action, hint, confirmation or recovery instruction is rendered
- **THEN** it SHALL NOT describe adopting a change as reversible by a rescan, and SHALL NOT state
  that timestamp proofs are deleted

#### Scenario: A single row is resolved on its own

- **WHEN** the operator uses a row's own accept control on a collection with other reviewable rows
- **THEN** only that file SHALL be acted on, and every other row's record and alert SHALL be
  unchanged

### Requirement: Fleet-wide review of every collection's open issues

The panel SHALL serve a fleet-wide review page listing every missing or modified file across all of
the collections the current user owns, grouped by collection, so that a fleet-wide issue count has a
destination that can act on it. Collections with no missing or modified files SHALL NOT be listed.

Each group SHALL name its collection, state **both** that collection's missing and modified counts —
including a count that is zero, since an unstated count cannot be told apart from an absent one, and
the two kinds carry different consequences — show its count of unreviewed events, and offer a link to
that collection's own review view. A group rendered with **no** rows because the page-wide render
budget was already spent SHALL say that is what happened and point at the collection's own review
view for its issues, rather than describing itself as showing the first none of them. Within a group,
missing files SHALL be listed before modified files. Groups SHALL be ordered worst-first (by missing
count, then modified count, then name).

Every number a group shows, every action a row authorises **and the open-event state each row
displays and acts on** SHALL be derived from **one read** of that collection's review population —
the same single-snapshot property the per-collection review view depends on, because a control
authorised against a population the page did not display is the failure that guard exists to
prevent. In particular the row's open event SHALL come from that same read rather than from a second
query: a scan or an acknowledgement committing between two reads would otherwise let a row's
"mark reviewed" control and its accept fingerprint describe two different states of the same file.

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

#### Scenario: A row's event and its fingerprint come from the same read

- **WHEN** an event is inserted or acknowledged for one of the listed files while the fleet-wide page
  is being rendered
- **THEN** the row's displayed reviewed state and the fingerprint its accept control carries SHALL
  both describe the single population read that produced the row, and SHALL NOT be drawn from two
  different snapshots

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

#### Scenario: The return destination is chosen from a fixed set, never supplied

- **WHEN** a per-file accept is submitted carrying a return-destination value that is not the
  fleet-page literal — including an absolute URL to another host, a scheme-relative or
  protocol-bearing string, or any other arbitrary text
- **THEN** the request SHALL still perform exactly the same scoped, fingerprint-guarded accept, and
  SHALL redirect to the collection's own review view; no supplied value SHALL ever become part of
  the redirect target

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

Every health figure the **panel** renders — the indicator's verdict, its count, and the per-card
stale markers — SHALL be computed over the **current user's own collections** only. The indicator
names a number and then sends the operator to a list where the collections behind that number are
supposed to be identified; a fleet-global count rendered above an owner-scoped list is a number with
no referent on the page it links to, which is the same "computes one thing, shows another" defect
this indicator is being fixed for. The machine-facing `GET /healthz` endpoint SHALL remain
fleet-global (see the app-runtime capability): it monitors the installation, and the two surfaces
answer different questions.

The indicator SHALL NOT assert a health verdict it has not computed. Before the first health poll of
a page has answered, it SHALL render a neutral "checking" state; it SHALL NOT render a healthy state
as a placeholder.

The indicator SHALL also fail **closed**: a health poll that does not produce a verdict SHALL NOT
leave the previous verdict displayed. Where the failure is reachable by the server (the health
computation itself raising), the poll SHALL answer with a rendered non-healthy "health check failed"
state rather than an error status, since an error status is not swapped into the page. Where it is
not (a failing request dependency, a transport error, a timeout), the rendered indicator SHALL carry
a client-side error hook that replaces the standing verdict with the same failed state. A previously
healthy indicator surviving a datastore or transport outage is the same fabricated assurance as a
placeholder verdict, and a more durable one, because every subsequent poll fails the same way. There SHALL be exactly one health indicator implementation in the panel: no page
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

#### Scenario: A failed health poll does not leave a health claim standing

- **WHEN** the indicator has rendered a healthy verdict and a subsequent poll fails, whether because
  the health computation raises or because the request never reaches the handler
- **THEN** the indicator SHALL stop claiming health and SHALL show a non-healthy "health check
  failed" state until a poll produces a verdict again

#### Scenario: The stale collection is identified where the link lands

- **WHEN** the operator follows the degraded health indicator to the collections list
- **THEN** the stale collection's card SHALL carry a marker naming its scan as overdue

#### Scenario: The panel's health is scoped to the viewer

- **WHEN** another user's collection is stale in multi-user mode while every collection the viewer
  owns is fresh
- **THEN** the viewer's health indicator SHALL report healthy and none of the viewer's collection
  cards SHALL carry a stale marker, while `/healthz` SHALL still report the installation degraded

#### Scenario: A card is matched to its freshness by identity, not by name

- **WHEN** two collections owned by different users share the same name and one of them is stale
- **THEN** the stale marker SHALL appear only on the card of the collection whose identifier the
  freshness record carries

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

The panel's **"last scan"** claim SHALL continue to be derived from **completed** scan runs only
(`ok` or `partial`), so that a run which never finished cannot refresh a statement about when the
collection was last checked. This is deliberately narrower than dead-man's-switch freshness, which
additionally honours a scan that is in flight and demonstrably alive (see the app-runtime
capability): the switch answers "is this collection still being watched", while "last scan" answers
"when did a scan last finish", and only the second may be refreshed by a run that has not finished.
Separately from both, where a collection's newest scan run ended `interrupted` or `error` more
recently than its last completed scan, the panel SHALL disclose it.

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

