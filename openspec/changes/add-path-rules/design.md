## Context

After `unify-issue-model` and `add-scan-safety-nets` the panel has one open item — a file recorded
`modified` or `missing` — and the scanner never lets a policy absorb corruption, a mass change or
an unreadable folder. What is left is ordinary noise: files that are expected to change or to come
and go, inside collections whose default is the opposite. The scanner's two decision points are
the hash-compare branch (a changed file: flag it, or re-record it quietly) and the missing sweep (a
tracked file the walk did not see: flag it). Both consult `collection.mode` and nothing else.

The risk of this change is the product's central one: a rule is a standing instruction not to
alarm. It must be narrow by construction, impossible to create by accident for a whole collection,
and unable to hide the findings that are never routine.

## Goals / Non-Goals

**Goals:** say "this is normal" once, where the noise is seen, with the consequence shown before
it is created; keep every safety net; keep watching and notarizing rule-covered files.

**Non-Goals:** history UI, rule editing, a CLI, rules for additions, per-rule alert routing.

## Decisions

### D1 — Storage

`collections.rules_json` (TEXT, NOT NULL, default `'[]'`): an ordered JSON array of
`{"pattern": str, "effect": "edits" | "comes_and_goes", "created_at": iso8601}`. It mirrors
`exclude_globs_json` and `alert_json`; a table would add a join for a list that is read whole on
every scan and edited a few times a year. At most 100 rules per collection.

### D2 — Pattern grammar (one module, `services/rules.py`)

Matched against the collection-relative POSIX path, case-sensitively:

- no `/` in the pattern → it is a **file name**, matched in any folder (`NOTES.md`);
- otherwise the pattern is **anchored at the collection root** (`clients/*/NOTES.md`);
- a trailing `/` means **everything beneath that folder** (`Agents/Queue/`);
- `*` and `?` match within one path segment; `**` matches any number of segments, including none;
  every other character is literal (no character classes).

Compiled once per scan to a regular expression (`**/` → `(?:.*/)?`, `**` → `.*`, `*` → `[^/]*`,
`?` → `[^/]`, everything else escaped), full-match. The first matching rule wins; `comes_and_goes`
implies `edits`.

**Refused at creation:** empty; leading `/`; a `.` or `..` segment; a NUL or newline; longer than
512 bytes; an empty segment (`a//b`); and any pattern with **no literal character at all** (`*`,
`**`, `**/*`, `*/**` …) — a rule for everything is a policy change, and the form says so and points
at the collection's policy. The grammar deliberately differs from exclude globs (which match any
path *part*): a rule silences alarms, so its reach has to be predictable from reading it.

### D3 — What the scanner does with a rule

- **Hash-compare branch.** After the return check and the corruption check (both unchanged, both
  first): a change is quiet when `collection.mode == "churn"` **or** an `edits`/`comes_and_goes`
  rule matches the path. Quiet changes are re-recorded and queued for a stamp as today, and are
  remembered for the mass-change ceiling — which therefore now applies in `worm` collections too,
  over the rule-covered changes of the pass.
- **Missing sweep.** After move reconciliation, a candidate that matches a `comes_and_goes` rule
  is a quiet removal: its events are detached (each keeping the path) and acknowledged, and its
  record is deleted — exactly what accepting its deletion does, by the same statements. Quiet
  removals have **their own ceiling**, with the same two settings as the mass-change rule: when a
  pass's quiet removals reach both the minimum count and the share of tracked files, none of them
  is removed; they are all recorded `missing` and alarmed like any deletion.
- **Never covered:** possible corruption; a reappearance with different bytes; a file with an
  open change alert (the unresolved-change rule runs after, and wins); anything on a pass that
  was refused. A file under an unreadable folder is not a missing candidate to begin with.
- A rule-covered file that reappears after a quiet removal is simply a new file.

### D4 — Applying a rule to issues that are already open

`rules.apply_to_open_issues(session, collection, rule, user_id)` resolves what the new rule
covers, in one transaction:

- `modified` rows whose path matches, **provided every open alert on the row is a plain `modified`
  event that is not part of a mass change** — a row with a `corrupted` or `restored_changed`
  alert, or a mass-change event, is left open — are accepted (status `ok`, alerts acknowledged,
  kept contents discarded), through `accept_files`;
- under `comes_and_goes`, `missing` rows whose path matches are retired, through `accept_files`.

It is called by the route that creates the rule, after the rule is stored, under the same
write-lock and operation-in-flight checks the accept guard uses. There is no population
fingerprint: a rule is not a statement about a list the operator was shown, it is a standing
instruction about a pattern, and files that match it tomorrow get the same treatment by design.
What keeps it honest is the preview (D5), recomputed at submit and stated in the confirmation, and
the refusals in D2. Removing a rule calls nothing.

### D5 — Preview

For a candidate `(pattern, effect)`, computed from the datastore, never from the filesystem:
tracked files matching; open issues it would resolve now, by kind (using D4's exact predicate);
and how many alarms of the last 90 days were for matching paths (events of kind `modified` /
`missing`, by the file's path or, for a retired file, the path kept in the event). The numbers are
shown next to each proposed pattern on the rule page and again in the submit confirmation.

### D6 — Panel

- **`GET /collection/{id}/rules/new?path=…&kind=changed|missing`** — reached from "Always expect
  this…" on a review row or group. Proposes, for a file `a/b/NOTES.md`: "files named `NOTES.md`,
  in any folder", "everything in `a/b/`", "just this file"; for a folder: the folder. Plus a free
  pattern field. Each proposal carries its preview. The effect defaults from `kind` and can be
  changed; the two effects are described in the words the policy descriptions use.
- **`POST /collection/{id}/rules`** — validates (D2), stores, applies (D4), redirects to the
  review page with a one-shot confirmation ("Rule added. N changes accepted, M records retired.").
- **`GET /collection/{id}/rules`** — the collection's rules, each with its effect, how many tracked
  files it matches now, and a remove control; plus the add form. Linked from the collection page.
- **`POST /collection/{id}/rules/delete`** — removes the rule whose pattern and effect are given
  (not an index: the list may have changed since the page was rendered).
- The collection page shows "N rules". The review guidance gains one sentence on rules.

All four routes are owner-scoped and CSRF-protected; the POSTs are refused while an operation is
in flight on the collection.

## Risks / Trade-offs

- **A rule too broad silences real incidents** → no match-all patterns; the preview states the
  reach before creation; corruption, changed reappearances and mass changes or deletions are never
  covered; the rules page shows what each rule matches now.
- **A quiet removal loses the record's link to its proof** (as accepting a deletion does) → the
  proof file stays in the store; the event keeps the path; documented in the effect's description.
- **Rule-covered changes are invisible until history ships** → same as a Working collection today;
  next change.
- **Classify-path change** → mandatory adversarial review, false-negative direction first.

## Migration Plan

Migration `0014` (`ADD COLUMN` with a default). One PR. Rollback: previous image + downgrade
(drops the column; rules are lost, nothing else).
