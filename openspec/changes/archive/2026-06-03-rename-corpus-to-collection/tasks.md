# Tasks — rename "corpus" to "collection"

## 1. Models & constraints
- [x] 1.1 In `src/models/db.py`, rename class `Corpus → Collection`, `__tablename__ "corpora" →
  "collections"`, and the FK columns `corpus_id → collection_id` on `FileEntry`, `Run`, `Event`
  (pointing at `collections.id`).
- [x] 1.2 Rename relationships (`User.corpora → User.collections`; `back_populates="corpus" →
  "collection"`; the `Collection.files/runs/events` back-refs) and constraint/index names
  (`ck_corpora_* → ck_collections_*`, `uq_files_corpus_relpath → uq_files_collection_relpath`,
  `uq_runs_one_running_per_corpus → uq_runs_one_running_per_collection`).

## 2. Alembic 0009 migration
- [x] 2.1 Generate `alembic/versions/0009_rename_corpus_to_collection.py` (down_revision = current
  head `0008`). Use SQLite batch mode to rename the `corpora` table to `collections`, rename the
  `corpus_id` columns to `collection_id`, rebuild FKs to `collections.id`, and rename the
  constraints/indexes.
- [x] 2.2 Confirm `alembic upgrade head` then `alembic downgrade base` round-trips cleanly on a
  fresh DB, and that on a copy of a populated DB the upgrade preserves all rows (counts match).

## 3. Service module + imports
- [x] 3.1 `git mv src/services/corpora.py src/services/collections.py`; rename
  `create_corpus/list_corpora/get_corpus_by_name/update_corpus → *_collection(s)`, rename
  `corpus_id` params to `collection_id`, keep `active_run`/`claim_run`/`query_files`/`browse_tree`.
- [x] 3.2 In `src/services/scanner.py` rename `scan_corpus → scan_collection`, `accept_corpus →
  accept_collection` and update its `corpora` import to `collections`.
- [x] 3.3 Update all importers: `src/services/scheduler.py`, `src/services/proofs.py`,
  `src/control_panel/routes.py`, `src/cli.py` (and `src/services/manifest.py` if it imports).

## 4. Routes + compat redirect
- [x] 4.1 In `src/control_panel/routes.py`, rename every `/corpus/...` path to `/collection/...`
  and the `corpus_id` path param to `collection_id`; rename helper `_get_owned_corpus →
  _get_owned_collection`, `_corpus_view → _collection_view`, `_corpus_status → _collection_status`,
  `_corpus_counts → _collection_counts`.
- [x] 4.2 Add `GET /corpus/{rest:path}` returning a 308 redirect to `/collection/{rest}` so old
  bookmarks and the Uptime-Kuma poll keep working.

## 5. CLI
- [x] 5.1 Rename the `add-corpus` subparser to `add-collection` and `--corpus` to `--collection`
  (on scan/accept/verify/export/stamp/import-manifest); update help text and printed messages
  ("Created collection #…", "no collections configured (use: cairn add-collection)").
- [x] 5.2 Register `add-corpus` and `--corpus` as **hidden aliases** (argparse `aliases=` /
  duplicate `add_argument`) so existing scripts keep working without appearing in `--help`.

## 6. Templates, CSS, copy
- [x] 6.1 Rename templates: `corpora.html → collections.html`, `corpus_form.html →
  collection_form.html`, `corpus_detail.html → collection_detail.html`,
  `partials/_corpus_card.html → partials/_collection_card.html`; update all `{% include %}` /
  `render` references.
- [x] 6.2 Replace visible copy and titles (Corpora → Collections, Add corpus → Add collection, "No
  corpora yet…", the form blurb) and rename Jinja context vars (`corpora → collections`,
  `sidebar_corpora → sidebar_collections`).
- [x] 6.3 Rename CSS classes in `static/css/panel.css` and the templates (`.corpus-card* →
  .collection-card*`, `.corpus-row* → .collection-row*`, `.add-corpus → .add-collection`).

## 7. Docs + live spec prose
- [x] 7.1 Update `DESIGN.md` and `CLAUDE.md` prose to "collection" (leave archived changes alone).
- [x] 7.2 Update the live `datastore`, `corpus-management`, and `web-panel` spec prose where it
  describes the renamed structures (these are applied from this change's deltas on archive).

## 8. Tests & verification
- [x] 8.1 Update the test suite to the new names; `grep -rn "corpus\|corpora\|Corpus" src tests`
  returns only intentional residue (hidden CLI aliases, the `/corpus` redirect, the kept
  capability id).
- [x] 8.2 Full test suite green; `make build` / app boots; the dashboard and a collection page render.
- [x] 8.3 `openspec validate rename-corpus-to-collection --strict` passes.
