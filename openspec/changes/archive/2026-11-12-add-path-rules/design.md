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

Surrounding spaces and tabs are stripped before validation (and nothing else is — a line break
is refused on the raw input). Compiled once per scan to one regular expression per rule, built
from **atomic groups** so that no pattern can backtrack super-linearly (each segment, and each
run of segments after a `**`, is matched at its leftmost placement and never retried — the
standard greedy-glob argument; only the final run is end-aligned). The meaning, segment by
segment, full-match:
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

- **`GET /collection/{id}/rules/new?path=…`** — reached from "Don't flag future changes…" on the
  review row of a file listed as an ordinary change. Proposes, for `a/b/NOTES.md`, narrowest
  first: "Only this file", "Everything in its folder, at any depth", "Every file named
  `NOTES.md`, in any folder"; plus a free pattern field, checked by a GET (`&pattern=…`) that
  states the typed pattern's reach or its refusal and stores nothing. Each proposal
  carries its reach. The page states what a rule never covers and that open changes stay listed.
  `path` is a query value: it is only ever split into the proposals and rendered escaped; a value
  that is not a legal relative path yields the free field alone.
- **`POST /collection/{id}/rules`** — validates (D2), stores, redirects to the collection's review
  view with a one-shot confirmation ("Rule added. It applies from the next scan. N changed files
  listed here match it and are still listed — a rule does not accept anything"). It accepts
  nothing.
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
again" on the rules page (now "follow the collection's policy"). **Implementation review (Codex, adversarial, engine scope), round 1:** two MAJOR, both
fixed — the line-break check ran after stripping, so `drafts/\n` was accepted; and a legal
pattern of repeated `**/a/` segments took seconds to reject one path (now bounded by
construction, and checked against an independent reference matcher over generated cases).

Also folded in from round 1: `**` only as a whole segment,
the trailing-slash reading stated, the ninety-day figure defined by event-time path and labelled
for what it is, and the existing mass-change requirement modified explicitly.

**Implementation review, round 2** (verification of the fixes, plus the panel routes): the
matcher agreed with the reviewer's own reference matcher over ~106,000 comparisons and no probe
took more than a millisecond. Two MAJOR on the rule page, both fixed — a proposal for a path that
itself contains `*` or `?` was labelled "Only this file" (it is now labelled as a pattern, with
its reach), and an accepted typed pattern was displayed stripped of characters the stored
pattern keeps (the card now shows exactly what its button stores). One MINOR declined, below.

**Verifier** (independent, read-only): one blocking gap, fixed — a file recorded `modified` with
no open alert (rows left by the retired "mark reviewed" verb) was recorded healthy by a quiet
change; a rule-covered change now never takes a `modified` row off the review list (the
`churn` policy's own long-standing handling of such a row is unchanged and still tested). The reference deployment holds no such row today. Doc drift it listed is corrected.

**Independent panel tests** (written by an agent that did not write the panel) found three
defects, all fixed: a file name containing a line break was offered a rule link that opened an
empty page (the starting path is now accepted and its folder proposed); accepting a deletion
dropped that file's earlier alarms from the ninety-day figure (the path the accept keeps in the
event is now read); and a stored entry the scanner ignores was shown as "Already a rule". The
row's link was renamed from "Stop flagging this…" to "Don't flag future changes…" — the change
already listed stays listed.

**Implementation review, round 3** (verification only): the scanner fix and the round-2 panel
fixes were confirmed. Two statements on the rule page were found false and corrected — a
"this file is already covered" notice shown in a Working collection, where a rule has no effect,
and a sentence saying a few changed files could never reach the mass-change ceiling, which
depends on the configured thresholds.

**Usability pass** (a browser-driving agent on a sanitized copy of production data, before
merge): no blockers, five major, all addressed —
(1) "an unusually large change is still flagged" read as a per-file safety net; it is the
collection-wide ceiling, which a rule covering a few dozen files can never reach. Every surface
now says that a change to a matching file is not flagged *however large*, and the ceiling is
described once, with its two thresholds, as what it is.
(2) Reach was counts only; each pattern now lists the first twenty files it matches.
(3) A listed file already covered by a rule looked like any other; its row now says "Covered by
rule …" instead of offering another, and the rule page says so for its starting file.
(4) A folder typed without its trailing slash matched nothing and was told to check its
spelling; a zero-match pattern now says it would do nothing, offers the trailing-slash reading
when that one matches, and can only be added through a secondary "Add it anyway".
(5) The rules list was reachable only through an 11px link; the collection page and the review
toolbar now carry a "Rules (N)" control, and a rule typed on the rules page returns there.
Also: the link is not offered in a Working collection; a pattern matching half the collection or
more states the share and points at the policy.

## Accepted limitations

- **Wording kept as is** (usability pass, minor): "tracked files" (it excludes missing files,
  where the collection page's file count does not), "change alarms", and "Not added." on a
  refused preview.
- **Equivalent patterns are not recognised as duplicates** (`**/NOTES.md` and `NOTES.md`,
  `x/**` and `x/`): both can be stored; the first that matches is the one a row names.
- **A rule added from the fleet review page returns to that collection's review page**, not to
  the fleet page.

- **No rule for deletions.** Cut in spec review (see above); a missing file is always an issue.
- **A file directly in the collection's folder cannot be named alone**: a pattern with no slash is
  a name in any folder. The page offers that one proposal and states its reach.
- **A path containing `*` or `?` cannot be named exactly** — the grammar has no escape. Such a
  proposal is labelled as a pattern and its reach is stated.
- **A rule in a Working collection is stored and does nothing**; both rule pages, the
  confirmation and the collection page say so.
- **A stored entry with an effect this version does not know is inert but occupies its pattern**
  until removed on the rules page. Reachable only by hand-editing the column or by a rollback
  across a version that adds an effect.
- **The ninety-day figure** matches alarms raised before `add-scan-safety-nets` by the file's
  present path (they recorded none), so a file renamed since is counted under its new name.
- **A file made an issue by the mass-change rule offers the rule link** like any ordinary change.
  The rule it leads to would quiet that file's later ordinary edits, not a mass change; the rule
  page says so. Telling such rows apart needs each open event's detail on the review page.
- **Declined (review round 2, MINOR):** a browser that sends a hand-made `cairn_done=rule:N`
  cookie sees the "Rule added" confirmation. The cookie selects and authorises nothing, and the
  only person it can mislead is the one who forged it; the same holds for the accept
  confirmations it was modelled on.
- **The "Rule removed" notice rides a query flag** (`?gone=1`) and repeats if the page is
  reloaded. It changes nothing.
- **Two sessions adding rules at the same instant** can lose one of the two (the column is
  re-read before each write, which narrows the window and does not close it). The rules page
  shows what is stored.

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
