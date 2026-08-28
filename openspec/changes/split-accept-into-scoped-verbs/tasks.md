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
  suite survives, re-pointed per the **matrix in task 2.10** — not blanket-parameterized across all
  four verbs. Removing a scenario is a regression; re-pointing an *event* scenario at the population
  its verb actually hashes is the split working, and the matrix says which is which.
- **Honour `proposal.md`'s Non-goals and #12's "Do NOT implement these".** No `status='gone'`
  (rejected fix 3); the two forbidden strings (rejected fix 5 / #16) must not appear anywhere; the
  *restore* branch's `kind='missing'` ack scoping is untouched (rejected fix 7); the review page's
  contrast card, the recovery panel and born-acknowledged informational events are "correct as
  built" and are extended, never rewritten.
- **No proof is touched.** No task queues, re-stamps, moves or deletes an `.ots`.
- **Do not bundle findings from other audit issues.** #27, R1 and R3 are sequenced separately.

## 0. Pre-flight (before slice A)

- [x] 0.1 Confirm the base: `src/control_panel/routes.py` contains `_guarded_accept` **and**
  `_population_fingerprint`; `src/services/scanner.py::accept_collection` still has the blanket
  `Event.acknowledged_at.is_(None)` ack over the whole collection;
  `openspec/changes/archive/2026-08-28-guard-proof-and-restore-integrity/` exists. If any is
  missing, the branch is stale — stop.
- [x] 0.2 Confirm the Alembic head is `0011_proof_provenance_and_restored_changed` and that this
  change adds nothing after it.
- [x] 0.3 Baseline the gates so slice failures are attributable: `PYTHONPATH=. pytest -q` and
  `ruff check .` green.
- [x] 0.4 Coordination: the audit issues #16/#30/#35 themselves serve as the open coordination
  record for this change (each carries the full contract and is closed by the archive commit);
  no separate umbrella issue was opened. Supervisor decision, recorded here.

## 1. Slice A — service + CLI (`src/services/scanner.py`, `src/cli.py`, tests)

- [x] 1.1 Add `scope: set[str] | None = None` to `accept_collection`. `None` MUST reproduce today's
  behaviour exactly — every status, blanket collection-wide ack, same return dict, same counts.
  Validate the scope against `{"new", "modified", "missing"}` and raise on anything else (a typo'd
  scope must never silently degrade to "everything").
- [x] 1.2 Replace the blanket event ack with the scoped one (design D8): a scoped call acknowledges
  only open events whose `file_id` is one of the files that scope touched. Unscoped keeps the
  blanket ack, including detached (`file_id IS NULL`) open events.
- [x] 1.3 Rewrite the detach as the single correlated `UPDATE` of design D7 — detach + conditional
  ack + `detail` backfill, `WHERE file_id IN (SELECT id FROM files WHERE collection_id = … AND
  status = 'missing')`, issued **before** the deletes. No Python `IN` list (parameter limit), no
  clobbering of a non-empty `detail`, `COALESCE` on `acknowledged_at`.
- [x] 1.4 Add `accept_file(session, collection, file, user_id)`: one row, same detach/ack/backfill
  statement narrowed to that `file_id`, `new|modified -> ok`, `missing -> detach then delete`, acks
  only that file's open events, returns the same count shape. It MUST refuse (raise / return a
  sentinel the caller turns into a refusal) if the file is not in the given collection.
- [x] 1.5 `src/cli.py`: help-text only — name `cairn accept` the unscoped legacy verb and point at
  the panel for the scoped ones. No `--scope` flag, no behaviour change.
- [x] 1.6 Tests (`tests/test_scanner.py`): unscoped parity against the pre-change behaviour;
  each scope touching only its own rows; **a `{"new"}` accept leaves an open `missing` alert open**
  (the R2 regression test); adopt acks the adopted file's events and no others; stop-tracking acks
  the deleted files' events and no others.
- [x] 1.7 Tests for #35: an event with NULL `detail` on a stopped-tracking file gets that file's
  relpath; a `moved` event's `old → new` and a `restored_changed` event's digest pair are
  **unchanged**; two files in one call each get their **own** path (the correlation test); an
  already-acknowledged event keeps its original `acknowledged_at`/`acknowledged_by`.
- [x] 1.8 A stop-tracking call over more `missing` rows than SQLite's bound-parameter limit
  completes (the subquery, not an `IN` list).
- [x] 1.9 `PYTHONPATH=. pytest -q` + `ruff check .` green. Commit. **Review A's diff before B
  starts** — B builds on this contract.

## 2. Slice B — panel routes + templates (`routes.py`, templates, CSS, tests)

- [x] 2.1 Split `_FP_SCOPES` into read scopes (`baseline-new` → `new`; `review` → `missing`,
  `modified`) and form scopes (`baseline-new`, `adopt-changed`, `stop-tracking`, `accept-file`), per
  design D2. Retire `review-accept`.
- [x] 2.2 Add `file_id` to `_PopEvent` (event leg selects `Event.file_id` into the spare `n2`
  slot) and add the pure `_narrow(pop, form, statuses, file_id=None)` helper. For `adopt-changed`,
  `stop-tracking` and `accept-file`, the event component of a narrowed population covers only the
  events of its files; **`baseline-new` is the explicit exception** — it passes the wide read's
  entire open-event set through unchanged, so the collection-wide no-open-events assertion stays
  cryptographically bound (design D2/D3).
- [x] 2.3 `_population_fingerprint`: unchanged encoding, reading the narrowed population's scope
  field; the `accept-file` form additionally carries `file={id}` in the header. Keep the
  `baseline-new` `issues=` assertion (design D5).
- [x] 2.4 `_guarded_accept(session, collection, user, form, submitted_fp, file_id=None)`: same
  write-lock-first / recount / compare / act sequence, same refusal paths (empty fp, in-flight
  operation, lock contention, mismatch), now reading the form's **read** scope and applying the same
  `_narrow`. It calls `accept_collection(scope=…)` or `accept_file`.
- [x] 2.5 New routes `POST /collection/{id}/review/adopt-changed`, `POST
  /collection/{id}/review/stop-tracking`, `POST /collection/{id}/file/{file_id}/accept` (CSRF,
  `_get_owned_collection`, 303 back to review; refusal → `?stale=1`). **Delete**
  `collection_review_accept`.
- [x] 2.6 `collection_review` publishes, from its single existing snapshot: the two bulk
  fingerprints, a per-row fingerprint on each rendered item, and the two counts the button labels
  use (design D9). No additional query.
- [x] 2.7 `collection_review.html`: the contrast card's right column becomes the stack of applicable
  scoped buttons with #16's exact labels, hints, styles and per-button confirms; the single "Accept
  all changes" form is gone; the recovery panel's instruction is retargeted at *Stop tracking*.
  Neither forbidden string appears.
- [x] 2.8 `partials/review_row.html`: per-row *Adopt this change* / *Stop tracking this file* with
  its own confirm and hidden `population_fp`; **Mark reviewed** keeps its htmx swap unchanged.
  `panel.css`: the one new modifier (design D10).
- [x] 2.9 `collection_detail.html`: reconcile the comment block — the existing form **is** the
  baseline scope; its label and light confirm already match #16's baseline row. No behaviour change.
- [x] 2.10 Re-point the D14 suite in `tests/test_ux_dashboard.py` **per the matrix below**. The
  event term is not the same for every verb (design D2/D3), so the suite does **not** blanket-run
  every scenario against every verb: a blanket re-point would assert that a scoped verb refuses on
  drift it cannot reach, which is what the delta's *"is not refused by drift it cannot reach"*
  scenarios forbid. Each row names its 1:1 delta scenario.

  **(a) Form-independent scenarios — parameterize across all four form scopes** (`baseline-new`,
  `adopt-changed`, `stop-tracking`, `accept-file`), unchanged in substance:

  | Scenario | Delta scenario |
  |---|---|
  | reused row identifier | *A reused row identifier does not validate a stale fingerprint* |
  | re-created record (same path + digest, reused id) | *A re-created record does not validate a form minted for its predecessor* |
  | recreated collection | *A recreated collection does not validate its predecessor's fingerprint* |
  | absent / empty fingerprint | *A submission with no fingerprint is refused* |
  | lock contention → refusal, not 500 | *Lock contention is a refusal, not a server error* |
  | non-contention DB failure → error, not staleness | *A datastore failure that is not contention is not reported as staleness* |
  | in-flight operation → refusal | *An accept is refused while an operation is in flight* |
  | refusal banner / all-clear refusal banner | *A refused submission is explained…* / *A refusal landing on an all-clear view…* |

  **(b) Event scenarios — one per verb, against the population that verb actually hashes.** Do not
  parameterize these together; each is a different assertion:

  | Verb | Event-ABA test | Outside-scope **non**-refusal test |
  |---|---|---|
  | `baseline-new` | **collection-wide** open-event emptiness: an unrelated file goes `ok → modified → missing → restored`, ending `ok` with its `modified` event still open while the `new` set and the issue counts return to their rendered values ⇒ **refuse** (delta: *A stale baseline form is refused by an alert opening on a file it does not name*) | **none — by design.** `baseline-new` has no outside scope: *any* open event on the collection, including a detached one, refuses it. Assert that explicitly rather than omitting it. |
  | `adopt-changed` | ABA on a **modified** file in its own population (alert acked, same-kind alert reopened, count restored) ⇒ refuse (delta: *…stale adopt form*) | a further file goes **missing** and opens an alert; the modified set is unchanged ⇒ **proceed**, and the missing alert stays open (delta: *Adopt is not refused by drift it cannot reach*) |
  | `stop-tracking` | ABA on a **missing** file in its own population ⇒ refuse (delta: *…stale stop-tracking form*) | a further file goes **modified** and opens an alert; the missing set is unchanged ⇒ **proceed**, and the modified alert stays open (delta: *Stop tracking is not refused by drift it cannot reach*) |
  | `accept-file` | ABA on **that row's own** events ⇒ refuse (delta: *…stale per-file form*) | an alert opens on a **different** row, and that row's state changes; the submitted row has not moved ⇒ **proceed**, touching only the submitted row (delta: *A per-file action is not refused by drift on another row*) |

  **(c) Cross-form replay — pairwise, all four scope strings.** Parameterize over every ordered pair
  of the four form scopes (12 pairs): a fingerprint minted for form X submitted to form Y's endpoint
  ⇒ refuse, mutate nothing. Include at least one pair whose two populations are byte-identical, so
  the test proves the *scope string* is doing the work and not a coincidental population difference.
  (delta: *No accept form validates any action but its own*)

  **(d) Cross-row replay for `accept-file`.** A fingerprint minted for row A submitted at row B's
  address ⇒ refuse, mutate neither row — including where A and B share status, digest and size, so
  only the `file={id}` header term separates them. (delta: *A per-file form does not validate a
  submission at another row*)
- [x] 2.11 New route tests, beyond the 2.10 matrix: an adopt submission **is** refused when a file
  enters or leaves the `modified` set; a per-file submission is refused when that row was already
  accepted, and when it went `missing -> ok` between render and submit; a per-file POST at a row
  belonging to another collection or another user 404s; a **detached** open event
  (`file_id IS NULL`) refuses `baseline-new` and is invisible to the three narrowed verbs
  (design D3).
- [x] 2.12 `tests/test_ux_review.py`: the review page renders the applicable buttons with their live
  counts, renders none whose count is zero, renders no "Accept all changes", and contains neither
  forbidden string.
- [ ] 2.13 `PYTHONPATH=. pytest -q` + `ruff check .` green. Commit.

## 3. Integration + gates

- [ ] 3.1 Grep for orphans: no template, test or route references `review/accept` or
  `review-accept`; no caller passes a scope the service does not accept; `cairn accept` still
  reaches the unscoped path.
- [ ] 3.2 Full local gates on the merged result: `PYTHONPATH=. pytest -q`, `ruff check .`,
  `make audit`, `openspec validate split-accept-into-scoped-verbs --strict`. This is the gate that
  **admits the change to review**, not the one that admits it to deploy — see 3.4a.
- [ ] 3.3 **`openspec-verifier` subagent** (non-author) against the spec deltas. Iterate to zero
  blocking gaps.
- [ ] 3.4 **Adversarial Codex** (mandatory — this is the accept path, data integrity, multiple call
  sites). Frame it per CLAUDE.md: the expensive failure is a **false negative** — an alert cleared
  that the operator was never shown, a file record removed that no button named, a guard that
  validates a stale form. Point it at `scanner.py`, `routes.py`, the two templates and the deltas.
  Fix BLOCKER/MAJOR before shipping; record accepted limitations.
- [ ] 3.4a **Final gate — mandatory, on the final tree, after every verifier and adversarial fix has
  landed and immediately before deploy.** 3.3 and 3.4 both *require* fixes, and those fixes are
  committed after 3.2 ran; on the accept path a guard-query or fingerprint-encoding edit made under
  review pressure is exactly the kind that breaks a sibling test, so the pre-review run is not
  evidence about the tree being shipped. Re-run **all four** on the final commit:
  `PYTHONPATH=. pytest -q`, `ruff check .`, `make audit`,
  `openspec validate split-accept-into-scoped-verbs --strict`. All four green, or deploy does not
  proceed. If a fix lands *after* this checkpoint for any reason, this checkpoint runs again — it is
  the last thing before 3.5, always.

- [ ] 3.5 Deploy: commit → push → `make deploy`. **No `make migrate`** — this change adds no
  revision. Gated on 3.4a being green on the commit being pushed.
- [ ] 3.6 **`user-representative` pass** on the live panel: a collection with new + modified +
  missing files. Check that each button's count matches what the page lists, that the confirms name
  the right consequence, that the per-row controls resolve one file without touching the others,
  and that a refusal is legible.
- [ ] 3.7 Update CLAUDE.md's accept/review notes, `/openspec-archive-change`, push, close #16, #30,
  #35 and the coordination issue.

## 4. Post-audit fixes

- [x] 4.1 **BLOCKER (adversarial Codex, `routes.py:685`) — the acknowledgement row swap rendered
  and hashed two different snapshots.** `POST /events/{id}/ack?view=review` built the swapped-in
  row from a `session.get(FileEntry, …)` and minted its per-file fingerprint from a *later*
  `_read_population`; with `expire_on_commit=False` and sqlite3's legacy transaction mode, a scan
  committing `modified -> missing` between the two left the row offering "Adopt this change" while
  its fingerprint validated `accept_file` **deleting** the now-`missing` record — the named-verb
  violation this change exists to remove, inside the guard's own re-mint. The swap now takes ONE
  post-ack narrowed population read and derives the rendered row's status (and so its verb), its
  open-event state and its fingerprint from that single result; a row the read shows in no
  actionable population is rendered with no accept control and no fingerprint (the endpoint fails
  closed on the absent field). `_review_item` accepts a `_PopEvent`, which is open by construction.
  Web-panel delta gains the agreement clause + its scenario; deterministic interleaving regressions
  in `tests/test_ux_dashboard.py`
  (`test_the_acknowledged_row_shows_the_verb_its_own_fingerprint_authorizes`,
  `test_an_acknowledged_row_that_left_the_population_carries_no_accept_control`), both verified to
  fail on the pre-fix route.

- [x] 4.2 Verifier concern 1: the baseline button now carries the required kept-vs-lost hint
  (title attribute with the #16 copy verbatim) on `collection_detail.html`; asserted in
  `tests/test_ux_review.py::test_the_baseline_button_states_what_it_keeps_and_loses`.

- [x] 4.3 **BLOCKER (adversarial Codex, `routes.py:929`) — the detail page's baseline confirmation
  and its fingerprint came from two different snapshots.** `collection_detail` read
  `cview["counts"]["new"]` (from `_collection_view`'s earlier count query) to render the confirm
  "Baseline N new file(s)…", while `population_fp` was minted from a later
  `_read_population("baseline-new")`. A scan committing a second `new` file between the two was
  invisible to the count and visible to the mint, so the operator confirmed *one* file while the
  form validly authorized *both*, and an unchanged submit baselined a file the confirmation never
  named — the render-time lie this change exists to remove, in the one claim the action makes.
  The gate already derived from the population read; now the displayed count does too:
  when `show_baseline` holds, `cview["counts"]["new"]` is overwritten (on a copy) with
  `len(baseline.files)` from that same read — the same pattern `collection_review` uses for its
  button labels and legend. The confirm string on `collection_detail.html:48` is the only
  expression re-pointed (nothing else on that page reads `counts.new`); the tiles stay pure display
  off `cview`. Web-panel delta: the single-read requirement gains the count-agreement clause plus
  the "The baseline confirmation names the population its fingerprint covers" scenario.
  Deterministic interleaving regressions in `tests/test_ux_dashboard.py`
  (`test_the_baseline_confirm_names_the_population_its_fingerprint_covers` — drift **before** the
  population read, verified to fail on the pre-fix route; and its inverse
  `test_a_new_file_landing_after_the_baseline_read_is_refused_at_submit`).
