## Why

A collection has one change policy for all of its files. Real folders are mixed, and the product
has no way to be told so (coordination issue #67). On the reference deployment, of 278 change
alarms raised by one Locked collection in five months, 213 were for three file names that are
edited as part of normal work, and 10 were for the documents the collection exists to protect; in
another, every change alarm ever raised was for the same four index files; in a third, hundreds of
"missing" alarms were queue files that are created and removed by design. Each one had to be
accepted by hand, and will be again. The only alternative today is an exclude pattern, which stops
watching the file altogether: no deletion alarm, no corruption check, no timestamp proof.

A tripwire that is always tripped is not read. The alarms worth reading need to be the only ones.

This refines DESIGN.md §3's WORM/churn decision from "one policy per collection" to "a default per
collection, with exceptions by path", and adds a page to §5's web panel.

## What Changes

- **Rules.** A collection carries an ordered list of rules, each a path pattern with one effect:
  - *Changes expected* — an ordinary change to a matching file is recorded and re-stamped but not
    flagged, whatever the collection's policy.
  - *Comes and goes* — additionally, a matching file that disappears is not flagged; its record is
    retired as if its deletion had been accepted.
- **What a rule never covers.** Possible corruption, a file that came back different, and an
  unusually large change — or, under a comes-and-goes rule, an unusually large *deletion* — in one
  scan are flagged exactly as without the rule. A matching file is still tracked, hashed, deep-
  verified and stamped: a rule changes what is flagged, never what is watched.
- **Patterns** are small and literal: a name with no slash matches that file name in any folder; a
  path with a slash is anchored at the collection root; `*` and `?` stay inside one path segment,
  `**` spans folders, a trailing `/` means a whole folder. A pattern that would match every file is
  refused — that is what the collection's policy is for.
- **Created where the noise is seen.** A flagged file or folder on a review page links to a small
  page that proposes patterns for it (this name anywhere, this folder, just this file), shows for
  each what it would match — tracked files, open issues, and how many of the last ninety days'
  alarms it would have prevented — and creates the rule.
- **Creating a rule resolves the open issues it covers** (and says how many before it does): the
  changes are accepted, and under a comes-and-goes rule the missing records are retired.
- **A rules page** per collection lists its rules with what each currently matches, and removes
  them. Removing a rule changes nothing about files already recorded; later changes are flagged
  again.
- **BREAKING (stored data):** migration `0014` adds `collections.rules_json`.

## Non-goals

- **No history views** and no event for a rule-covered change: recording quiet changes so they can
  be browsed is the next change. Until then a rule-covered edit leaves the same trace an edit in a
  Working collection does — the file's record is updated and re-stamped.
- **No rule for additions.** New files are not flagged today and stay that way.
- **No per-rule alert routing, schedules or expiry.**
- **No rule editing** — remove and re-create.
- **No CLI for rules** in this change; they are managed from the panel.
- Exclude patterns keep their meaning (not watched at all) and their own syntax.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `corpus-management`: a collection carries rules; the pattern grammar and its validation.
- `integrity-scanning`: classification consults the rules; what a rule never covers; applying a
  rule to issues that are already open.
- `web-panel`: creating a rule from a review page with a preview; the rules page.
- `alerting`: a rule-covered change or deletion does not alert.
- `datastore`: `collections.rules_json`.

## Impact

- New `src/services/rules.py` — the pattern grammar, validation, matching, preview, applying a
  rule to open issues.
- `src/services/scanner.py` — the hash-compare branch and the missing sweep consult the rules; the
  mass-change ceiling covers rule-covered changes and deletions.
- `src/services/collections.py`, `src/models/db.py`, `alembic/versions/0014_*`.
- `src/control_panel/routes.py` + templates: `rule_new.html`, `collection_rules.html`, links from
  `partials/review_row.html` / `review_groups.html`, the collection page, the guidance.
- Tests: `tests/test_rules.py` (grammar, matching, validation), `tests/test_scan_rules.py`
  (classification, ceilings, never-covered findings), `tests/test_rules_panel.py`,
  `tests/test_migration_0014.py`.
- Docs: `README.md`, `DESIGN.md` §3 and §5, `CLAUDE.md`.
