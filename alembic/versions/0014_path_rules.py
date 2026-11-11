"""path rules: collections.rules_json

Adds the per-collection list of path rules (add-path-rules, design D1): an ordered JSON array of
``{"pattern", "effect", "created_at"}`` held as TEXT, like ``exclude_globs_json``. A plain additive
column — ``server_default='[]'`` fills every existing collection with an empty list, so no table
rebuild and no backfill pass; nothing else changes. The downgrade drops the column (the rules are
lost, nothing else).

Revision ID: 0014_path_rules
Revises: 0013_scan_safety_nets
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_path_rules"
down_revision = "0013_scan_safety_nets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "collections",
        sa.Column("rules_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("collections", "rules_json")
