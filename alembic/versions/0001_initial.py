"""initial schema: users, corpora, files, runs, events

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255)),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("username", name="uq_users_username"),
    )

    op.create_table(
        "corpora",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("root", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="worm"),
        sa.Column(
            "hash_cadence_seconds", sa.Integer(), nullable=False, server_default="900"
        ),
        sa.Column("ots_mode", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("exclude_globs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("alert_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("mode in ('worm','churn')", name="ck_corpora_mode"),
        sa.CheckConstraint("ots_mode in ('none','perfile')", name="ck_corpora_ots_mode"),
    )
    op.create_index("ix_corpora_user_id", "corpora", ["user_id"])

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("corpus_id", sa.Integer(), nullable=False),
        sa.Column("relpath", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("mtime", sa.Float()),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked", sa.DateTime(timezone=True)),
        sa.Column("last_changed", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("ots_path", sa.Text()),
        sa.Column("ots_state", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("ots_stamped_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["corpus_id"], ["corpora.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("corpus_id", "relpath", name="uq_files_corpus_relpath"),
        sa.CheckConstraint(
            "status in ('ok','new','modified','missing')", name="ck_files_status"
        ),
        sa.CheckConstraint(
            "ots_state in ('none','pending','incomplete','complete')",
            name="ck_files_ots_state",
        ),
    )
    op.create_index("ix_files_corpus_id", "files", ["corpus_id"])
    op.create_index("ix_files_status", "files", ["status"])

    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("corpus_id", sa.Integer(), nullable=False),
        sa.Column("started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished", sa.DateTime(timezone=True)),
        sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("modified", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stamped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("upgraded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.String(length=16), nullable=False, server_default="running"),
        sa.ForeignKeyConstraint(["corpus_id"], ["corpora.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "result in ('ok','error','partial','running')", name="ck_runs_result"
        ),
    )
    op.create_index("ix_runs_corpus_id", "runs", ["corpus_id"])

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("corpus_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer()),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.Integer()),
        sa.ForeignKeyConstraint(["corpus_id"], ["corpora.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acknowledged_by"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "kind in ('added','modified','missing','restored')", name="ck_events_kind"
        ),
    )
    op.create_index("ix_events_corpus_id", "events", ["corpus_id"])
    op.create_index("ix_events_file_id", "events", ["file_id"])
    op.create_index("ix_events_acknowledged_at", "events", ["acknowledged_at"])


def downgrade() -> None:
    op.drop_table("events")
    op.drop_table("runs")
    op.drop_table("files")
    op.drop_table("corpora")
    op.drop_table("users")
