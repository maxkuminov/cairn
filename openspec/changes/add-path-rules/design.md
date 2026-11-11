## Context

After `unify-issue-model` and `add-scan-safety-nets` the panel has one open item — a file recorded
`modified` or `missing` — and the scanner never lets a policy absorb corruption, a mass change or
an unreadable folder. What is left is ordinary noise: files that are expected to change, inside
collections whose default is that nothing changes. The scanner decides in one place — the
hash-compare branch — whether a changed file is flagged or re-recorded quietly, and it consults
`collection.mode` and nothing else.

The risk of this change is the product's central one: a rule is a standing instruction not to
alarm. It must be narrow by construction, impossible to create by accident for a whole collection,
unable to hide the findings that are never routine, and it must not grow a second way of
resolving issues.

## Goals / Non-Goals

**Goals:** say "changes to these files are normal" once, where the noise is seen, with the reach
shown before the rule exists; keep every safety net; keep watching and notarizing those files.

**Non-Goals:** rules for deletions, any automatic resolution of issues, history UI, rule editing,
a CLI, per-rule alert routing.

## Decisions

### D1 — Storage

`collections.rules_json` (TEXT, NOT NULL, default `'[]'`): an ordered JSON array of
`{"pattern": str, "effect": "edits", "created_at": iso8601}`. It mirrors `exclude_globs_json`; a
table would add a join for a list read whole on every scan and edited a few times a year. `effect`
has one legal value today and is kept so a future effect needs no data migration; anything else in
that field makes the entry inert (it matches nothing) and the rules page shows it as unrecognised.
At most 100 rules per collection, no duplicate patterns.

### D2 — Pattern grammar (one module, `services/rules.py`)

Matched against the collection-relative POSIX path, case-sensitively, with no Unicode
normalisation (a pattern matches the path as it is stored):

- **no `/` in the pattern** → it is a **file name**, matched in any folder (`NOTES.md`,
  `*-Internal.xlsx`). `**` is not legal in a name.
- **otherwise** the pattern is a path **anchored at the collection root**, segment by segment
  (`clients/*/NOTES.md`). A **trailing `/`** means every file beneath that folder, at any depth
  (`_PhotoOrganize/` — anchored like any other path pattern, so it is the root-level folder).
- within a segment `*` matches any run of characters and `?` exactly one, neither crossing `/`;
  a segment that is exactly `**` matches any number of whole segments, including none
  (`**/cache/**`, `a/**/b`). `**` anywhere else — `a**b`, `**.md` — is **refused**: it has two
  readings. Every other character is literal; there are no character classes.

Compiled once per scan to one regular expression per rule, segment by segment, full-match:
a `**` segment that is **not last** → `(?:[^/]+/)*` (zero or more folders); a `**` segment that
**is last** → `(?:[^/]+/)*[^/]+` (any file beneath, at any depth — so `a/**` and `a/` mean the same
thing); a trailing `/` → the same; within a segment `*` → `[^/]*`, `?` → `[^/]`, the rest escaped.
`**/cache/**` therefore matches `cache/a`, `x/cache/a` and `x/y/cache/b/c`, and not `cache`.

**Refused at creation:** empty; leading `/`; a `.` or `..` segment; an empty segment (`a//b`); a
NUL or a line break; longer than 512 bytes; `**` that is not a whole segment or appears in a name;
and any pattern with **no literal character** (`*`, `**`, `**/*`, `*/**`, `?*` …) — a rule for
everything is a policy change, and the form says so and points at the collection's policy. A
pattern such as `*.*` is legal and broad; its reach is stated before it is created (D4), and the
ceiling (D3) bounds what it can absorb.

The grammar deliberately differs from exclude globs (which match any path *part*): a rule
silences alarms, so its reach has to be predictable from reading it.

### D3 — What the scanner does with a rule

One change, in the hash-compare branch, after the return check and the corruption check (both
unchanged and both first): an ordinary change is quiet when `collection.mode == "churn"` **or** a
rule matches the path. A quiet change is re-recorded and queued for a stamp exactly as a `churn`
re-baseline is, goes through the same helper that keeps a file with an open change alert
`modified`, and is remembered for the mass-change ceiling — whose population therefore becomes
"the changes this pass re-recorded quietly", in a `worm` collection too.

Nothing else consults the rules: not the missing sweep (a deletion is flagged as before), not the
reappearance branch (a file that comes back different is flagged), not the corruption check, not
any accept. A rule cannot remove a record or acknowledge an alert, and the only status it
changes is the one it exists to change: a healthy file with an ordinary change stays healthy. A
file that is already an issue stays one — a rule-covered edit to a `modified` file is quiet (no
further alert) but leaves it `modified` with its alerts open and its kept contents untouched,
which is exactly what an edit to such a file in a `churn` collection does today.

### D4 — Reach, shown before creation

For a candidate pattern, computed from the datastore, never from the filesystem:

- **tracked files it matches** (and the collection's total, so a broad pattern reads as broad);
- **open changed files it matches** — stated as *staying listed*, not as resolved;
- **change alarms in the last 90 days for files it matches**: `modified` events (not `corrupted`,
  not `restored_changed`, not events written by the mass-change rule), matched by the path the
  event recorded where it recorded one (events written since `add-scan-safety-nets` carry it) and
  otherwise by the file's current path. The label says "for files this matches", not "would have
  been prevented".

### D5 — Panel

- **`GET /collection/{id}/rules/new?path=…`** — reached from "Changes to this are expected…" on a
  changed file's review row. Proposes, for `a/b/NOTES.md`: "files named `NOTES.md`, in any
  folder", "everything in `a/b/`", "just this file"; plus a free pattern field. Each proposal
  carries its reach. The page states what a rule never covers and that open changes stay listed.
  `path` is a query value: it is only ever split into the proposals and rendered escaped; a value
  that is not a legal relative path yields the free field alone.
- **`POST /collection/{id}/rules`** — validates (D2), stores, redirects to the collection's review
  view with a one-shot confirmation ("Rule added: changes to … are expected from the next scan.
  N open changes it matches are still listed below."). It accepts nothing.
- **`GET /collection/{id}/rules`** — the rules, each with how many tracked files it matches now
  (zero is called out), a remove control, and the add form. Linked from the collection page, which
  states the rule count.
- **`POST /collection/{id}/rules/delete`** — removes the rule with the submitted pattern (never an
  index). Removing changes no record.
- Missing files' rows offer no rule link (there is no rule for deletions); the guidance says so.

All routes are owner-scoped; the POSTs are CSRF-protected. Storing a rule is a small write to the
collection row and is safe beside a running scan: the scan compiled its rules when it started, so
a rule added mid-scan applies from the next one.

## Review record

Spec round 1 (Codex): four blockers, all in the first draft's second effect ("comes and goes" —
retire a matching file's record when it disappears) and in resolving open issues at creation.
Retiring at scan time discards the digest a reappearance must be compared against; a collection-
wide ceiling does not protect a subfolder that is temporarily gone; a record with unresolved
findings could be retired; and resolving at creation mutated a population the operator had not
been shown, outside the accept guard. **Both ideas are cut** rather than patched: one effect
("changes expected"), and a rule resolves nothing. Round 2 confirmed the cut complete and found four text defects, fixed: the compile rule for a
final `**` segment, an over-broad claim about statuses, the further-change scenario of the kept-
contents requirement (now "raised where the collection would flag that change"), and "flagged
again" on the rules page (now "follow the collection's policy"). Also folded in from round 1: `**` only as a whole segment,
the trailing-slash reading stated, the ninety-day figure defined by event-time path and labelled
for what it is, and the existing mass-change requirement modified explicitly.

## Risks / Trade-offs

- **A rule too broad silences real edits** → no match-all patterns; reach stated before creation
  and on the rules page; deletions, corruption, changed reappearances and mass changes are never
  covered.
- **Rule-covered changes are invisible until history ships** → same as a Working collection today.
- **Deletions in churny folders still alarm** → one folder-level accept; revisit with a design
  that keeps the record.
- **Classify-path change** → mandatory adversarial review, false-negative direction first.

## Migration Plan

Migration `0014` (`ADD COLUMN` with a default). One PR. Rollback: previous image + downgrade
(drops the column; rules are lost, nothing else).
