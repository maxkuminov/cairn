"""oidc identity: users.oidc_issuer/oidc_subject/oidc_linked_at + users.session_epoch

Four purely additive columns for the oidc-sso-login change (design D5), plus one unique index:

- `oidc_issuer`    — nullable, CHECK `length() <= 512` (`ck_users_oidc_issuer_len`).
- `oidc_subject`   — nullable, CHECK `length() <= 255` (`ck_users_oidc_subject_len`).
- `oidc_linked_at` — nullable timestamp; carries the cross-column all-or-none CHECK
  (`ck_users_oidc_all_or_none`): the three OIDC columns are all NULL or all non-NULL.
- `session_epoch`  — INTEGER NOT NULL DEFAULT 0; bumped on link/unlink/revoke-sessions.
- `uq_users_oidc_identity` — UNIQUE index on (`oidc_issuer`, `oidc_subject`).

SQLite ignores `VARCHAR(N)`, hence the explicit `length()` CHECKs.

**This migration NEVER rebuilds `users`, in either direction.** Alembic's batch mode copies a
SQLite table into a new one, drops the old one and renames. `alembic/env.py` turns
`PRAGMA foreign_keys=ON` on for every migration connection, and `collections.user_id` is
`ON DELETE CASCADE` (`files`, `runs`, `events` cascade from `collections`, and
`events.acknowledged_by` is `ON DELETE SET NULL`), so dropping the old `users` table would delete
every collection and its whole integrity history. Therefore:

- upgrade = plain `ALTER TABLE users ADD COLUMN …` statements (no row is copied), the all-or-none
  CHECK on the last OIDC column added so every column it names already exists, then
  `CREATE UNIQUE INDEX`;
- downgrade = drop the index, then `oidc_linked_at` (its definition carries the cross-column
  CHECK), `oidc_subject`, `oidc_issuer`, `session_epoch`, each with plain `ALTER TABLE users DROP
  COLUMN`. SQLite refuses to drop a column that is still indexed or named by another column's
  CHECK, which is why this order is fixed;
- `DROP COLUMN` needs SQLite >= 3.35: the migration REFUSES below that (both directions, so an
  upgrade never lands on a database that could not be downgraded in place) rather than falling
  back to a rebuild.

Never replace these statements with `op.batch_alter_table("users")`. The preservation test
(`tests/test_migration_0013.py`) traces every statement and fails on a `users` rebuild.

Revision ID: 0013_oidc_identity
Revises: 0012_run_error_visibility
Create Date: 2026-11-04
"""

from __future__ import annotations

from alembic import op

revision = "0013_oidc_identity"
down_revision = "0012_run_error_visibility"
branch_labels = None
depends_on = None

_MIN_SQLITE = (3, 35, 0)

_UPGRADE = (
    "ALTER TABLE users ADD COLUMN session_epoch INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN oidc_issuer VARCHAR(512) "
    "CONSTRAINT ck_users_oidc_issuer_len "
    "CHECK (oidc_issuer IS NULL OR length(oidc_issuer) <= 512)",
    "ALTER TABLE users ADD COLUMN oidc_subject VARCHAR(255) "
    "CONSTRAINT ck_users_oidc_subject_len "
    "CHECK (oidc_subject IS NULL OR length(oidc_subject) <= 255)",
    "ALTER TABLE users ADD COLUMN oidc_linked_at DATETIME "
    "CONSTRAINT ck_users_oidc_all_or_none CHECK ("
    "(oidc_issuer IS NULL AND oidc_subject IS NULL AND oidc_linked_at IS NULL) OR "
    "(oidc_issuer IS NOT NULL AND oidc_subject IS NOT NULL AND oidc_linked_at IS NOT NULL))",
    "CREATE UNIQUE INDEX uq_users_oidc_identity ON users (oidc_issuer, oidc_subject)",
)

_DOWNGRADE = (
    "DROP INDEX uq_users_oidc_identity",
    "ALTER TABLE users DROP COLUMN oidc_linked_at",
    "ALTER TABLE users DROP COLUMN oidc_subject",
    "ALTER TABLE users DROP COLUMN oidc_issuer",
    "ALTER TABLE users DROP COLUMN session_epoch",
)


def _require_sqlite_335() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":  # pragma: no cover - Cairn is SQLite-only
        raise RuntimeError(f"0013_oidc_identity supports SQLite only, not {bind.dialect.name}")
    raw = bind.exec_driver_sql("SELECT sqlite_version()").scalar()
    version = tuple(int(p) for p in str(raw).split(".")[:3])
    if version < _MIN_SQLITE:
        raise RuntimeError(
            f"0013_oidc_identity needs SQLite >= 3.35 for in-place ALTER TABLE DROP COLUMN; "
            f"this interpreter links SQLite {raw}. Refusing rather than rebuilding `users`."
        )


def upgrade() -> None:
    _require_sqlite_335()
    for stmt in _UPGRADE:
        op.execute(stmt)


def downgrade() -> None:
    _require_sqlite_335()
    for stmt in _DOWNGRADE:
        op.execute(stmt)
