"""one running run per corpus: partial unique index makes the single-writer claim atomic

The concurrency guard (corpora.active_run → launch scan) was a non-atomic check-then-act: a
``running`` Run was committed only after the scan's first awaits, so a near-simultaneous scheduler
tick or second POST re-checked, still saw nothing running, and started a SECOND concurrent scan on
the same corpus. A partial unique index on ``corpus_id WHERE result='running'`` makes the claim
atomic — inserting a second ``running`` row for a corpus raises IntegrityError, which the claim
sites (corpora.claim_run) treat as "already running" and refuse/skip.

Additive: existing rows are untouched (a healthy DB has at most one running run per corpus already;
the startup reaper terminates any stale ones before this index would matter). No table rebuild —
SQLite supports partial (``WHERE``-clause) indexes natively.

Revision ID: 0008_one_running_run_per_corpus
Revises: 0007_interrupted_run_result
Create Date: 2026-06-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_one_running_run_per_corpus"
down_revision = "0007_interrupted_run_result"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_runs_one_running_per_corpus",
        "runs",
        ["corpus_id"],
        unique=True,
        sqlite_where=sa.text("result = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_runs_one_running_per_corpus", table_name="runs")
