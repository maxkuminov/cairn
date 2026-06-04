"""ack informational events: backfill acks for existing added/restored events

Routine `added`/`restored` events used to be written unacknowledged and so nagged on the
dashboard. They are now born acknowledged (see scanner); this clears the existing backlog so the
deployed panel reflects only real nags (`missing` / worm `modified`) after upgrade.

Data-only — no schema change. Idempotent (a second run matches 0 rows). Downgrade is a no-op:
the pre-state (which routine events were unacknowledged) is not recoverable and re-nagging has no
value.

Revision ID: 0004_ack_informational_events
Revises: 0003_app_settings
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op

revision = "0004_ack_informational_events"
down_revision = "0003_app_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE events SET acknowledged_at = detected_at "
        "WHERE kind IN ('added', 'restored') AND acknowledged_at IS NULL"
    )


def downgrade() -> None:
    # No-op: cannot distinguish system-acked rows from user-acked ones, and re-nagging on
    # rollback is undesirable.
    pass
