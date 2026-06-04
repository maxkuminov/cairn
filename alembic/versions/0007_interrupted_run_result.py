"""interrupted run result: runs.result CHECK gains 'interrupted'

The startup orphaned-run reaper (scheduler.reap_orphaned_runs) marks a run left at
``result='running'`` by a killed/crashed process as terminal. It now uses ``interrupted`` (a benign
restart-induced interruption) rather than ``error`` (a genuine scan failure), so the two are
distinguishable. SQLite can't ALTER a CHECK in place, so the ``runs`` table is rebuilt via batch
mode to widen ``ck_runs_result`` (same pattern as 0005 for events.kind).

Additive: existing rows are untouched by the upgrade. Downgrade first relabels any ``interrupted``
rows back to ``error`` (so the narrower constraint still holds) before restoring the original CHECK.

Revision ID: 0007_interrupted_run_result
Revises: 0006_typed_progress_runs
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op

revision = "0007_interrupted_run_result"
down_revision = "0006_typed_progress_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_runs_result", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_result",
            "result in ('ok','error','partial','running','interrupted')",
        )


def downgrade() -> None:
    # Relabel reaped runs so the narrower constraint can be restored without violation.
    op.execute("UPDATE runs SET result = 'error' WHERE result = 'interrupted'")
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint("ck_runs_result", type_="check")
        batch_op.create_check_constraint(
            "ck_runs_result",
            "result in ('ok','error','partial','running')",
        )
