"""scan safety nets: files.prior_sha256/prior_size/prior_mtime + events.kind 'corrupted'

Two schema changes for the add-scan-safety-nets change (design D7):

- `files.prior_sha256`, `files.prior_size`, `files.prior_mtime` — nullable, plain ADD COLUMNs (no
  rebuild of the large `files` table). They hold the contents that were on record when the file was
  last healthy, kept while an unaccepted change is open, so a file put back exactly as it was can
  close its own issue (design D3). Every existing row reads NULL: nothing was kept before this
  revision, and a row with no kept contents behaves exactly as before.
- `events.kind` CHECK gains `corrupted` (a digest that changed behind an unchanged size and
  modification time — design D2). SQLite cannot ALTER a CHECK in place, so the events table is
  rebuilt via batch mode, exactly as 0005 (`moved`) and 0011 (`restored_changed`) did.

Existing rows are untouched by the upgrade.

The downgrade refuses while any `corrupted` event exists, for the reason 0011 gives for
`restored_changed`: narrowing the CHECK could only succeed by deleting those findings or relabelling
them as something the system never detected. With none it narrows the CHECK and drops the three
columns.

Revision ID: 0013_scan_safety_nets
Revises: 0012_run_error_visibility
Create Date: 2026-11-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_scan_safety_nets"
down_revision = "0012_run_error_visibility"
branch_labels = None
depends_on = None

_KINDS_NEW = "('added','modified','missing','restored','moved','restored_changed','corrupted')"
_KINDS_OLD = "('added','modified','missing','restored','moved','restored_changed')"


def upgrade() -> None:
    op.add_column("files", sa.Column("prior_sha256", sa.String(64), nullable=True))
    op.add_column("files", sa.Column("prior_size", sa.BigInteger(), nullable=True))
    op.add_column("files", sa.Column("prior_mtime", sa.Float(), nullable=True))
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint("ck_events_kind", f"kind in {_KINDS_NEW}")


def downgrade() -> None:
    # Refuse before touching anything (same reasoning as 0011's downgrade).
    stranded = op.get_bind().execute(
        sa.text("SELECT count(*) FROM events WHERE kind = 'corrupted'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Refusing to downgrade past {revision}: {stranded} events row(s) have "
            "kind='corrupted', which the previous schema's ck_events_kind constraint does not "
            "admit. Downgrading would have to delete them or rewrite them to another kind — either "
            "destroys the record of a possible corruption. Accept or export those events, or "
            "deliberately remove them, then retry the downgrade."
        )
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_constraint("ck_events_kind", type_="check")
        batch_op.create_check_constraint("ck_events_kind", f"kind in {_KINDS_OLD}")
    op.drop_column("files", "prior_mtime")
    op.drop_column("files", "prior_size")
    op.drop_column("files", "prior_sha256")
