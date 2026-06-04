"""SQLAlchemy ORM for Cairn's five locked tables (DESIGN.md §5).

SQLite-friendly: plain columns + JSON-as-TEXT, enums as TEXT with CHECK constraints, UTC
timezone-aware datetimes. No Postgres types. The scanner is the single writer; WAL keeps panel
reads concurrent.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    collections: Mapped[list["Collection"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Resolved absolute path; must lie under the owner's admin-provisioned mounted base.
    root: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(16), default="worm", nullable=False)
    hash_cadence_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    # Deep-verify cadence: re-hash every tracked file this often (catches silent bit-rot the
    # size+mtime fast-path misses). Default weekly; 0 disables deep verify for this collection.
    verify_cadence_seconds: Mapped[int] = mapped_column(
        Integer, default=604800, nullable=False
    )
    ots_mode: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    # When true, the weekly deep-verify pass promotes intact `new` files to `ok` (so additions to a
    # steadily-growing collection don't pile up needing manual baselining). Off by default; never
    # auto-accepts `modified`/`missing` (that stays the explicit `accept`).
    auto_baseline_new: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exclude_globs_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    alert_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    # Wall-clock of the last completed deep (full re-hash) pass; NULL = never deep-scanned (owed).
    last_full_scan_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("mode in ('worm','churn')", name="ck_collections_mode"),
        CheckConstraint("ots_mode in ('none','perfile')", name="ck_collections_ots_mode"),
    )

    owner: Mapped["User"] = relationship(back_populates="collections")
    files: Mapped[list["FileEntry"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )
    runs: Mapped[list["Run"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )
    events: Mapped[list["Event"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )


class FileEntry(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(Text, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    mtime: Mapped[float | None] = mapped_column(Float)  # epoch seconds, for fast-path diffing
    sha256: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="new", nullable=False)
    ots_path: Mapped[str | None] = mapped_column(Text)
    ots_state: Mapped[str] = mapped_column(String(16), default="none", nullable=False)
    ots_stamped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("collection_id", "relpath", name="uq_files_collection_relpath"),
        CheckConstraint(
            "status in ('ok','new','modified','missing')", name="ck_files_status"
        ),
        CheckConstraint(
            "ots_state in ('none','pending','incomplete','complete')",
            name="ck_files_ots_state",
        ),
        Index("ix_files_status", "status"),
    )

    collection: Mapped["Collection"] = relationship(back_populates="files")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # What kind of operation this run records: an integrity 'scan', an on-demand OTS 'stamp'
    # backfill, or the daily 'upgrade' pass. Only 'scan' runs count toward scan freshness.
    kind: Mapped[str] = mapped_column(String(16), default="scan", nullable=False)
    started: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    finished: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Live progress: items handled so far (files walked / stamped / proofs upgraded), written as
    # the operation runs so a concurrent reader sees a growing count. ``total`` is the planned
    # denominator (NULL = unknown → indeterminate progress; a scan estimates it from the prior run).
    processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total: Mapped[int | None] = mapped_column(Integer)
    added: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    modified: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    missing: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Files reconciled as a move/rename this run (excluded from added/missing — same content,
    # new path). See scanner._reconcile_moves.
    moved: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stamped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    upgraded: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # True when this run re-hashed every tracked file (deep verify) rather than fast-pathing.
    deep: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    result: Mapped[str] = mapped_column(String(16), default="running", nullable=False)

    __table_args__ = (
        CheckConstraint(
            "result in ('ok','error','partial','running','interrupted')",
            name="ck_runs_result",
        ),
        CheckConstraint(
            "kind in ('scan','stamp','upgrade')", name="ck_runs_kind"
        ),
        # At most one in-progress run per collection: the partial unique index makes the single-writer
        # claim atomic. A second concurrent insert of a `running` row (a near-simultaneous manual op
        # + scheduler tick) raises IntegrityError, which the claim site treats as "already running".
        Index(
            "uq_runs_one_running_per_collection",
            "collection_id",
            unique=True,
            sqlite_where=text("result = 'running'"),
        ),
    )

    collection: Mapped["Collection"] = relationship(back_populates="runs")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_id: Mapped[int | None] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Free-text context for the event; currently records "old → new" relpath for a `moved` file.
    detail: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    acknowledged_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('added','modified','missing','restored','moved')",
            name="ck_events_kind",
        ),
    )

    collection: Mapped["Collection"] = relationship(back_populates="events")


class AppSetting(Base):
    """Global, UI-editable app config as a simple key-value store.

    Holds runtime-configurable settings that would otherwise be env-only (currently the SMTP
    server config). Values are stored as TEXT; the service layer coerces them back to the typed
    :class:`~src.config.Settings` fields and overlays them over the env defaults (DB wins).
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
