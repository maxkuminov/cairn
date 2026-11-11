## Why

The panel shows the operator two ledgers at once — a log of alerts (each "reviewed" or not) and a
status on each file — and offers a different verb for each. "Mark reviewed" clears only the alert;
"Adopt" / "Stop tracking" clear the alert and the file status. The dashboard shows both counts side
by side (on the reference deployment: 385 unreviewed next to 384 open issues) and puts the weaker
verb first: it is the only action in the event feed, and "Mark all 385 reviewed" leaves the red 384
and every status pill exactly where they were. The owner of the deployment, who commissioned every
one of those surfaces, reports that the difference between acknowledging and re-baselining is still
not clear (coordination issue #62).

The "baseline" wording makes it worse because, for a changed file, it describes a difference that
does not exist. The scanner replaces the stored fingerprint the moment it sees the change; "Adopt"
only flips the status back to `ok`. The card that contrasts "Baseline unchanged" with "Rewrites your
baseline" is contrasting two controls that leave the recorded fingerprint identical. New files carry
the same problem in miniature: "not yet baselined" reads as a to-do, while baselining a new file
changes nothing about how it is watched or stamped.

The presentation then buries the files. The review page puts roughly 350 words of explanation above
the first file (a screen and a half on a phone, where the alert email's link lands); 377 missing
files render as 377 rows and 758 buttons with no grouping although 355 of them were found by one
scan; the all-collections review orders collections by how many files are missing, so a handful of
changed files in a locked collection sit below a hundred rows of routine deletions.

This is DESIGN.md §5 "Web panel" territory (the review, dashboard and collection pages) and it
revises the vocabulary half of §3's WORM/churn decision without touching its semantics.

## What Changes

- **One user-facing concept, the issue.** An issue is a tracked file whose status is `modified` or
  `missing`. Every count the panel shows about work waiting for the operator counts issues. The
  event log becomes read-only history.
- **BREAKING — "Mark reviewed" is removed.** `POST /events/{id}/ack`, `POST /events/ack-all` and
  `POST /collection/{id}/review/ack-all` are deleted, with every "unreviewed" count, pill and
  out-of-band refresh that fed them. A stale tab's POST gets a 404/405 and mutates nothing.
- **Verbs are named for their outcome.** A changed file is resolved by **Accept change**, a missing
  one by **Accept deletion** (today's "Adopt" / "Stop tracking"); the alternative to either is to
  restore the file outside Cairn. The honest-claims rules stay: accepting a change is never described
  as reversible by a rescan, and accepting a deletion is never described as deleting a proof.
- **BREAKING — "baseline" leaves the panel's review vocabulary.** The collection-detail "Baseline N
  new files" action and its route `POST /collection/{id}/accept` are removed, as are the dashboard
  "New files" tile, the detail page's "Matching baseline" / "New files" stats and the form's
  auto-baseline control. New collections created from the panel get auto-baseline on; existing rows
  and the CLI flag are untouched.
- **WORM / Churn are labelled Locked / Working** on every panel surface and in the CLI's help; the
  CLI accepts `locked` / `working` as aliases. Stored values stay `worm` / `churn`.
- **The review page is rebuilt around the files.** Issues are listed first, grouped by folder, each
  group with its counts, when it was found and one action per kind present; a group can be opened as
  its own review scope. Each row carries the facts needed to decide (what happened, when it was
  found, when the file was last written, how often it has changed). Explanation is one collapsible
  block instead of three cards.
- **Bulk verbs gain a folder scope.** The two bulk accept routes take the folder they were rendered
  for; the scope is part of the population fingerprint, and the accept acts on exactly the records
  the fingerprint covered.
- **The all-collections review orders by severity** — locked collections first, then newest
  finding — and renders each collection with the same folder groups.
- **The dashboard leads with what needs the operator**: a "Needs you" list of affected collections
  (or a plain all-clear), three summary tiles, the collection cards, and a read-only summary of
  recent activity by day and collection in place of the per-event feed with its buttons.
- **Status pills say what is wrong** ("2 changed", "377 missing") instead of "Attention" / "Alert".
- Alert e-mails say "Review and resolve" instead of "Review and acknowledge".

## Non-goals

- **No scanner change and no schema change.** How a change, a deletion or a new file is detected,
  classified, stamped and alerted on is untouched. `events.acknowledged_at` keeps being written by
  the accept and restore paths exactly as today; it is simply no longer an operator control.
- **No promise that a changed file's issue closes itself when the original is restored.** That
  needs the pre-change fingerprint, which the scanner does not keep; it is the follow-on scanner
  change. The copy in this change makes the claim for missing files only, where it is already true.
- **No path rules, no history views, no per-file page.** Follow-on change.
- **The verify card's wording** (which uses "recorded baseline" for the fingerprint on record) is
  not reworded here; it is reworked with the per-file history page.
- **`cairn accept`** keeps its unscoped, collection-wide behavior.
- No new dependency, no JavaScript framework; Slate tokens and existing components only.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `web-panel`: the dashboard, review, fleet-review, collection-detail and collection-form
  requirements are replaced or edited for the single issue model, the outcome-named verbs, the
  folder-scoped bulk actions and the Locked / Working labels; the accept guard requirement loses its
  baseline action and gains the folder scope.
- `integrity-scanning`: the accept requirement gains an accept over an explicit set of file records
  (what the panel's folder-scoped verbs call), with the same per-file consequences and the same
  scoped acknowledgement as the existing scoped accept.
- `corpus-management`: the CLI accepts `locked` / `working` as names for the two change policies.


## Impact

- `src/control_panel/routes.py` — review, fleet-review and dashboard handlers; the fingerprint
  forms (`baseline-new` removed, folder scope added); three acknowledge routes and the baseline
  route deleted; status labels.
- `src/control_panel/templates/` — `collection_review.html`, `review_fleet.html`, `dashboard.html`,
  `collection_detail.html`, `collection_form.html`, `learn.html`, `partials/review_row.html`,
  `partials/_collection_card.html`; new `partials/review_groups.html`, `partials/activity.html`;
  deleted `partials/_event_row.html`, `event_ack.html`, `events_feed.html`, `_events_controls.html`,
  `review_ack_row.html`, `_review_open_pill.html`.
- `src/control_panel/static/css/panel.css` — review groups, needs-you list, activity list.
- `src/services/scanner.py` — new `accept_files` (explicit id set); no change to scanning.
- `src/cli.py` — `--mode` aliases and help text. `src/notify/smtp.py` — one string.
- Tests: `tests/test_ux_review.py`, `test_fleet_review.py`, `test_ux_dashboard.py`,
  `test_panel.py`, `test_ux_docs.py`, `test_alert_links.py` and neighbours — acknowledge and
  baseline tests retired, guard tests kept, new tests for groups, folder scope and vocabulary.
- Docs: `README.md`, `DESIGN.md` §5, `CLAUDE.md`.
- No migration. Deploy is the usual merge → image → Flux rollout.
