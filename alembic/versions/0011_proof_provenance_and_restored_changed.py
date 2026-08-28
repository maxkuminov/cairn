"""proof provenance: files.ots_digest + events.kind 'restored_changed'

Two additive schema changes for the guard-proof-and-restore-integrity change:
- `files.ots_digest` (nullable TEXT) records the digest the proof stored at `ots_path` commits to,
  so a proof/row divergence is detectable without re-parsing the `.ots`. Plain ADD COLUMN, no
  table rebuild and **no in-migration backfill** — the daily upgrade pass fills it, corroborated
  (design D3); existing rows stay NULL.
- `events.kind` CHECK gains `restored_changed` (a file that came back different from what left).
  SQLite can't ALTER a CHECK in place, so the events table is rebuilt via batch mode — exactly what
  `0005_rename_detection` did on this table to add `moved`.

Existing rows are untouched by the upgrade.

The downgrade is deliberately **not** symmetric (design D4a): narrowing the CHECK while
`restored_changed` rows exist can only succeed by deleting them, rewriting them to a kind that
means something else, or leaving the CHECK off — all of which destroy or falsify the audit record
of the most dangerous incident the product detects. So it refuses, naming the kind and the row
count, and leaves the database untouched. With no such rows it narrows the CHECK and drops
`files.ots_digest` normally.

Revision ID: 0011_proof_provenance_and_restored_changed
Revises: 0010_auto_baseline_new
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_proof_provenance_and_restored_changed"
down_revision = "0010_auto_baseline_new"
branch_labels = None
depends_on = None

_KINDS_NEW = "('added','modified','missing','restored','moved','restored_changed')"
_KINDS_OLD = "('added','modified','missing','restored','moved')"


def upgrade() -> None:
    # files.ots_digest — plain ADD COLUMN (SQLite supports this directly); nullable, no backfill.
    op.add_column("files", sa.Column("ots_digest", sa.String(64), nullable=True))
    # events: widen the kind CHECK to include 'restored_changed'. Batch mode rebuilds the table
    # (SQLite has no ALTER for CHECK constraints), preserving the FKs via reflection.
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint("ck_events_kind", f"kind in {_KINDS_NEW}")


def downgrade() -> None:
    # Refuse before touching anything: the narrowed CHECK cannot admit these rows, and every way
    # to make the copy succeed loses or falsifies evidence (design D4a).
    stranded = op.get_bind().execute(
        sa.text("SELECT count(*) FROM events WHERE kind = 'restored_changed'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Refusing to downgrade past {revision}: {stranded} events row(s) have "
            "kind='restored_changed', which the previous schema's ck_events_kind constraint does "
            "not admit. Downgrading would have to delete them or rewrite them to another kind — "
            "either destroys the audit record of a file that came back different from what left. "
            "Export those events, or deliberately re-classify them, then retry the downgrade."
        )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint("ck_events_kind", f"kind in {_KINDS_OLD}")
    op.drop_column("files", "ots_digest")
