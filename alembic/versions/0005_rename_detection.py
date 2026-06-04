"""rename detection: events.kind 'moved' + events.detail, runs.moved

Adds content-addressed move/rename support to the schema:
- `events.kind` CHECK gains `moved` (SQLite can't ALTER a CHECK in place, so the events table is
  rebuilt via batch mode).
- `events.detail` (nullable TEXT) records the old → new path of a `moved` file.
- `runs.moved` (int, default 0) counts files reconciled as moves in a run.

Additive: existing rows are untouched by the upgrade (the new columns backfill NULL / 0).
Downgrade reverses the constraint change and drops the added columns.

Revision ID: 0005_rename_detection
Revises: 0004_ack_informational_events
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_rename_detection"
down_revision = "0004_ack_informational_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # runs.moved — plain ADD COLUMN (SQLite supports this directly).
    op.add_column(
        "runs",
        sa.Column("moved", sa.Integer(), nullable=False, server_default="0"),
    )
    # events: add detail + widen the kind CHECK to include 'moved'. Batch mode rebuilds the
    # table (SQLite has no ALTER for CHECK constraints), preserving the FKs via reflection.
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("detail", sa.Text(), nullable=True))
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_events_kind",
            "kind in ('added','modified','missing','restored','moved')",
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint(
            "ck_events_kind",
            "kind in ('added','modified','missing','restored')",
        )
        batch_op.drop_column("detail")
    op.drop_column("runs", "moved")
