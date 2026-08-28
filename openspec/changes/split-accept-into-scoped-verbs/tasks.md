# Tasks — split accept into scoped verbs

## Plan shape: two slices, **sequential on one branch**, not parallel worktrees

The obvious split is service+CLI (slice A) versus routes+templates (slice B). It is the right
*review* boundary and the wrong *execution* boundary here, so A and B run in order on one branch,
each by its own subagent, with a review of A's diff before B starts.

Why not fan out:

- **B's entire substance is the contract A defines.** The scoped-ack semantics, `accept_file`'s
  return shape and the detach/ack/backfill ordering are what B's routes call and what B's tests
  assert. Fanning out means B stubs `accept_collection(scope=…)` and `accept_file` with a
  `TODO(#16)` — i.e. stubs the change — and the post-merge integration step becomes the first time
  the real semantics are exercised. The orchestrator rule "fan-out ships green-but-unwired code" is
  the failure mode here, not a risk of it.
- **The seam is one file wide either way.** `routes.py` is untouched by A and `scanner.py` is
  untouched by B, so the merge saves nothing: there is no merge to save.
- **The win would be wall-clock on a ~600-line change**, against a guard whose whole value is that
  its two sides encode identically. That is not a trade worth making on the accept path.

Standing guardrails for both slices:

- **No Alembic revision, no model change, no new `files.status` value, no new event kind.**
  `events.detail` already exists. If a task appears to need a schema change, **stop and escalate** —
  do not add one.
- **Do not weaken the D14 guard.** Every existing scenario in `tests/test_ux_dashboard.py`'s D14
  suite survives, re-pointed at the new verbs. Removing one is a regression, not a consequence of
  the split.
- **Honour `proposal.md`'s Non-goals and #12's "Do NOT implement these".** No `status='gone'`
  (rejected fix 3); the two forbidden strings (rejected fix 5 / #16) must not appear anywhere; the
  *restore* branch's `kind='missing'` ack scoping is untouched (rejected fix 7); the review page's
  contrast card, the recovery panel and born-acknowledged informational events are "correct as
  built" and are extended, never rewritten.
- **No proof is touched.** No task queues, re-stamps, moves or deletes an `.ots`.
- **Do not bundle findings from other audit issues.** #27, R1 and R3 are sequenced separately.

## 0. Pre-flight (before slice A)

- [ ] 0.1 Confirm the base: `src/control_panel/routes.py` contains `_guarded_accept` **and**
  `_population_fingerprint`; `src/services/scanner.py::accept_collection` still has the blanket
  `Event.acknowledged_at.is_(None)` ack over the whole collection;
  `openspec/changes/archive/2026-08-28-guard-proof-and-restore-integrity/` exists. If any is
  missing, the branch is stale — stop.
- [ ] 0.2 Confirm the Alembic head is `0011_proof_provenance_and_restored_changed` and that this
  change adds nothing after it.
- [ ] 0.3 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green.
- [ ] 0.4 Open the coordination issue referencing #16, #30, #35 and link it from all three.

## 1. Slice A — service + CLI (`src/services/scanner.py`, `src/cli.py`, tests)

- [ ] 1.1 Add `scope: set[str] | None = None` to `accept_collection`. `None` MUST reproduce today's
  behaviour exactly — every status, blanket collection-wide ack, same return dict, same counts.
  Validate the scope against `{"new", "modified", "missing"}` and raise on anything else (a typo'd
  scope must never silently degrade to "everything").
- [ ] 1.2 Replace the blanket event ack with the scoped one (design D8): a scoped call acknowledges
  only open events whose `file_id` is one of the files that scope touched. Unscoped keeps the
  blanket ack, including detached (`file_id IS NULL`) open events.
- [ ] 1.3 Rewrite the detach as the single correlated `UPDATE` of design D7 — detach + conditional
  ack + `detail` backfill, `WHERE file_id IN (SELECT id FROM files WHERE collection_id = … AND
  status = 'missing')`, issued **before** the deletes. No Python `IN` list (parameter limit), no
  clobbering of a non-empty `detail`, `COALESCE` on `acknowledged_at`.
- [ ] 1.4 Add `accept_file(session, collection, file, user_id)`: one row, same detach/ack/backfill
  statement narrowed to that `file_id`, `new|modified -> ok`, `missing -> detach then delete`, acks
  only that file's open events, returns the same count shape. It MUST refuse (raise / return a
  sentinel the caller turns into a refusal) if the file is not in the given collection.
- [ ] 1.5 `src/cli.py`: help-text only — name `cairn accept` the unscoped legacy verb and point at
  the panel for the scoped ones. No `--scope` flag, no behaviour change.
- [ ] 1.6 Tests (`tests/test_scanner.py`): unscoped parity against the pre-change behaviour;
  each scope touching only its own rows; **a `{"new"}` accept leaves an open `missing` alert open**
  (the R2 regression test); adopt acks the adopted file's events and no others; stop-tracking acks
  the deleted files' events and no others.
- [ ] 1.7 Tests for #35: an event with NULL `detail` on a stopped-tracking file gets that file's
  relpath; a `moved` event's `old → new` and a `restored_changed` event's digest pair are
  **unchanged**; two files in one call each get their **own** path (the correlation test); an
  already-acknowledged event keeps its original `acknowledged_at`/`acknowledged_by`.
- [ ] 1.8 A stop-tracking call over more `missing` rows than SQLite's bound-parameter limit
  completes (the subquery, not an `IN` list).
- [ ] 1.9 `PYTHONPATH=. pytest -q` + `ruff check .` green. Commit. **Review A's diff before B
  starts** — B builds on this contract.

## 2. Slice B — panel routes + templates (`routes.py`, templates, CSS, tests)

- [ ] 2.1 Split `_FP_SCOPES` into read scopes (`baseline-new` → `new`; `review` → `missing`,
  `modified`) and form scopes (`baseline-new`, `adopt-changed`, `stop-tracking`, `accept-file`), per
  design D2. Retire `review-accept`.
- [ ] 2.2 Add `file_id` to `_PopEvent` (event leg selects `Event.file_id` into the spare `n2`
  slot) and add the pure `_narrow(pop, form, statuses, file_id=None)` helper. The event component of
  a narrowed population covers only the events of its files (design D3).
- [ ] 2.3 `_population_fingerprint`: unchanged encoding, reading the narrowed population's scope
  field; the `accept-file` form additionally carries `file={id}` in the header. Keep the
  `baseline-new` `issues=` assertion (design D5).
- [ ] 2.4 `_guarded_accept(session, collection, user, form, submitted_fp, file_id=None)`: same
  write-lock-first / recount / compare / act sequence, same refusal paths (empty fp, in-flight
  operation, lock contention, mismatch), now reading the form's **read** scope and applying the same
  `_narrow`. It calls `accept_collection(scope=…)` or `accept_file`.
- [ ] 2.5 New routes `POST /collection/{id}/review/adopt-changed`, `POST
  /collection/{id}/review/stop-tracking`, `POST /collection/{id}/file/{file_id}/accept` (CSRF,
  `_get_owned_collection`, 303 back to review; refusal → `?stale=1`). **Delete**
  `collection_review_accept`.
- [ ] 2.6 `collection_review` publishes, from its single existing snapshot: the two bulk
  fingerprints, a per-row fingerprint on each rendered item, and the two counts the button labels
  use (design D9). No additional query.
- [ ] 2.7 `collection_review.html`: the contrast card's right column becomes the stack of applicable
  scoped buttons with #16's exact labels, hints, styles and per-button confirms; the single "Accept
  all changes" form is gone; the recovery panel's instruction is retargeted at *Stop tracking*.
  Neither forbidden string appears.
- [ ] 2.8 `partials/review_row.html`: per-row *Adopt this change* / *Stop tracking this file* with
  its own confirm and hidden `population_fp`; **Mark reviewed** keeps its htmx swap unchanged.
  `panel.css`: the one new modifier (design D10).
- [ ] 2.9 `collection_detail.html`: reconcile the comment block — the existing form **is** the
  baseline scope; its label and light confirm already match #16's baseline row. No behaviour change.
- [ ] 2.10 Re-point the D14 suite in `tests/test_ux_dashboard.py`: every existing scenario (reused
  row id, recreated collection, ABA event, replaced alert, lock contention, absent/empty
  fingerprint, in-flight operation, refusal banner, all-clear refusal banner) runs once per scoped
  verb **and** for the per-file verb.
- [ ] 2.11 New route tests: a stop-tracking submission is **not** refused by an unrelated
  `modified` alert opening after render (the narrowed population, design D3); an adopt submission
  **is** refused when a file enters or leaves the `modified` set; a per-file submission is refused
  when that row was already accepted, and when it went `missing -> ok` between render and submit; a
  per-file POST at a row belonging to another collection or another user 404s.
- [ ] 2.12 `tests/test_ux_review.py`: the review page renders the applicable buttons with their live
  counts, renders none whose count is zero, renders no "Accept all changes", and contains neither
  forbidden string.
- [ ] 2.13 `PYTHONPATH=. pytest -q` + `ruff check .` green. Commit.

## 3. Integration + gates

- [ ] 3.1 Grep for orphans: no template, test or route references `review/accept` or
  `review-accept`; no caller passes a scope the service does not accept; `cairn accept` still
  reaches the unscoped path.
- [ ] 3.2 Full local gates on the merged result: `PYTHONPATH=. pytest -q`, `ruff check .`,
  `make audit`, `openspec validate split-accept-into-scoped-verbs --strict`.
- [ ] 3.3 **`openspec-verifier` subagent** (non-author) against the spec deltas. Iterate to zero
  blocking gaps.
- [ ] 3.4 **Adversarial Codex** (mandatory — this is the accept path, data integrity, multiple call
  sites). Frame it per CLAUDE.md: the expensive failure is a **false negative** — an alert cleared
  that the operator was never shown, a file record removed that no button named, a guard that
  validates a stale form. Point it at `scanner.py`, `routes.py`, the two templates and the deltas.
  Fix BLOCKER/MAJOR before shipping; record accepted limitations.
- [ ] 3.5 Deploy: commit → push → `make deploy`. **No `make migrate`** — this change adds no
  revision.
- [ ] 3.6 **`user-representative` pass** on the live panel: a collection with new + modified +
  missing files. Check that each button's count matches what the page lists, that the confirms name
  the right consequence, that the per-row controls resolve one file without touching the others,
  and that a refusal is legible.
- [ ] 3.7 Update CLAUDE.md's accept/review notes, `/openspec-archive-change`, push, close #16, #30,
  #35 and the coordination issue.
