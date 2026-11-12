## Why

A collection has one change policy for all of its files. Real folders are mixed, and the product
has no way to be told so (coordination issue #67). On the reference deployment, of 278 change
alarms raised by one Locked collection in five months, 213 were for three file names that are
edited as part of normal work, and 10 were for the documents the collection exists to protect; in
another, every change alarm ever raised was for the same four index files. Each one had to be
accepted by hand, and will be again. The only alternative today is an exclude pattern, which stops
watching the file altogether: no deletion alarm, no corruption check, no timestamp proof.

A tripwire that is always tripped is not read. The alarms worth reading need to be the only ones.

This refines the WORM/churn decision (DESIGN.md §5, the per-run flow) from "one policy per
collection" to "a default per collection, with exceptions by path", and adds two pages to §5's
web panel.

## What Changes

- **Rules.** A collection carries a list of path patterns for files whose **changes are
  expected**. An ordinary change to a matching file is recorded and re-stamped but not flagged,
  whatever the collection's policy — the behaviour a Working collection has, for those files only.
- **What a rule never covers.** Deletions (a matching file that disappears is flagged exactly as
  before), possible corruption, a file that came back different, a file that still has an open
  change alert, and an unusually large change in one scan. A matching file is still tracked,
  hashed, deep-verified and stamped: a rule changes what is flagged, never what is watched.
- **Patterns** are small and literal: a name with no slash matches that file name in any folder; a
  path with a slash is anchored at the collection root; `*` and `?` stay inside one path segment,
  `**` stands for whole folders, a trailing `/` means a whole folder. A pattern that would match
  every file is refused — that is what the collection's policy is for.
- **Created where the noise is seen.** A changed file on a review page links to a small page that
  proposes patterns for it (this name anywhere, this folder, just this file) and shows for each
  what it matches — tracked files, open issues, and how many of the last ninety days' change
  alarms were for those files — before the rule is created.
- **A rule applies from the next scan. It resolves nothing by itself**: the changes already
  listed stay listed until they are accepted with the ordinary, guarded accept action, and the
  page says so.
- **A rules page** per collection lists its rules with what each currently matches, and removes
  them.
- **The mass-change ceiling now also covers rule-covered changes in a Locked collection.**
- **BREAKING (stored data):** migration `0014` adds `collections.rules_json`.

## Non-goals

- **No rule for deletions ("comes and goes").** Proposed and cut in spec review: retiring a
  record at scan time discards the digest a later reappearance must be compared against, and a
  temporarily unmounted subfolder would lose its records below any collection-wide ceiling. A
  missing file stays an issue; a folder's worth is one accept. If it returns, it will be designed
  around keeping the record.
- **A rule does not accept anything.** No new path that resolves issues or removes records.
- **No history views** and no event for a rule-covered change (next change): a rule-covered edit
  leaves the trace an edit in a Working collection does.
- No rule editing (remove and re-create), no CLI for rules, no per-rule alert routing or expiry.
- Exclude patterns keep their meaning (not watched at all) and their own syntax.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `corpus-management`: a collection carries rules; the pattern grammar and its validation.
- `integrity-scanning`: classification consults the rules; what a rule never covers; the
  mass-change ceiling's population.
- `web-panel`: creating a rule from a review page with its reach shown first; the rules page.
- `alerting`: a rule-covered change does not alert.
- `datastore`: `collections.rules_json`.

## Impact

- New `src/services/rules.py` — the pattern grammar, validation, matching, reach.
- `src/services/scanner.py` — the hash-compare branch consults the rules; the mass-change ceiling
  counts rule-covered changes.
- `src/services/collections.py`, `src/models/db.py`, `alembic/versions/0014_*`.
- `src/control_panel/routes.py` + templates: `rule_new.html`, `collection_rules.html`, a link from
  `partials/review_row.html`, the collection page, the guidance.
- Tests: `tests/test_rules.py`, `tests/test_scan_rules.py`, `tests/test_rules_panel.py`,
  `tests/test_migration_0014.py`.
- Docs: `README.md`, `DESIGN.md` §5, `CLAUDE.md`.
