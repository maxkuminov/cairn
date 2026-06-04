"""deep verify: corpora.verify_cadence_seconds + last_full_scan_at, runs.deep

Revision ID: 0002_deep_verify
Revises: 0001_initial
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_deep_verify"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Server defaults backfill existing rows: every corpus gets a weekly deep cadence, and
    # last_full_scan_at stays NULL (= deep verify owed now; the scheduler's one-deep-per-tick
    # cap rate-limits the catch-up). runs.deep backfills False for historical runs.
    op.add_column(
        "corpora",
        sa.Column(
            "verify_cadence_seconds",
            sa.Integer(),
            nullable=False,
            server_default="604800",
        ),
    )
    op.add_column(
        "corpora",
        sa.Column("last_full_scan_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "runs",
        sa.Column("deep", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("runs", "deep")
    op.drop_column("corpora", "last_full_scan_at")
    op.drop_column("corpora", "verify_cadence_seconds")
