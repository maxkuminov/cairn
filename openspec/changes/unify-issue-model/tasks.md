## 1. Service: accept an explicit set

- [ ] 1.1 Add `scanner.accept_files(session, collection, file_ids, user_id)`: chunked by id, re-asserting collection and status per statement; `missing` → detach + path backfill + delete, `modified`/`new` → `ok`; closes only those files' open events; one transaction
- [ ] 1.2 Tests in `tests/test_scanner.py`: named records only, mixed kinds, foreign-collection id ignored, already-`ok` id ignored, set larger than the bound-parameter ceiling
- [ ] 1.3 Unresolved-change rule (design D12): read the open-change-alert file ids at scan start; route every `status = "ok"` write for an existing row (reappearance, move, churn re-baseline, auto-baseline) through one helper; end-of-pass sweep over the held rows; no event, no alert; `RunSummary.reflagged` + one INFO line
- [ ] 1.4 Tests: changed → missing → restored stays `modified`; changed → renamed stays `modified`; mid-scan committed read (batch size patched) already `modified`; churn edit over an open `restored_changed`; no event/alert written; accepted change not re-flagged; legacy `ok`+open-event row healed; `missing` left alone; both policies

## 2. Guard: folder scope, baseline form removed

- [ ] 2.1 Add `_in_scope(relpath, dir, deep)` and scope validation; extend `_narrow` and the fingerprint header with `dir`/`deep` for `adopt-changed` and `stop-tracking`; add an index so per-row and per-group minting does not rescan the population
- [ ] 2.2 `_guarded_accept` takes the submitted scope, recounts with it, and calls `accept_files` with the hashed ids for the bulk forms; malformed scope refuses as stale
- [ ] 2.3 Remove the `baseline-new` form, its read scope, `_FP_ACCEPT_SCOPES` and `POST /collection/{id}/accept`
- [ ] 2.4 Add `mtime` to the population read (`_PopFile`)
- [ ] 2.5 The two bulk routes read `dir`, `deep`, `return_dir` and `from`; success and refusal return to the surface and scope the form was rendered on (fixed path, URL-encoded query value)

## 3. Acknowledge verb removed

- [ ] 3.1 Delete `POST /events/{id}/ack`, `POST /events/ack-all`, `POST /collection/{id}/review/ack-all`, `_event_feed`, `_event_view`, `_open_event_count` and the `open_events` view key
- [ ] 3.2 Delete `partials/_event_row.html`, `event_ack.html`, `events_feed.html`, `_events_controls.html`, `review_ack_row.html`, `_review_open_pill.html`

## 4. Review view

- [ ] 4.1 Grouping helper: common folder, child-folder groups, direct-files group, per-group counts / found range / fingerprints, ordering, row caps — all from the one snapshot
- [ ] 4.2 Display-only row facts: change count + first change per rendered file, `restored_changed` detail, found/last-written/last-seen times
- [ ] 4.3 `collection_review` handler: `dir` scope, summary by day, page-wide actions, copy list for the scope, empty states
- [ ] 4.4 Templates: rewrite `collection_review.html` and `partials/review_row.html`; new `partials/review_groups.html`; one collapsed help + recovery block after the list
- [ ] 4.5 CSS for groups, toolbar, quiet accept buttons; phone layout

## 5. Fleet review

- [ ] 5.1 `fleet_review` handler: per-collection sections built by the grouping helper, ordered Locked → newest finding → name, budgets kept, `from=fleet` on every form
- [ ] 5.2 Rewrite `review_fleet.html` on the shared groups partial

## 6. Dashboard

- [ ] 6.1 Handler: needs-you list (ordered like the fleet page, top two folders per collection), tiles reduced to three, recent activity aggregated by local day and collection
- [ ] 6.2 Templates: rewrite `dashboard.html`; new `partials/activity.html`; collection cards in a two-column grid
- [ ] 6.3 CSS for needs-you and activity

## 7. Vocabulary

- [ ] 7.1 Policy helper (Locked / Working + descriptions) used by detail page, form, cards and learn page
- [ ] 7.2 Status pill states counts (`_collection_status` label from counts); card legend wording
- [ ] 7.3 Collection detail: remove baseline form and the "Matching baseline" / "New files" stats; review link stays primary while issues exist
- [ ] 7.4 Collection form: Locked / Working toggle and help; remove the auto-baseline control; create sets `auto_baseline_new=True`, update leaves it alone; drop the deployment-specific exclude hint
- [ ] 7.5 `learn.html`: rewrite the alert / accept passages for the issue model
- [ ] 7.6 CLI: `--mode locked|working` aliases and help text; SMTP plaintext call to action

## 8. Tests

- [ ] 8.1 Retire acknowledge and baseline tests; assert the four retired addresses answer 404/405 and mutate nothing
- [ ] 8.2 Keep and adapt the guard tests (verb names, three-form cross-replay matrix)
- [ ] 8.3 New guard tests: folder scope acts only inside its folder, cross-folder replay refused (identical populations included), in-folder drift refuses / out-of-folder drift does not, direct-files scope, literal folder names (`%`, `_`, prefix siblings), malformed scope
- [ ] 8.4 New page tests: grouping (several folders, direct files, single folder, large group collapsed + scope link), scoped view, summary by day, row facts, files-before-instructions order, recovery guidance wording
- [ ] 8.5 Fleet ordering, group action returns to fleet, budgets
- [ ] 8.6 Dashboard: needs-you variants, read-only activity, counts agree with the badge, no acknowledge control or unreviewed count on any page
- [ ] 8.7 Vocabulary: Locked / Working on detail and form, status pill wording, form create/update of `auto_baseline_new`, CLI aliases
- [ ] 8.8 Full suite green

## 9. Verify and document

- [ ] 9.1 Run the panel against a copy of production-shaped data: desktop and phone screenshots of dashboard, review (changed-only, large missing set, scoped), fleet, detail, form
- [ ] 9.2 `openspec validate unify-issue-model --strict`
- [ ] 9.3 Update `README.md`, `DESIGN.md` §5 and `CLAUDE.md` (including the stale deploy paragraph)
