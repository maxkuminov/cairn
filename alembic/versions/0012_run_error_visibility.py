"""run error visibility: runs.errors + runs.error_sample

Two purely additive columns for the add-fleet-review-and-run-health change (design D6). No table
rebuild, no CHECK change — plain ADD COLUMNs, which SQLite supports directly.

- `runs.errors` — INTEGER NOT NULL DEFAULT 0. `RunSummary.errors` already exists in the scanner and
  already decides whether a run finishes `partial`; this is the column that count was always
  missing, so a `partial` result can say how many files it skipped instead of only that it skipped
  some.
- `runs.error_sample` — nullable TEXT holding a **bounded, ASCII-only JSON array of diagnostic
  renderings** of the skipped files (20 entries / 256 B per entry / 4096 B serialized — design D6),
  each prefixed with its cause. JSON because that is this codebase's established shape for a blob
  column (`exclude_globs_json`, `alert_json`) and it is SQLite-friendly. The entries are a
  *rendering*, never a path: the headline skip cause is a name that could not be stored as TEXT in
  the first place, so writing the raw name here would reproduce, in the column added to report it,
  the `UnicodeEncodeError` that `tolerate-unencodable-paths` fixed.

Existing rows are untouched: every pre-0012 run reads `errors = 0` (the server default) and
`error_sample = NULL`.

The downgrade drops both columns and is deliberately **symmetric** — unlike `0011`, which refuses.
Nothing is lost that the logs do not also hold: every skip is also emitted as a WARNING at scan
finalize naming the count and the same capped sample, so these columns are a convenience copy of
information the operator's log already carries. That claim is conditional on that finalize WARNING
existing; if it is ever dropped, this downgrade's honesty must be re-examined with it.

Revision ID: 0012_run_error_visibility
Revises: 0011_proof_provenance_and_restored_changed
Create Date: 2026-08-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_run_error_visibility"
down_revision = "0011_proof_provenance_and_restored_changed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL with a server default so the ADD COLUMN backfills existing rows to 0 in place —
    # no rebuild, and a run that predates this column truthfully reports no recorded skips.
    op.add_column(
        "runs", sa.Column("errors", sa.Integer(), nullable=False, server_default="0")
    )
    # Nullable: NULL means "no sample recorded" (a pre-0012 run, or a run that skipped nothing).
    op.add_column("runs", sa.Column("error_sample", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "error_sample")
    op.drop_column("runs", "errors")
