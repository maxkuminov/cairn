"""typed, progress-bearing runs: runs.kind + runs.processed + runs.total

Turns a ``runs`` row into a typed operation record carrying live progress:
- ``runs.kind`` (TEXT NOT NULL DEFAULT 'scan', CHECK in ('scan','stamp','upgrade')) distinguishes
  an integrity scan from an OTS stamp backfill / upgrade pass; only 'scan' runs count toward
  scan-freshness (the dead-man's switch).
- ``runs.processed`` (int, default 0) — items handled so far, updated as the operation runs.
- ``runs.total`` (nullable int) — the planned denominator; NULL = unknown (indeterminate badge).

Adding a CHECK constraint on SQLite needs a table rebuild, so all three columns + the CHECK are
applied inside one ``batch_alter_table`` (same pattern as 0005). Existing rows backfill to
``kind='scan'`` via the column default, so freshness behaviour is unchanged for past runs.
Downgrade drops the three columns (another batch rebuild).

Revision ID: 0006_typed_progress_runs
Revises: 0005_rename_detection
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_typed_progress_runs"
down_revision = "0005_rename_detection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One table rebuild: add kind/processed/total and the kind CHECK. Existing rows take the
    # server_default ('scan' / 0), which is exactly the required backfill. The existing
    # ck_runs_result CHECK is reflected and preserved by batch mode.
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(16), nullable=False, server_default="scan")
        )
        batch_op.add_column(
            sa.Column("processed", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("total", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_runs_kind", "kind in ('scan','stamp','upgrade')"
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_runs_kind", type_="check")
        batch_op.drop_column("total")
        batch_op.drop_column("processed")
        batch_op.drop_column("kind")
