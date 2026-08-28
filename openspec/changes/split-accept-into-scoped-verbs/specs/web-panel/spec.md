# web-panel Specification (delta)

## ADDED Requirements

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

## MODIFIED Requirements

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
