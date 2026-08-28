# web-panel Specification (delta)

## MODIFIED Requirements

### Requirement: Verify a tracked file's proof from the panel without upload

The verify page SHALL let the user search files Cairn already tracks (no file upload) and verify a
selected file by re-hashing it from the read-only store and checking the stored `.ots` proof. The
result SHALL present the verdict and, when complete, the SHA-256, the existed-by date, and the
Bitcoin block, plus an option to export the portable bundle. A complete "Anchored" badge elsewhere
SHALL deep-link here and verify immediately.

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

A file whose bytes no longer match the digest its proof commits to SHALL be rendered as a failure
that names that fact — it SHALL NOT be rendered as a proof awaiting confirmation, and SHALL NOT be
accompanied by copy telling the operator to wait. This is the product's core detection; presenting
it as a young proof is a false negative on the one claim Cairn exists to make.

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

Every list of already-anchored files SHALL render each row's **actual** notarization state. A row
SHALL NOT be given a fixed "Anchored" badge: the badge is what an operator reads to decide whether a
proof is usable as evidence, and the list is ordered newest-first, so a hardcoded badge shows the
least-confirmed proofs as the most confirmed. The page SHALL render a distinct empty state when
nothing is anchored yet, separate from the empty state for a search that matched nothing.

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

#### Scenario: The anchored list shows real proof states

- **WHEN** the verify page lists recently anchored files that include a not-yet-confirmed proof
- **THEN** that row SHALL show the not-yet-confirmed badge, not "Anchored"

#### Scenario: Nothing anchored yet

- **WHEN** the user opens the verify page and no file has been stamped
- **THEN** the page SHALL render an empty state saying so, distinct from the "no search matches"
  message

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
collection's review view; when more than one is affected it SHALL link to the collections list. It
SHALL NOT link to a route that does not exist. At a count of zero it SHALL remain a non-interactive
element.

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
- **THEN** the Open issues tile SHALL link to the collections list, and SHALL NOT link to a
  fleet-wide review route that does not exist

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

### Requirement: Accept and scan actions are available from the panel

The collection detail page SHALL offer "Scan now", and SHALL offer a re-baseline action only in the
state where that action is harmless. Both mutate via the existing services and leave the operator on
an up-to-date view of the collection; the re-baseline action MAY do so by an ordinary form
submission followed by a redirect to a freshly rendered page rather than an in-place refresh.

Re-baselining is irreversible: it rewrites the expected version of modified files and removes the
records of missing ones. The collection detail page SHALL NOT present it as the page's primary
action while the collection has missing or modified files. In that state the primary action SHALL be
a link to the review view, which explains the choice between noting a change and adopting it; the
re-baseline action SHALL be reachable only from there. When the collection has no missing or
modified files, **no open (unreviewed) events**, and does have files that are merely not yet
baselined, the page MAY offer that harmless baseline action directly, and it SHALL require a
confirmation naming what it will do. Any re-baseline form rendered on this page SHALL carry a
confirmation.

Both conditions are required because the re-baseline verb acts on two populations: it promotes
not-yet-baselined files **and** it marks every open event on the collection reviewed. A file that
was missing and has since been restored is healthy again while its alert remains open, so a
collection can have no missing or modified files and still have an unread alert; offering the
"harmless" baseline action there would clear that alert behind a confirmation that describes only a
new-file promotion. Where the collection has open events, the primary action SHALL remain the link
to the review view, which is where those events can be read and cleared deliberately.

Choosing what to render is not sufficient, because the collection can change between the page being
rendered and the form being submitted. **Every endpoint that re-baselines a collection SHALL be
bound to the population its form was rendered for** — the collection-detail baseline action and the
review view's accept alike. Neither is exempt: a rendered list is a statement about the past, so a
scan that records another missing file after the render makes "these are the files this button will
adopt" false, and the operator removes a record they never saw.

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
rewritten while the record survives. For the same reason the fingerprint SHALL be scoped by the collection's creation time as
well as its identifier, so that a recreated collection reusing an identifier cannot validate a
fingerprint issued for its predecessor. The fingerprint SHALL also be scoped so that a fingerprint
issued for one form cannot validate another, and its encoding SHALL be unambiguous for every legal
file path, so that two different populations cannot be encoded to the same input.

The fingerprint SHALL cover the **whole protected population its form is rendered for**, rather than
any truncated list the page displayed: for the review view's accept, the collection's entire
`missing` + `modified` set; for the collection-detail baseline action, its entire `new` set together
with the assertion that the collection has no missing or modified files. An absent or empty
fingerprint SHALL be treated as a mismatch, so the check fails closed.

The population a form's fingerprint is computed over and the population the page **renders** SHALL
be derived from a **single, consistent read** — one snapshot covering the file records, the open-event
set and any count the form asserts — rather than from separate reads that a concurrent writer can
interleave. A page that reads its visible rows in one query and computes the fingerprint in another
can publish a fingerprint for a population it never displayed, and an unchanged submission of that
form would then validate and destroy a record the operator was never shown — the exact accident the
guard exists to prevent, reintroduced inside the guard's own mint. The same single-read derivation
SHALL be used when the endpoint recomputes the fingerprint, so the two sides cannot encode the same
state differently.

Because the same verb also marks every open event on the collection reviewed, the fingerprint SHALL
additionally cover the collection's **set of open (unreviewed) events by identity** — each event's
identifier, its kind and the time it was detected — recomputed inside the same guarded transaction
as the file records, so that any change to *which* alerts are open between render and submit is a
refusal. Without an event term at all, a form can be validated by a population that has returned to
its rendered value while an alert the operator never saw is silently cleared: a file may be recorded
modified, then missing, then restored between render and submit, leaving the protected file set
exactly as rendered while its alert stays open by design. Covering that set by **count** is not
sufficient: an open alert may be acknowledged and a different one opened on the same file with the
same kind, returning the count to what it was while the incident the operator is being asked to
clear is a different one. Event identifiers may themselves be reused after a record is deleted, so
identity alone is not sufficient either — hence the detection time, which separates one generation of
an alert from the next. For the collection-detail baseline action that set SHALL be empty, and the
fingerprint SHALL carry that emptiness assertion, so the action cannot clear an alert behind a
confirmation that promises only a new-file promotion.

The review view's accept also promotes files that are merely **not yet baselined**, and that set is
deliberately **outside** the fingerprint: a new file appearing between render and submit SHALL NOT
cause a refusal, and SHALL be baselined by the accept along with the rest. This is an accepted
limitation, not an oversight. The guard's purpose is the *destructive* half — the missing records the
accept removes and the alerts it clears — and a collection that is actively growing (a scan every
few minutes adding photos) would otherwise refuse every accept over a promotion that deletes
nothing, which would train the operator to work around the guard. Narrowing the re-baseline verb so
it acts only on what was displayed is separate work. The open-event term does not reintroduce that
refusal: events recording a merely-added file are written already reviewed, so a file first seen
between render and submit does not change the collection's open-event count.

The recomputation and the re-baseline SHALL happen **within a single write transaction**, entered
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
renders as an explanation** that the collection changed since the page loaded and that the list
shown is current. A refusal with no visible explanation is indistinguishable from a broken button
and invites the operator to click it again.

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

#### Scenario: The review view's accept is refused after its list goes stale

- **WHEN** the user opens the review view listing one missing file, a scan then records a second
  file missing, and the user submits the already-rendered accept form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove either missing file's record or acknowledge
  either event, and SHALL redirect to the review view with the marker that makes the refusal visible

#### Scenario: A refused submission is explained on the review view

- **WHEN** the review view is opened with the staleness marker a refused re-baseline redirects to
- **THEN** the view SHALL render a dismissable notice saying the collection changed since the page
  loaded and that the list shown is current, and SHALL NOT render that notice on an ordinary visit
  or for an unrecognized marker value

#### Scenario: A submission with no fingerprint is refused

- **WHEN** a re-baseline is submitted without the population fingerprint, or with an empty one
- **THEN** the endpoint SHALL refuse and SHALL NOT re-baseline anything

#### Scenario: A reused row identifier does not validate a stale fingerprint

- **WHEN** the review view is rendered for a collection, the missing file it listed is then removed
  from the datastore and a scan records a **different** file, at a different path, whose row reuses
  the removed record's identifier and which is itself missing, and the operator submits the
  originally rendered form
- **THEN** the endpoint SHALL refuse, SHALL NOT remove the replacement file's record or acknowledge
  its event, and SHALL redirect to the review view with the staleness marker — a reused identifier
  SHALL NOT be sufficient for a fingerprint to still match

#### Scenario: An unchanged file set does not validate a form once a new alert exists

- **WHEN** the review view is rendered for a collection, a *different* file is then recorded
  modified, recorded missing, and restored — so the collection's missing + modified set returns to
  exactly the set the form was rendered for while that file's alert remains open — and the operator
  submits the already-rendered accept form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened event, SHALL NOT remove
  any file record, and SHALL redirect to the review view with the staleness marker

#### Scenario: Replacing one open alert with another does not validate a stale form

- **WHEN** the review view is rendered for a collection with one missing file and one open alert on
  it, that alert is then acknowledged and a **new** alert of the same kind is opened on the same
  file — so the count of open alerts returns to exactly what it was while the file records never
  move — and the operator submits the already-rendered accept form
- **THEN** the endpoint SHALL refuse, SHALL NOT acknowledge the newly opened alert and SHALL NOT
  remove the file's record

#### Scenario: A record that appears after the page's read is in neither the list nor the fingerprint

- **WHEN** an accept-family page is rendered and a scan commits a further missing file, and its
  alert, immediately after the page has read the population it renders
- **THEN** the page SHALL neither display that record nor include it in the fingerprint it
  publishes, and submitting that form SHALL be refused because the endpoint's own read now sees it

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

- **WHEN** a re-baseline is submitted while another writer holds the datastore's write lock, or has
  committed since this request's read snapshot, so that the endpoint cannot take the write lock its
  check-and-act depends on
- **THEN** the endpoint SHALL refuse exactly as it does on a fingerprint mismatch — mutating
  nothing and redirecting to the review view with the staleness marker — and SHALL NOT return a
  server error

#### Scenario: A datastore failure that is not contention is not reported as staleness

- **WHEN** the re-baseline endpoint's datastore access fails for a reason that is not write-lock
  contention
- **THEN** that failure SHALL surface as the error it is, and SHALL NOT be presented to the operator
  as the collection having changed since the page loaded

#### Scenario: Accepted limitation — a new file appearing after render does not refuse the accept

- **WHEN** the review view's accept form is submitted after a scan has added a file that is merely
  not yet baselined, while the collection's missing and modified files are exactly what the view
  listed
- **THEN** the endpoint SHALL accept rather than refuse, and that new file SHALL be baselined along
  with the rest; the response is the view's ordinary post-accept redirect, and no additional notice
  is claimed for the file that appeared

#### Scenario: A re-baseline is refused while an operation is in flight

- **WHEN** the user submits a re-baseline form while a scan or stamp operation is running on that
  collection
- **THEN** the endpoint SHALL refuse rather than re-baseline against a population that is still
  changing

#### Scenario: Accept changes from the review view

- **WHEN** the user accepts changes from the collection's review view and the population it listed
  has not changed since the page was rendered
- **THEN** the collection SHALL be re-baselined (new/modified → ok, missing removed, events
  acknowledged) and the stat row + table SHALL refresh — the guard SHALL NOT stand in the way of an
  accept that is still acting on what it displayed

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
all of the collection's open events reviewed, and accept (re-baseline) the collection, reusing the
existing acknowledge/accept behavior. Marking one or all events reviewed SHALL refresh the "need
action" count and the sidebar alert badge in place without a full page reload; the accept
(re-baseline) action MAY instead complete as an ordinary form submission followed by a redirect to a
freshly rendered review view. The review view SHALL provide **recovery guidance that assumes
no particular backup tool**: a copyable list of the affected file paths and tool-neutral recovery
instructions; for files that were notarized it SHALL note that their OpenTimestamps proof of prior
existence survives. All review and recovery actions SHALL be scoped to the current user's own
collections.

The controls SHALL be styled by consequence, not by the colour of the condition they refer to: the
per-file mark-reviewed action, which changes nothing about any file, SHALL be the quiet control, and
the bulk accept, which rewrites baselines and removes records, SHALL be the loud one. The bulk
mark-reviewed action SHALL state how many alerts it will clear and that nothing about the files
changes.

The control for clearing alerts SHALL be rendered whenever the collection has open events, whether
or not any file is currently missing or modified. A file that was missing and has since been
restored leaves its alert open while the file itself is healthy; suppressing the clearing control in
that state leaves a red count with nothing in the interface able to clear it. In that state the view
SHALL name the case and SHALL NOT offer the accept action — from a view listing no issues, accept
would silently baseline every not-yet-baselined file in the collection.

Where the list of affected files is truncated, the link to the full set SHALL open the file browser
already switched to the list view and already filtered to issues, rather than a view that discards
both. Copy-to-clipboard controls SHALL handle a clipboard write being unavailable or rejected, and
SHALL report the failure rather than silently appearing to succeed.

#### Scenario: Dashboard issue count links to the review view

- **WHEN** a collection has one or more missing or modified files and the user views the dashboard
- **THEN** the card's issue count SHALL be a visibly clickable link that opens that collection's
  review view

#### Scenario: Review view lists what happened to each file

- **WHEN** the user opens the review view for a collection with missing and/or modified files
- **THEN** each affected file SHALL be listed with a missing/modified indicator, its last-seen or
  detected time, its size, and whether it was notarized

#### Scenario: Mark a file reviewed from the review view

- **WHEN** the user marks a file's event reviewed from the review view
- **THEN** the event SHALL be recorded acknowledged and the "need action" count and sidebar alert
  badge SHALL refresh in place without a full page reload

#### Scenario: Bulk accept and mark-all-reviewed from the review view

- **WHEN** the user triggers accept or mark-all-reviewed from the review view
- **THEN** the action SHALL reuse the existing accept/acknowledge behavior scoped to the user's own
  collections, and the view SHALL refresh to reflect the cleared issues

#### Scenario: Recovery guidance is offered without assuming a backup tool

- **WHEN** the user views a collection with missing or modified files
- **THEN** the review view SHALL offer a copyable list of the affected paths and tool-neutral
  recovery instructions, and SHALL note for any notarized file that its proof of prior existence
  survives

#### Scenario: Alerts left open by a restored file can be cleared

- **WHEN** the user opens the review view for a collection with no missing or modified files but
  with open events
- **THEN** the view SHALL render the mark-all-reviewed control, naming the case, and SHALL NOT
  render the accept action

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

## ADDED Requirements

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

#### Scenario: A running operation does not crush the collection name

- **WHEN** a collection with a running operation is rendered on a viewport narrower than the mobile
  breakpoint
- **THEN** the collection's name SHALL remain readable, with the progress bar omitted if necessary

#### Scenario: Detail metadata reflows instead of clipping

- **WHEN** the collection detail page is rendered on a viewport narrower than the mobile breakpoint
- **THEN** the status metadata cell SHALL reflow to fit its value rather than truncating it
