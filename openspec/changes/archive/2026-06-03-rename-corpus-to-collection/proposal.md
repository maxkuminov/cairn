# Rename "corpus" to "collection" across the product

## Why
Cairn's core noun for "a folder it watches under one policy" is **corpus** / **corpora**. The word
reads as academic jargon to the people actually using the panel (the operator reviewing what
happened to their files, the homelab users being onboarded in Phase 2). "Collection" says the same
thing — a body of files held together — without the friction. The term is everywhere: the sidebar,
every card and form, the CLI (`add-corpus`, `--corpus`), the routes (`/corpus/...`), the DB table
(`corpora`), and the ORM. A **display-only** rename would leave the code, URLs, and CLI speaking a
different language than the UI, so this is a **full rename** — UI, code, DB, routes, and CLI all
move to "collection" together and stay internally consistent.

## What Changes
- **Models / DB** (`src/models/db.py`): `__tablename__ "corpora" → "collections"`; ORM class
  `Corpus → Collection`; FK columns `corpus_id → collection_id` on `files`, `runs`, `events`;
  `User.corpora → User.collections`; constraint/index names (`ck_corpora_* → ck_collections_*`,
  `uq_files_corpus_relpath → uq_files_collection_relpath`, `uq_runs_one_running_per_corpus →
  uq_runs_one_running_per_collection`).
- **Migration** (`alembic/versions/0009_rename_corpus_to_collection.py`): in-place SQLite
  **batch-mode** rebuild that renames the table, the `collection_id` columns and their foreign
  keys, and the constraints — **preserving all existing rows**. `upgrade head` / `downgrade base`
  round-trip cleanly on a fresh DB; on the live DB row counts are unchanged.
- **Service module**: rename `src/services/corpora.py → src/services/collections.py`; functions
  drop the noun (`create_corpus → create_collection`, `list_corpora → list_collections`,
  `get_corpus_by_name → get_collection_by_name`, `update_corpus → update_collection`,
  `active_run`/`claim_run` keep their names, `corpus_id` params → `collection_id`). Update all
  importers: `scanner.py` (`scan_corpus → scan_collection`, `accept_corpus → accept_collection`),
  `scheduler.py`, `proofs.py`, `control_panel/routes.py`, `cli.py`, tests.
- **Routes** (`src/control_panel/routes.py`): `/corpus/... → /collection/...` for all patterns
  (`/collection/new`, `/collection/validate-root`, `/collection/{collection_id}` and its
  `/files`, `/tree`, `/op-status`, `/scan`, `/accept`, `/stamp-all`, `/edit`); path param
  `corpus_id → collection_id`. A thin **`GET /corpus/{rest:path}` → 308 redirect** to
  `/collection/{rest}` keeps bookmarks and the Uptime-Kuma link working.
- **CLI** (`src/cli.py`): `add-corpus → add-collection`; `--corpus → --collection` on
  `scan`/`accept`/`verify`/`export`/`stamp`/`import-manifest`. The old `add-corpus` subcommand and
  `--corpus` flag stay as **hidden aliases** so existing muscle memory and scripts keep working;
  help text and messages use the new names.
- **Templates + CSS** (`src/control_panel/templates/**`, `static/css/panel.css`): all visible copy
  ("Corpora" → "Collections", "Add corpus" → "Add collection", "No corpora yet…", the form blurb
  "A collection is a folder Cairn watches…"); rename templates `corpus_form.html →
  collection_form.html`, `corpus_detail.html → collection_detail.html`, `corpora.html →
  collections.html`, `partials/_corpus_card.html → partials/_collection_card.html`; CSS classes
  `.corpus-card* / .corpus-row* / .add-corpus → .collection-*`; Jinja context vars
  `corpora / sidebar_corpora → collections / sidebar_collections`.
- **Docs**: `DESIGN.md`, `CLAUDE.md`, and the prose of the live structural/interface specs
  (`datastore`, `corpus-management`, `web-panel`) move to "collection". The OpenSpec **capability
  id** `corpus-management` is left unchanged.

## Non-goals
- Renaming the OpenSpec capability id `corpus-management` or its filename (internal traceability;
  archived changes reference it as historical record).
- Editing archived changes under `openspec/changes/archive/**` (immutable history).
- Restic / backup-tool integration or any recovery UX (a separate change,
  `add-issue-review-and-recovery`).

## Impact
- **Affected specs:** `datastore` (table is `collections`, FK `collection_id`, rename migration),
  `corpus-management` (`cairn add-collection`, a `collections` row), `web-panel` (add/edit
  collection, `/collection` routes + the `/corpus` → `/collection` redirect).
- **Affected code:** `src/models/db.py`, `alembic/versions/0009_*`, `src/services/collections.py`
  (renamed) + importers (`scanner.py`, `scheduler.py`, `proofs.py`,
  `control_panel/routes.py`, `cli.py`), `src/control_panel/templates/**`,
  `src/control_panel/static/css/panel.css`, `tests/**`.
- **Data migration:** in-place rename preserving rows; `alembic downgrade` reverses it. A DB with
  existing collections behaves exactly as before under the new names.
- **Compatibility:** old `/corpus/...` URLs 308-redirect to `/collection/...`; `cairn add-corpus`
  and `--corpus` remain as hidden aliases.
