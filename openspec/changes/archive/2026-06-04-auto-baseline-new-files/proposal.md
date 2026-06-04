# Auto-baseline intact new files on the deep-verify pass

## Why
On a WORM collection, adding files is the normal happy path — a new photo, a new document. The
scanner classifies each as `new` (informational, born-acknowledged), watches and notarizes it, but
**never promotes `new → ok` on its own**: a scan is detection, not baselining. So `new` files
accumulate forever unless the operator clicks "Baseline new files" by hand. For a steadily-growing
collection like Photos that manual chore is constant and adds no safety — the file was already
hashed and notarized when first seen; `new` vs `ok` is only a baseline/UI distinction.

The weekly **deep-verify pass** already re-hashes every tracked file. That is the natural moment to
let a `new` file graduate: its bytes have just been re-confirmed intact, so promoting it to `ok` is
a *verified* baseline acceptance, not a blind one.

## What Changes
- Add a per-collection boolean **`auto_baseline_new`** (default **off** — current behavior is
  unchanged for every existing collection). Editable from the add/edit collection form (and the
  `cairn add-collection` CLI).
- When a **deep** scan runs on a collection with `auto_baseline_new` on, after classification and the
  missing-sweep the scanner SHALL promote every file that is **still `new` and intact** (present and
  re-hashed unchanged this pass) to `ok`. Files this pass reclassified `modified` or `missing`, and
  files **first discovered by this pass**, are left untouched — only files already tracked as `new`
  graduate, and only on a deep pass (a quick "Scan now" never auto-baselines).
- Modified and missing files are **never** auto-accepted — that stays the explicit `accept`
  operation. Auto-baseline only ever does `new → ok`.
- The promotion count is logged in the scan summary; no new alert, no re-stamp (a `new` file was
  already stamped when first seen).
- Enable `auto_baseline_new` on the **Photos** collection (the concrete ask); all other collections
  stay off until their owner turns the toggle on.

## Non-goals
- Auto-accepting modified or missing files (still manual `accept`).
- Promoting on a quick scan or immediately on first sight — graduation only happens on the deep pass,
  for files that were already tracked as `new`.
- An age threshold — any intact, already-tracked `new` file graduates on the deep pass (the weekly
  deep cadence is itself the "after a while").

## Impact
- **Affected specs:** `integrity-scanning` (deep-verify requirement gains the auto-baseline
  behavior), `corpus-management` (a collection persists `auto_baseline_new`), `datastore`
  (`collections.auto_baseline_new` column + migration), `web-panel` (the add/edit form toggle).
- **Affected code:** `src/models/db.py` (column), `alembic/versions/0010_*` (additive column),
  `src/services/collections.py` (create/update accept the flag), `src/services/scanner.py`
  (deep-pass promotion + summary counter), `src/control_panel/routes.py` (form field plumbing),
  `src/control_panel/templates/collection_form.html` (toggle), `src/cli.py` (`--auto-baseline`),
  `tests/`.
- **Data migration:** additive boolean defaulting `0` (off) — existing collections behave exactly as
  before; `alembic downgrade` drops the column. No re-scan required.
