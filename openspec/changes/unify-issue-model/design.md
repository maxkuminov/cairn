## Context

The panel was hardened over several audit rounds whose brief was "no false reassurance". Each round
added precise copy and a guard, and the result is accurate and hard to use: two counters, two verb
families, and explanations in front of the data. The earlier spec text deliberately enshrined the
split ("the vocabulary SHALL distinguish the two state machines the panel tracks"). This change
reverses that decision with the owner's agreement, because the split is an artefact of the data
model rather than something the operator needs to hold:

- `files.status` (`ok` / `new` / `modified` / `missing`) is the only state that drives the status
  pills, the sidebar badge and the "open issues" numbers.
- `events.acknowledged_at` drives the "unreviewed" numbers and nothing else.
- For a `modified` file the scanner has already overwritten `files.sha256` at detection
  (`scanner.py`, the `sha != row.sha256` branch). Accepting sets `status='ok'` and acknowledges the
  file's events; marking reviewed only acknowledges. Neither changes what later scans compare to.
- On the reference deployment no file is in the "acknowledged but still an issue" state: the
  acknowledge-only verb is used as a step on the way to accepting, not as a destination.

What must survive: the population-fingerprint guard on every accept-family endpoint (design D14 of
`fix-ux-audit-sprint1`, extended by `split-accept-into-scoped-verbs`), the owner scoping, the
honest-claims rules about what accepting does and does not do, the run-health and proof-coverage
wording, and the Slate visual language.

## Goals / Non-Goals

**Goals:**

- One thing to count and one way to resolve it, named for what happens.
- A review page where the first thing on screen is the files, grouped the way the incident
  happened (by folder, with when it was found), with one action per group.
- A dashboard that answers "does anything need me?" first.
- Keep every submit-time safety property of the accept guard, including for the new folder scope.

**Non-Goals:**

- Changes to detection, alert dispatch or the schema (follow-on change). The one scanner addition
  is D12, which this change needs in order to remove the event count safely.
- Rules, history, per-file pages (follow-on change).
- Rewording the verify card.

## Decisions

### D1 — The issue is the only open item

An issue is a file with `status in ('modified','missing')`. `_alert_badge_count` (already the single
definition of the sidebar badge) is the single definition of "open issues" for every surface. No
template renders a count of unacknowledged events, and no route's only effect is to acknowledge one.

`events.acknowledged_at` stays in the schema and keeps being written by `accept_*` and by the
scanner's restore path. It now means "the issue this event belongs to was resolved (or the event was
informational)".

That only holds if every open *change* alert has an issue behind it, and today two scanner paths
break it: a `modified` file that goes missing and comes back with the bytes on record (the restore
branch sets `ok` and closes only the `missing` events), and a `modified` file later reconciled as a
move (`_reconcile_moves` sets `ok`). Both leave an unaccepted change on an `ok` file, and the only
trace is the open `modified` event — which today shows as an "unreviewed" count with no row behind
it (the reference deployment has exactly one). With that count gone the file would read all clear.

So the scan enforces the invariant itself (**D12**): before it commits its final batch, every file
of the collection that carries an open `modified` or `restored_changed` event and whose status is
`ok` or `new` is set back to `modified`. It is one read of the affected ids plus an in-memory status
write on the rows the scan already holds; it writes no event and raises no alert (the alert went out
when the change was found). It runs after the missing sweep, move reconciliation and auto-baseline,
so none of them can undo it, and it heals data left by earlier versions on the first scan after
deploy. *Alternatives considered:* (a) keep a "dismiss" control for the leftovers — the acknowledge
verb again; (b) widen the panel's definition of an issue to "status or open change event" — that
puts a second definition into every count, the population read and the fingerprint; (c) patch the
two branches — correct today, silent again the next time a third path appears. An open `missing`
event needs no such rule: the restore path closes it in the same transaction that sets `ok`.

### D2 — Verbs and their styling

| File state | Row action | Bulk action | What it does |
|---|---|---|---|
| `modified` | Accept change | Accept N changes | `status → ok`, the file's open events acknowledged |
| `missing` | Accept deletion | Accept N deletions | events detached (path kept), record deleted |

Hints, in the operator's terms:

- *Accept change* — "This is the file now. Cairn recorded the new contents when it found the
  change; accepting clears the issue and this change is not reported again." Nothing here claims a
  proof: stamping is queued only in notarized collections and can still be pending, and the row's
  proof badge is what reports it.
- *Accept deletion* — "Cairn forgets the file: its record, first-seen date and the link to its
  timestamp proof are removed. Nothing on disk is touched and the proof file stays in the proof
  store, but the file is no longer listed under Verify."

Both keep a confirmation naming the file or the count. Styling is by consequence *and* quiet by
default: a row's control is a small outline button whose text carries the kind's colour (amber for a
change, red for a deletion); a group's bulk control is the same shape with the count; nothing is a
filled red button repeated down a column. The page-level bulk controls sit in one toolbar.
*Alternative considered:* keep "Stop tracking". Rejected for symmetry — the operator's question is
the same for both kinds ("was that expected?"), and "Accept deletion" plus its hint states the
record removal just as plainly.

### D3 — Routes removed, not redirected

`POST /events/{id}/ack`, `POST /events/ack-all`, `POST /collection/{id}/review/ack-all` and
`POST /collection/{id}/accept` are deleted. Following the precedent set when `review/accept` was
retired, nothing forwards: a stale tab gets 404/405 and nothing is mutated. The `baseline-new`
fingerprint form, its read scope and its "whole-collection open-event" exception go with the route.

### D4 — "New" is informational

A `new` file is watched and stamped from first sight; the status only records that the weekly deep
pass has not yet re-read it. The panel stops presenting it as work: no tile, no button, no form
control. The collection card keeps its status bar and "N new" legend (they describe, they do not
ask), and the file browser keeps its `new` filter so the files can still be listed. The collection
create route sets `auto_baseline_new=True`; the update route no longer reads the field (so an
existing collection keeps whatever it has). *Alternative considered:* have the scanner write `ok`
directly. That is a scanner change; it belongs to the follow-on.

### D5 — Policy names

`worm` renders as **Locked** ("nothing here should change — every change or deletion is flagged"),
`churn` as **Working** ("edits are normal and are not flagged — deletions are, and so is a file that
comes back different after going missing"). One helper maps the stored
value to label and description; templates never print the stored value. `cairn add-collection --mode`
accepts `locked` / `working` and normalizes to the stored values.

### D6 — Folder scope for the bulk verbs

A **scope** is `(dir, deep)`: `dir` is a collection-relative directory path with no leading or
trailing slash (`""` = the collection root); `deep` selects the whole subtree (`1`) or only the files
directly in `dir` (`0`). One pure predicate, `_in_scope(relpath, dir, deep)`, is the only place the
rule exists:

- `dir == ""`, deep → every path; not deep → paths containing no `/`.
- otherwise the path must start with `dir + "/"`; not deep → and the remainder contains no `/`.

The two bulk forms (`adopt-changed`, `stop-tracking` — route paths unchanged) post `dir` and `deep`
as hidden fields. They are operator-controlled bytes, so they are **bound, not trusted**:

- `_narrow` slices the wide read by status **and** scope; the fingerprint header gains
  `dir={byte length}US{dir}USdeep={0|1}` for these two forms (always, including the root scope), so
  a fingerprint minted for one folder cannot validate a submission naming another even where the two
  populations coincide.
- The recount under the write lock uses the *submitted* scope. If it does not reproduce the
  submitted fingerprint the request is refused like any other drift.
- After the comparison succeeds the route hands the service **the ids of the records the recount
  just hashed** — `accept_files(session, collection, file_ids, user_id)` — instead of asking it to
  re-select by status. The service re-asserts collection and status per statement. There is then
  exactly one encoding of "which records" (the Python slice that was hashed); a SQL re-expression of
  the folder rule that could disagree with it never exists. *Alternative considered:* add a
  `relpath LIKE` filter to `accept_collection`. Rejected: two encodings of one rule on a destructive
  path, and `LIKE` needs escaping for `%` / `_` in real folder names.

`dir` is validated only as far as it is used: it is compared against stored relpaths and echoed
back URL-encoded as a query value on a fixed path. It never reaches the filesystem and never forms
a redirect host or path. A `dir` longer than 4096 bytes or containing a NUL is refused as stale.

The per-file form is unchanged.

### D7 — Grouping

Given the issue files in the page's scope, let `A` be their deepest common directory (never above
the page scope). If every file sits directly in `A` the page renders one flat list. Otherwise it
renders one group per immediate child directory of `A` that contains issues (scope `(child, deep)`),
plus — when some files sit directly in `A` — one group for those (scope `(A, not deep)`).

Each group shows its path, counts per kind, when its issues were found (one date, or a range), one
bulk action per kind present, and its rows. A group of up to 8 files is open; a larger one is a
collapsed `<details>` holding up to 200 rows and a link that opens the group as the page's own
scope (`?dir=…` plus `deep=0` for a direct-files group, so the link never widens what the group
held), where the same rule groups one level deeper. Groups are ordered newest finding first, then
by path. The page-level toolbar acts on the page scope.

A bulk action may cover files the page does not show — a group past its row cap, or a scope past
the page's. The action stays (an incident of tens of thousands of files cannot be listed in full,
and the guard already hashes the whole population rather than the visible rows), but it never
hides that: where the covered count exceeds the rows shown, the control's hint and its confirmation
state how many files are **not shown on this page**, and the group links to the scoped view and to
the file browser filtered to issues. *Alternative considered:* withhold the action until every
covered file is on screen. Rejected: it turns one decision about a deleted folder into paging
through it, and it would make the panel's bulk verbs unusable in exactly the incidents they exist
for.

"When it was found" is the newest open event's `detected_at` for the file, taken from the same
snapshot as the row; a file with no open event (legacy data) falls back to `last_changed`, then
`last_checked`. The page header summarises the findings by day ("found 26 Oct (355), 2 Nov (11) …").

All of it — groups, counts, every fingerprint — is a pure function of the one `_read_population`
snapshot. Minting is indexed (files by id, open events by file id) so a page with hundreds of rows
does not rescan the population per row.

### D8 — Row facts

Per row: the kind in words; when it was found; the file's own modification time (changed files —
the fact that answers "was that me?"); size; a "proof
kept" marker for notarized files; and, for a file with more than one recorded change, "changed N
times since <date>". A file whose open event is `restored_changed` is labelled "Came back
different" and shows that event's recorded detail (both digests) in full, wrapping — the surface
that requirement moves to now that the event feed is gone.

A missing file's row does **not** say when the file was "last seen": the record's `last_checked` is
rewritten by the very scan that finds the file missing, so the old "last seen" line was showing the
finding time under another name. The time the row states is when the file was found missing.

A file stays "came back different" for as long as that event is open, even when a later ordinary
change has opened a newer event on the same file: accepting the row closes both, so the digest
record is shown until then.

The modification time rides the population read (one more column). The change count and the event
detail are display-only reads keyed by the rendered rows' ids; they name no verb and enter no
fingerprint.

### D9 — Fleet review

Same component, one section per affected collection. Collections are ordered: Locked before
Working, then newest finding first, then name. Each group's forms carry `from=fleet` (the existing
whitelisted return literal) so success and refusal both land back on the fleet page. The existing
render budgets stay (100 rows per collection, 500 in total); a collection past the budget still
renders its section header, counts, groups' headers and its link. There is still no control that
acts on more than one collection.

### D10 — Dashboard

Top to bottom: page header with "Scan all collections" (unchanged); **Needs you**; three tiles
(files monitored, proofs anchored, last activity — the latter two with their existing rules);
collection cards in a responsive two-column grid; **Recent activity**.

*Needs you* lists each affected collection (same order as the fleet page) with its counts, when the
newest issue was found, its two largest folder groups and a Review link to that collection's review
page; with more than one affected collection it also links to the fleet page. With none it is a
single all-clear line — worded for what was established ("No files are changed or missing"), and
replaced by "No files indexed yet" when nothing is watched. It carries no forms, so it mints no
fingerprints.

*Recent activity* aggregates the event log by local day and collection for the last 30 days, newest
first, at most 12 lines: "Photos — 710 added · 4 changed". It is read-only. *Alternative
considered:* keep the per-event feed without buttons. Rejected: twenty rows of one bulk addition
say less than one line, and the per-file detail belongs to the review page (issues) and the
follow-on history view (everything else).

### D12 — The scan keeps an unresolved change listed

See D1. Two layers. **At the write:** the scan reads, once at its start, the ids of the
collection's files that carry an open `modified` / `restored_changed` event (a small set; no accept
can run during a scan, so it cannot go stale), and every place the pass would set an existing row
`ok` — matching reappearance, move reconciliation, the `churn` re-baseline, auto-baseline — goes
through one helper that returns `modified` for an id in that set. Batches commit every 500 files,
so this is what keeps every *committed* state honest, including the state an aborted scan leaves.
**At the end:** one sweep over the held ORM rows in the same transaction as the final batch (not a
bulk `UPDATE` — a later flush of a stale in-memory status would overwrite it), for paths nobody
listed and for rows left by earlier versions. The sweep re-reads the set first: the panel refuses
an accept while a scan runs, but `cairn accept` does not, so an alert can close mid-pass, and a row
this rule alone held `modified` goes back to `ok` rather than being left flagged with nothing open
behind it (found in implementation review; correcting the "cannot go stale" assumption above).
`import-manifest`, the one other writer of `files.status`, applies the same rule when it updates an
existing row.

Review round 2 found the mirror image: a held row can already read `modified` while the stored row
was set `ok` by an accept from another connection, so re-assigning `modified` writes nothing. Two
changes close it. The end-of-pass sweep also issues the rule as an `UPDATE` against the database
(after flushing, restricted to ids with an open change alert). And the race is removed at its
source: `cairn accept` refuses while an operation is running — checked before and again under the
write lock — and the panel's accept guard repeats its in-flight check under the lock it already
takes, where a claim can no longer commit. `RunSummary` counts the re-flagged files and the scan logs
one INFO line when the count is non-zero. It never touches a file that is `missing` (already an
issue) and never a file with no open change event.

### D11 — Status wording

The collection status pill states the fact: "All clear", "N changed", "N missing", or
"N missing · M changed"; "No files indexed" for an empty collection. Colours and icons are
unchanged (amber triangle for changed, red minus-circle for missing).

## Risks / Trade-offs

- **The acknowledge-only state disappears** → an operator who wants "seen it, still restoring" keeps
  the issue open; nothing is lost but a quieter counter. A snooze can be added later if wanted.
- **A file with an open change alert must stay an issue** (D1/D12) → enforced by the scan itself;
  data left by earlier versions heals on the first scan after deploy. Until that scan runs such a
  file is not listed (at most one scan cadence).
- **Folder scope is new attack surface on a destructive endpoint** → bound into the fingerprint,
  recomputed under the write lock from the submitted scope, and the mutation is by the hashed ids
  (D6). Gets the adversarial review.
- **Large test rewrite** (acknowledge and baseline tests retire) → guard tests are kept and extended
  rather than rewritten; removed-route tests assert 404/405.
- **Open tabs across the deploy** → their fingerprints no longer match (header changed) and their
  acknowledge buttons 404; both fail closed.

## Accepted limitations

Found during implementation and test review; none crosses the guard, each is recorded rather than
polished.

- **A folder whose name contains a CR or LF cannot use its group's bulk action.** A browser
  normalises a newline in the hidden `dir` field to CRLF on submit, so the scope never reproduces
  and the action is always refused ("Your action was NOT applied"). It fails closed; the per-file
  actions and the parent folder's bulk action still work.
- **A blank `deep` field means "whole subtree".** The framework substitutes the default for an empty
  form value before the scope is parsed, so it is not refused as malformed. It is still bound: it
  validates only the subtree fingerprint.
- **A fingerprint for an empty population is accepted as a no-op.** No page mints one (an action
  over nothing is never rendered); a hand-built one mutates nothing.
- **The needs-you header's "N missing · M changed" split is read separately from its total** (the
  total is the sidebar badge's number, as required). Under a concurrent scan the two can differ for
  one render; neither authorises anything.
- **The collection card's status-bar label still says "modified"** where the legend beside it says
  "changed" — the label's wording is pinned by an existing requirement and left alone.
- **Until the first scan after deploy**, a record left `ok` with an open change alert by an earlier
  version is not listed (D12 heals it on that scan).

## Migration Plan

No data migration. Ship as one PR; rollback is reverting the merge (no schema or stored-value
change, so the previous image runs against the same database).
