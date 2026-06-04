"""auto-baseline new files: collections.auto_baseline_new

Adds a per-collection boolean controlling whether the weekly deep-verify pass promotes intact
`new` files to `ok` (so additions don't pile up needing manual baselining). Additive and off by
default — existing collections behave exactly as before. ``server_default='0'`` lets the NOT NULL
column be added to a populated table without a backfill pass.

Revision ID: 0010_auto_baseline_new
Revises: 0009_rename_corpus_to_collection
Create Date: 2026-06-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_auto_baseline_new"
down_revision = "0009_rename_corpus_to_collection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column(
            "auto_baseline_new",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("collections", "auto_baseline_new")
