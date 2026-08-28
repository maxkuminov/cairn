# Split `accept` into scoped verbs named for their consequences

## Why

GitHub **#12** (the Aug-2026 audit's meta issue) names this as root cause **R2**:

> `accept_collection` is one unscoped irreversible verb doing four unrelated things
>
> ```
> new      -> ok       (harmless: loses the "nobody reviewed this" marker)
> modified -> ok       (irreversible: this change will never be reported again)
> missing  -> DELETE   (irreversible: path, first_seen, sha256, ots_path gone; events detached
>                       to file_id=NULL and render as "—"; the .ots survives on disk, orphaned)
> all open events      -> acknowledged
> ```
>
> One label chosen from a render-time snapshot, no scope parameter, no per-file variant.

Everything downstream of that is a symptom. The three issues in this change are the symptoms that
matter:

- **#16** — the verb, its label and its blast radius do not agree with each other.
- **#30** — there is no way to accept *one* file, so a scan that reports one legitimate edit and one
  suspicious deletion cannot be resolved separately.
- **#35** — the records the verb leaves behind render their path as "—".

### Failure narrative 1 — the button that does more than it says (#16)

An operator opens `/collection/7/review`. The page lists exactly what it says it lists: two missing
tax documents and one modified contract. The one action beside them is **"Accept all changes"**. It
also promotes every `status='new'` file in the collection — 4 672 of them on a collection like Bob
Tax Services — files the page never displayed, whose "nobody has vouched for this" marker is gone
for good. There is no un-baseline.

The same click acknowledges **every** open event on the collection, not just the events of the files
it touched. That is the R2 defect in its purest form: on a `scope={"new"}` call it would clear a
missing-file alert the button never mentioned. The blanket
`UPDATE events … WHERE collection_id = … AND acknowledged_at IS NULL` is the line this change
deletes.

Worse, the label is chosen from a *render-time snapshot* while the effect is chosen at submit time
from `files.status`. The D14 population guard (shipped by `fix-ux-audit-sprint1`) closed the
window where those two disagree — it refuses a submission whose population has drifted. It did not,
and could not, close the gap where they disagree *at render time*, because a single verb acting on
three populations has no honest label. #12's own guard text records the deferral:

> Narrowing the re-baseline verb so it acts only on what was displayed is separate work.

This is that work.

### Failure narrative 2 — all-or-nothing (#30)

A scan reports two changes: a spreadsheet the operator edited this morning, and a photograph that
vanished from a directory nobody has touched in three years. The panel offers one control that
resolves both. The operator either waves the deletion through to clear the edit, or leaves the edit
alarming to keep the deletion visible. Both outcomes teach the operator that Cairn's counts are
noise. In a trust product, a control that trains the operator to ignore alarms is a false-negative
generator with a human in the loop.

### Failure narrative 3 — the history that survives as "—" (#35)

`accept_collection` deliberately detaches events (`file_id = NULL`) before deleting the file rows so
the audit trail survives the `ON DELETE CASCADE` — #12 explicitly lists that intent under **"Do NOT
'fix' these — they are correct as built"**. The rendering defeats it: `events.detail` is NULL for a
`missing` event, and the feed renders "Missing — —". The record of *which* file the operator
stopped tracking is the one fact those rows exist to carry, and it is exactly the fact that is lost.

### What this change does not re-litigate

`accept_collection`'s **detach-before-delete** and the D14 **population fingerprint** are both
correct as built. This change extends them; it does not replace either. No `.ots` proof is deleted
by any path here — as #12's headline records, none ever was.

DESIGN.md references: **§5** (the `files` shape and the per-run scan flow), **§8** (the panel's
review and accept surfaces).

## What Changes

### One verb becomes three, each named for its consequence (#16)

`accept_collection(session, collection, user_id, scope: set[str] | None = None)`. `scope=None` keeps
today's behaviour byte-for-byte, so `cairn accept` and every existing caller are unchanged. Each
panel control passes its own scope, and the panel is the only caller that passes one.

| Scope | Effect | Label | Style |
|---|---|---|---|
| `{"new"}` | `new -> ok` | **Baseline N new files** | primary, light confirm |
| `{"modified"}` | `modified -> ok` | **Adopt N changed files** | `btn--subtle`, own confirm |
| `{"missing"}` | detach events, then delete the rows | **Stop tracking N missing files** | `btn--danger`, own confirm |

Each ships the hint #16 specifies, verbatim in substance:

- *Baseline* — folds files Cairn found but you haven't vouched for into your expected set; they are
  already watched and notarized, this only clears the New label, and there is no un-baseline.
- *Adopt* — treats the current contents as correct from now on; Cairn recorded and notarized the new
  contents when it detected the change; this stops the alert and you won't be told about this change
  again.
- *Stop tracking* — permanently removes N files from Cairn's records: their paths, first-seen dates,
  and the link to their timestamp proofs. Nothing on disk is deleted and the `.ots` proof files stay
  in the proof store, but Cairn will no longer list them under Verify. To get them back, restore
  from backup and run a scan instead.

**The event acknowledgement is scoped to the files the scope actually touched.** This is the R2
defect and it is the substance of the change, not a detail of it: a scoped verb that still
blanket-acked would clear alerts its label never mentioned, which is the same defect with three
labels instead of one.

### Copy that must NOT ship

Recorded here because both strings sound reasonable and a fresh reader may re-derive them (#12's
rejected fixes 5 and #16's own list):

- **"Alert only — reversible by a rescan"** for adopted changes. False: the scan already wrote
  `row.sha256`, so a rescan matches and re-raises nothing.
- **"…their .ots proofs will be deleted."** They are not deleted. What is lost is the *link*.

### Each scoped verb rides the existing D14 guard, minted over its own population

Not a new guard — the same `_read_population` → `_population_fingerprint` → `_guarded_accept`
machinery, with the fingerprint minted over the population *that verb* acts on and the form scope
string identifying which verb it was minted for. The single-snapshot mint/recount discipline, the
durable-identity record encoding, the write-locked check-and-act, the lock-contention-is-a-refusal
classification and the `?stale=1` explanation are all preserved unchanged.

The open-event component narrows with the verb: it covers the open events **of the files in that
verb's population** — the events the verb will actually acknowledge — rather than the collection's
whole open-event set. It still binds them by identity and generation (id + kind + `detected_at`), so
the ABA replay it was introduced for stays closed.

### Per-file accept (#30)

- A new `accept_file(session, collection, file, user_id)` service, which replicates the pre-delete
  event detach and acknowledges **only that file's** events.
- A per-row control on the review page: *Stop tracking this file* on a `missing` row,
  *Adopt this change* on a `modified` row, each with its own confirmation and its own tiny
  fingerprint over that one row, checked by the same guard.

### A stopped-tracking file's events keep their path (#35)

At accept time — the `{"missing"}` scope and `accept_file` on a missing row — the same statement
that detaches the events backfills `events.detail` with **that event's own file's** relpath, via a
correlated subquery. It fills `detail` **only where it is NULL or empty**, so `moved`'s
`old → new` and `restored_changed`'s digest pair are never clobbered. This is the cheap version
#35 asks for: one statement, no schema change, no history page.

### The panel's accept surfaces are reorganized

- The review page's contrast card keeps its left column (*Mark reviewed*) exactly as built — #12
  lists it under "correct as built". Its right column becomes the stack of whichever scoped buttons
  apply, each with its live count taken from the same snapshot the page is rendered from.
- **The single "Accept all changes" button disappears from the panel**, and with it the
  `POST /collection/{id}/review/accept` route and the `review-accept` fingerprint scope string. The
  unscoped `accept_collection(scope=None)` survives for the CLI.
- The collection-detail page's existing "Baseline new files" form **is** the baseline scope; it
  keeps its route, its scope string and its render gate. Reconciling it is a rename of what it
  passes, not a new control.
- The recovery panel's "For anything you don't want back, **Accept all changes**" instruction is
  retargeted at *Stop tracking*.

### No schema change

`events.detail` already exists (migration `0005`). No column, no CHECK, no Alembic revision. If
implementation appears to need one, **stop and escalate** rather than adding it.

## Non-goals

- **No bulk-select UI.** No checkboxes, no "accept these 4 of 17". The unit is the whole scope or
  one file; anything between is a selection model the review page has no room for and #30 does not
  ask for.
- **No undo.** Every one of these verbs is irreversible by design and the copy says so. An undo
  would require retaining deleted `files` rows, which is a schema change and a new state machine.
- **`cairn accept` keeps its current unscoped behaviour** — one alias, no `--scope` flag, no CLI
  vocabulary work. It is documented as the unscoped legacy verb in its help text and in CLAUDE.md;
  splitting the CLI is deferred until the panel vocabulary has been used in anger.
- **The D7 render gate on the collection-detail baseline form is not relaxed.** With scoped acks its
  two zero assertions (`issues == 0`, no open events) are no longer *load-bearing*, but relaxing the
  gate would put a baseline control beside an open alarm, which is a UX ordering decision, not a
  consequence of scoping. See design D5.
- **No fleet-wide review page (#27), no dashboard count relinking (R3), no vocabulary rename of
  "Acknowledge" → "Reviewed" beyond what sprint 1 already shipped.** Those are their own issues.
- **No retroactive repair of already-detached events.** Rows detached by past accepts keep their
  NULL `detail` and keep rendering "—"; the feed is capped at 20 rows with no history page (#35's
  own "low value" note), so backfilling history is work with no reader.
- **No change to stamping, proof placement or `ots_state`.** Nothing here queues, re-stamps or
  deletes a proof. `ots-notarization` has no delta.
- **No new `files.status` value** — #12's rejected fix 3, explicitly.
- **No blanket per-file acknowledgement in the *restore* branch** — #12's rejected fix 7 is
  untouched; the scoped ack introduced here applies to accept, never to a restore. See design D8.

## Issue index

| Issue | Audit ref | Covered here |
|---|---|---|
| [#16](../../../issues/16) | A4 | `scope` parameter, three scoped verbs, scoped event ack, forbidden copy |
| [#30](../../../issues/30) | A18 | `accept_file`, per-row control, per-row fingerprint |
| [#35](../../../issues/35) | A23 | correlated `events.detail` backfill at accept time |
| [#12](../../../issues/12) | meta | root cause R2; rejected fixes 3, 5 and 7 honoured; "correct as built" list respected |
