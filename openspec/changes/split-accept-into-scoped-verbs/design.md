# Design notes — split accept into scoped verbs

Only the decisions that go beyond what #16, #30 and #35 already state. Where the issue text is
explicit — the three labels, the three hints, the two forbidden strings — that text *is* the spec
and is not re-derived here.

## Grounding: line references verified against HEAD (`9249795`)

- **`accept_collection` → `src/services/scanner.py:862-899`.** Confirmed shape: load every file row
  for the collection, `UPDATE events SET file_id = NULL WHERE file_id IN (missing_ids)`, then the
  per-row `new|modified -> ok` / `missing -> session.delete(f)` loop, then a blanket
  `SELECT … Event.acknowledged_at IS NULL` over the **whole collection** followed by a Python loop
  setting `acknowledged_at`/`acknowledged_by`. The blanket ack is the R2 defect (#12).
- **The D14 guard → `src/control_panel/routes.py:1075-1440`**: `_FP_SCOPES`, `_PopFile`,
  `_PopEvent`, `_Population`, `_read_population` (one `UNION ALL`, one snapshot),
  `_population_fingerprint` (pure), `_guarded_accept` (write lock → recount → compare → act),
  `collection_accept` (scope `baseline-new`), `collection_review_accept` (scope `review-accept`).
- **The review page → `src/control_panel/routes.py:1493-1560`** (`collection_review`, the single
  `_read_population(…, "review-accept")` read everything on the page is derived from) and
  **`collection_review.html:159-213`** (the contrast card; the accept form with its
  `population_fp` hidden field is at `:200-210`).
- **The per-row control → `partials/review_row.html`**; the per-row ack route is
  `routes.py::ack_event` with `?view=review`.
- **The detail page's baseline form → `collection_detail.html:26-45`**, gated on `show_baseline`
  (`routes.py:877-890`).
- **CLI → `src/cli.py:176-200`** (`_cmd_accept`) and **`:954-958`** (the parser).
- **Existing D14 test suite → `tests/test_ux_dashboard.py:495-1070`**, parameterized on the two
  scope strings; `tests/test_ux_review.py` asserts review-page markup.
- **Alembic head is `0011_proof_provenance_and_restored_changed`.** This change adds **no**
  revision. `events.detail` was added by `0005_rename_detection`.

## D1 — Route shapes: one POST per verb, scope decided by the URL

**Chosen.** Four routes, each with a server-side constant scope:

| Route | Form scope string | Redirect on success |
|---|---|---|
| `POST /collection/{id}/accept` *(existing path, unchanged)* | `baseline-new` | `/collection/{id}` |
| `POST /collection/{id}/review/adopt-changed` | `adopt-changed` | `/collection/{id}/review` |
| `POST /collection/{id}/review/stop-tracking` | `stop-tracking` | `/collection/{id}/review` |
| `POST /collection/{id}/file/{file_id}/accept` | `accept-file` | `/collection/{id}/review` |

Every one of them refuses to `/collection/{id}/review?stale=1`, which is the existing marker and the
existing banner.

**Rejected — one route with a `scope` form field.** The fingerprint header already carries the scope
string, so a tampered field would refuse rather than mis-act; the objection is not exploitability.
It is that the verb performed would be chosen by operator-supplied input on a destructive endpoint,
the access log would show one URL for three different consequences, and every future reader of
`_guarded_accept` would have to prove the scope was validated before believing the guard. A constant
per route removes the question.

**Rejected — reusing `POST /collection/{id}/review/accept` for the combined pair.** #16 replaces the
single accept-all; keeping a fourth verb that means "adopt *and* stop tracking" reintroduces the
label-cannot-describe-the-effect problem at half the blast radius.

The retired route is **removed, not redirected**: a POST from a stale open tab gets a 405/404,
mutates nothing, and the operator reloads. Silently forwarding it to one of the scoped verbs would
perform a verb the tab's button never named.

## D2 — Read scope vs form scope: one wide read, a pure narrowing, one encoder

The mint side (`collection_review`) needs the whole `missing` + `modified` set once, because the page
renders it, copies it, counts it *and* mints two bulk fingerprints and one fingerprint per rendered
row from it. Reading per verb would be several snapshots and would reopen exactly the window D14
closed.

So the two concepts are separated:

- **Read scope** — what `_read_population` fetches in its single `UNION ALL`. Two values:
  `"baseline-new"` (statuses `new`) and `"review"` (statuses `missing`, `modified`). `"review"`
  replaces `"review-accept"`; the old name described a form, and it no longer names one.
- **Form scope** — what a fingerprint is minted *for*: `baseline-new`, `adopt-changed`,
  `stop-tracking`, `accept-file`. It is the first field of the fingerprint header, so a fingerprint
  minted for one verb can never validate another (already an explicit D14 requirement — it now has
  four values instead of two to keep apart).

Between them sits one **pure** helper, `_narrow(pop, form, statuses, file_id=None) -> _Population`:
it filters `pop.files` to the form's statuses (or to the single `file_id`), filters `pop.open_events`
to the events belonging to those files, and returns a `_Population` whose `scope` field is the form
scope string. `_population_fingerprint` is unchanged apart from reading that field.

**The recount side reads the same wide read scope and applies the same `_narrow`.** It does *not*
narrow in SQL. D14 requires that "the same single-read derivation SHALL be used when the endpoint
recomputes the fingerprint, so the two sides cannot encode the same state differently"; two
different narrowings (Python on the mint side, SQL on the recount side) is precisely the divergence
that requirement forbids, even where they look equivalent.

## D3 — The open-event term narrows with the verb

Today the fingerprint's event component is *every* open event on the collection, because the verb
acknowledges every open event on the collection. Once the ack is scoped, that is no longer the
population being mutated, and keeping it would make the guard refuse on drift the verb cannot touch
— a `modified` alert opening elsewhere would refuse a *stop-tracking* submission whose own
population never moved. Refusals the operator cannot explain are how a guard gets worked around.

**Chosen:** the event component covers the open events of the files in the *narrowed* population —
exactly the events the verb will acknowledge — still by identity **and** generation
(`id`, `kind`, `detected_at`).

The two replays D14 introduced the component for are both still closed, because both are about the
alerts the verb clears:

- *ABA on the same file* — a file's open `missing` event is acknowledged in another tab and a fresh
  incident opens; the count returns to 1 while the incident is a different one. The file is in the
  verb's population, so its events are in the hash and `detected_at` separates the generations.
- *modified → missing → restored between render and submit* — the file leaves and re-enters the
  narrowed population, and its row's `status`/`sha256` change, so the file record itself refuses
  before the event term is consulted.

This requires `_PopEvent` to carry `file_id`. The `UNION ALL`'s event leg has spare padded columns;
`Event.file_id` goes into the `n2` slot (`size`), which is already an integer column on the file
leg, so no result-type coercion changes. Detached open events (`file_id IS NULL`) enter no scoped
verb's hash and are acknowledged by no scoped verb — they are cleared by *Mark all reviewed*, which
is unchanged and is not fingerprint-guarded (it mutates no file record).

## D4 — What happens to the `review-accept` scope string

It is **retired**, along with its route and its button. Nothing else in the product mints it, and
leaving a live scope string with no form to mint it is a loaded gun for the next reader who needs
"just an accept-all for a script".

Consequence for the existing test suite: `tests/test_ux_dashboard.py`'s D14 parameterization
(`("/collection/1/accept", "baseline-new"), ("/collection/1/review/accept", "review-accept")`) is
re-pointed at the two new review routes. Every scenario in that suite — reused row id, recreated
collection, ABA event, lock contention, missing/empty fingerprint, in-flight operation — must still
run, once per scoped verb, and once for the per-file verb. Deleting any of them is a regression in
the guard, not a consequence of the split.

## D5 — Where each verb is offered

- **Baseline (`new`)** stays on **collection detail only**, behind its existing `show_baseline`
  gate. It is *not* added to the review page: the review page renders `missing` + `modified`, and a
  button whose count refers to a population the page does not display is the R3 dead-end pattern
  the audit filed separately. In the one state where the review page has no issues, the existing
  spec already requires an all-clear empty state that offers no accept action.
- **Adopt / Stop tracking** are offered on the **review page only**, in the contrast card's right
  column, each rendered only when its count is non-zero.

**The D7 gate is deliberately not relaxed.** Its two conditions were justified by the unscoped
verb's second population ("it promotes not-yet-baselined files **and** it marks every open event on
the collection reviewed"). With the ack scoped to touched files, and `added` events born
acknowledged, the baseline verb now mutates no event at all — so the zero assertions are no longer
load-bearing. They are **retained** anyway: they are already hashed, already spec'd, already tested,
and removing them buys nothing this change needs while widening the destructive surface under
review. The spec's *justification* for the gate is rewritten in the delta, because the old one ("the verb
also marks every open event on the collection reviewed") lapses with the scoped ack; what remains
is the ordering argument — a baseline control must not sit in front of an alarm nobody has read.
*Whether the baseline control should become available beside open issues is a UX question for its
own change.* Flagged for Max.

## D6 — The per-file guard

Same guard, population of one. The form scope string is `accept-file` and the header additionally
carries `file={file_id}`, so a fingerprint minted for one row cannot validate a POST at another
row's URL even in the impossible case that two rows encode identically.

Its population is: the single file record (id, framed relpath, status, sha256, `first_seen` — the
identical record encoding, unchanged) plus that file's open events. `_read_population` gains an
optional `file_id` filter used **only on the recount side**, where reading the whole collection's
issue set to hash one row would be waste; the mint side slices the row out of the page's existing
snapshot. This is the one place mint and recount read different row *sets* — and it is safe
precisely because `_narrow` produces the same `_Population` either way: the encoder sees one record
and that file's events in both cases. (Contrast D2, where the divergence would have been in *how*
the same rows were selected.)

Fail-closed properties fall out for free:

- The row was already accepted by another tab ⇒ it is not in the recount ⇒ empty record set ⇒
  mismatch ⇒ refusal.
- The row moved `missing -> ok` (restored) between render and submit ⇒ `status` differs ⇒ refusal,
  so a restored file is never silently deleted by a stale row button.
- The row was deleted and its id reused ⇒ `first_seen`/`relpath` differ ⇒ refusal.

**Response shape: plain POST → 303 back to the review page, not an htmx row swap.** The row's
disappearance changes the header count, the legend, the copy list, the two bulk button labels, the
"need action" pill and the sidebar badge; an OOB swap set that large is a second rendering of the
page, and — decisively — a *refusal* inside an htmx swap has nowhere to render the staleness banner.
The per-row **Mark reviewed** control keeps its existing htmx swap: it mutates no file record, needs
no fingerprint, and its OOB set is two elements.

## D7 — The detach + ack + `detail` backfill is one statement (#35)

Issued **before** the delete, in the same write transaction the guard already holds:

```sql
UPDATE events
   SET file_id         = NULL,
       acknowledged_at = COALESCE(acknowledged_at, :now),
       acknowledged_by = CASE WHEN acknowledged_at IS NULL THEN :uid ELSE acknowledged_by END,
       detail          = CASE WHEN detail IS NULL OR detail = ''
                              THEN (SELECT relpath FROM files WHERE files.id = events.file_id)
                              ELSE detail END
 WHERE file_id IN (SELECT id FROM files
                    WHERE collection_id = :cid AND status = 'missing')   -- or: = :file_id
```

Five things this shape buys, each of which was a bug in the obvious alternative:

1. **Correlated, per row.** #35's own caution: one bulk `values()` cannot give each event its own
   path. The scalar subquery on `events.file_id` does.
2. **Evaluated pre-update.** SQLite evaluates every `SET` expression against the row's *old* values,
   so `detail`'s subquery still sees the pre-NULL `file_id` in the same statement that clears it.
   This is load-bearing and gets its own test — reversing it into two statements would backfill
   nothing.
3. **`detail` filled only when NULL or empty**, so `moved`'s `old → new` and `restored_changed`'s
   digest pair survive untouched (#35's second caution).
4. **`acknowledged_at` via `COALESCE`**, so an already-acknowledged event keeps its original
   timestamp and its original `acknowledged_by`. Re-stamping history at accept time would make the
   reading log lie about when it was read.
5. **A subquery, not a Python `IN` list.** A whole deleted folder is easily more `missing` rows than
   SQLite's bound-parameter limit; the existing code builds `missing_ids` in Python and would raise.
   The subquery has no such bound and needs no batching.

The row deletion itself stays as it is (ORM `session.delete`, which is what produces the counts the
CLI prints).

## D8 — The scoping rule for the event ack, and why it is not #12's rejected fix 7

**Rule: a scoped accept acknowledges every open event belonging to the files that scope actually
touched, and nothing else.**

#12's rejected fix 7 forbids a blanket `WHERE file_id = …` acknowledgement **in the restore
branch**, because there the file *survives*: an open WORM `modified` event on it describes a
different, still-unresolved condition, and clearing it is a false negative. That reasoning does not
transfer to accept:

- `stop-tracking` **deletes the record**. Every open event on it becomes unreachable from every
  surface in the panel the moment the row is gone; leaving one open is a nag with nothing behind it,
  and the operator explicitly asked to stop tracking this file.
- `adopt-changed` sets the file `ok` and rewrites nothing else. Its open events (`modified`,
  `restored_changed`) are *about the change being adopted* — that is what "adopt" means.
- `baseline-new` touches files whose events are born acknowledged, so in practice it acknowledges
  nothing at all.

The two rules coexist without contradiction: the restore branch keeps `kind='missing'` scoping and
is not touched by this change.

## D9 — Counts on the buttons come from the snapshot, never from a second query

`c.counts.missing` / `c.counts.modified` on the review page are already overwritten from `pop` for
the legend (`routes.py:1529-1533`). The button labels — "Adopt 3 changed files", "Stop tracking 12
missing files" — read the same two numbers, so the label, the rendered list, the copy list and the
fingerprint are all one snapshot. A label sourced from `_collection_view`'s earlier count query
could name a number the fingerprint does not cover, which is the render-time lie this change exists
to remove, reintroduced in the label.

## D10 — Per-row control styling

#16 fixes the **bulk** styles: adopt is `btn--subtle`, stop-tracking is `btn--danger`, each with its
own confirm. Per row, `btn--subtle` is already taken by **Mark reviewed** (design D8 of sprint 1:
the reading-log write is the quiet control), so a `btn--subtle` per-row *Adopt this change* would be
visually identical to the control that changes nothing — the exact collision the contrast card
exists to prevent.

**Chosen:** the per-row adopt gets a distinct amber-outlined variant (one new `.btn--warn-outline`
modifier over the existing `--warn` token; no new colour), the per-row stop-tracking is
`btn--danger btn--sm`, and Mark reviewed is unchanged. Both destructive per-row controls carry their
own confirm naming the single file.

*Alternative Max may prefer:* reuse `btn--subtle` per row for literal consistency with #16's table
and rely on label + icon alone to separate it from Mark reviewed. Cheaper, and one fewer CSS rule.

## D11 — What the CLI keeps

`cairn accept` calls `accept_collection(scope=None)` — the unscoped verb, unchanged in behaviour and
in its printed counts (`accepted=… removed_missing=… events_acknowledged=…`). Its `--help` gains one
clause naming it the unscoped legacy verb and pointing at the panel for the scoped ones. No
`--scope` flag: a CLI vocabulary split with no operator asking for it is speculative surface, and
the `None` path must keep existing so the panel's three scopes have a regression baseline to be
compared against.

## D12 — Scope of the file edits

| File | What changes |
|---|---|
| `src/services/scanner.py` | `accept_collection(scope=…)`, the single detach/ack/backfill statement, `accept_file` |
| `src/cli.py` | help text only |
| `src/control_panel/routes.py` | `_FP_SCOPES` split into read/form scopes, `_narrow`, `_PopEvent.file_id`, optional `file_id` read filter, `_guarded_accept(form=…)`, two new review routes + one per-file route, `collection_review` publishes the per-form fingerprints, `collection_review_accept` deleted |
| `collection_review.html` | contrast card right column → scoped stack; recovery-panel copy retarget |
| `partials/review_row.html` | per-row accept control |
| `collection_detail.html` | comment/copy reconciliation only (the form already posts the baseline scope) |
| `static/css/panel.css` | one modifier (D10) |
| `tests/` | D14 suite re-pointed and extended; new service + route tests |

No migration. No model change. No `ots-notarization` delta.
