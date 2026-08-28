# web-panel Specification (delta)

## MODIFIED Requirements

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
  to commit to exactly that recorded provenance — so the proof at that path is the one the system
  recorded placing, made from earlier bytes — the panel SHALL report as established that the proof
  **predates this version** of the file and SHALL state that this is not evidence against the
  current file. It SHALL claim a re-stamp is pending only where the record indicates one is owed;
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

#### Scenario: Recorded provenance establishes that the proof predates this version

- **WHEN** the panel verifies a file whose live bytes still hash to the digest Cairn recorded for it,
  whose recorded proof provenance differs from that digest, and whose stored proof commits to exactly
  that recorded provenance
- **THEN** the panel SHALL report as established that the proof predates this version of the file,
  SHALL state that this is not evidence against the current file, and SHALL claim a re-stamp is
  pending only if the record indicates one is owed

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
and that a file which comes back different raises a **new** alert rather than clearing the old one.
It SHALL NOT tell the operator that restoring a file and rescanning returns it to a healthy state
unconditionally, and SHALL NOT describe a set of restored files as matching what was recorded unless
that comparison established it. Promising a verification the product does not perform is the failure
class this page exists to close; promising one it does perform, without its negative outcome, is the
same failure with a smaller blast radius.

#### Scenario: Recovery guidance names both outcomes of a rescan

- **WHEN** the review view renders its recovery guidance
- **THEN** it SHALL state that a restored file returns to a healthy state only where its bytes match
  the digest recorded for it, and that a file restored with different bytes raises a new alert

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

### Requirement: A file that came back different is rendered as an alarm, not as an arrival

The panel SHALL render a `restored_changed` event — a file that was recorded missing and reappeared
with bytes that do not match the digest recorded for it — with its own label and with the visual
weight of the alarming kinds, never with the muted styling used for the informational kinds
(`added`, `restored`, `moved`). It SHALL show that event's recorded detail, which carries both
digests, in the same place the panel already shows a moved file's old → new path. The event is the
one surface that distinguishes "your file came back" from "something else came back in its place";
rendering it as an arrival would restore the false reassurance the underlying fix removes.

The affected file SHALL appear in the collection's review view and count toward its issue count and
status like any other `modified` file, so the collection SHALL NOT read as all clear while it is
unresolved.

#### Scenario: The event feed names a changed reappearance as such

- **WHEN** a scan writes a `restored_changed` event and the operator views the event feed
- **THEN** the row SHALL carry a label distinct from `restored`, SHALL be styled as an alarming
  kind, and SHALL show the recorded detail carrying both digests

#### Scenario: A collection with an unresolved changed reappearance does not read as all clear

- **WHEN** a collection's only non-`ok` file is one that reappeared with different bytes
- **THEN** the collection SHALL NOT be presented as all clear, and the file SHALL appear in its
  review view
