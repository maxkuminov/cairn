"""rename corpus -> collection: table corpora->collections, corpus_id->collection_id

Pure terminology rename (DESIGN.md / web panel / CLI now say "collection"). The DB follows so
the schema speaks the same language as the rest of the product. This is an **in-place** rename —
no table rebuild, no row copy — so it is cheap and safe even on the ~186k-row ``files`` table:

- ``ALTER TABLE corpora RENAME TO collections`` — SQLite >= 3.25 also rewrites the foreign-key
  references in ``files``/``runs``/``events`` to point at ``collections`` (legacy_alter_table off,
  which is the default), so the children need no FK surgery.
- ``ALTER TABLE <child> RENAME COLUMN corpus_id TO collection_id`` — renames the column in place;
  SQLite updates the column references inside that table's own FK / unique / index definitions.

The one object that carries the term in its *name* and is referenced by later code is the partial
unique index that enforces one running run per collection — it is dropped and recreated under the
new name ``uq_runs_one_running_per_collection``. The remaining auto/explicit constraint names that
still embed "corpus" (``uq_files_corpus_relpath``, ``ck_corpora_*``, ``ix_files_corpus_id``) are
cosmetic labels on the renamed tables; renaming them would force a full ``files`` rebuild for no
functional gain, so they are intentionally left as-is.

Existing rows are preserved (row counts unchanged); ``downgrade`` reverses the rename.

Revision ID: 0009_rename_corpus_to_collection
Revises: 0008_one_running_run_per_corpus
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_rename_corpus_to_collection"
down_revision = "0008_one_running_run_per_corpus"
branch_labels = None
depends_on = None

_CHILDREN = ("files", "runs", "events")


def upgrade() -> None:
    # Drop the named partial index before the column rename so we can recreate it on the new
    # column under the new name.
    op.drop_index("uq_runs_one_running_per_corpus", table_name="runs")

    # Rename the parent table; child FK references to `corpora` are rewritten to `collections`
    # automatically by SQLite (>= 3.25, legacy_alter_table off).
    op.rename_table("corpora", "collections")

    # Rename the foreign-key column on each child in place.
    for table in _CHILDREN:
        op.execute(
            sa.text(f"ALTER TABLE {table} RENAME COLUMN corpus_id TO collection_id")
        )

    op.create_index(
        "uq_runs_one_running_per_collection",
        "runs",
        ["collection_id"],
        unique=True,
        sqlite_where=sa.text("result = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_runs_one_running_per_collection", table_name="runs")

    for table in _CHILDREN:
        op.execute(
            sa.text(f"ALTER TABLE {table} RENAME COLUMN collection_id TO corpus_id")
        )

    op.rename_table("collections", "corpora")

    op.create_index(
        "uq_runs_one_running_per_corpus",
        "runs",
        ["corpus_id"],
        unique=True,
        sqlite_where=sa.text("result = 'running'"),
    )
