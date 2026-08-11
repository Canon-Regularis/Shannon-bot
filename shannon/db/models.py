from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shannon.db.base import Base, TimestampMixin
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status


def _enum(python_enum: type, name: str) -> Enum:
    """Store enums as VARCHAR plus a CHECK constraint.

    Native PostgreSQL enums would need an ALTER TYPE migration every time a later MVP adds a
    value, and MVP 3 and 4 both add to these sets.
    """
    return Enum(
        python_enum,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class Repository(TimestampMixin, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", name="uq_repositories_discord_guild_id"),
        UniqueConstraint("github_repo_id", name="uq_repositories_github_repo_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    repo_url: Mapped[str] = mapped_column(String(512), nullable=False)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # passive_deletes hands cascading to the database FKs, so deleting a repository does not
    # need every child row loaded into the session first.
    channel_mappings: Mapped[list[ChannelMapping]] = relationship(
        back_populates="repository", cascade="all, delete-orphan", passive_deletes=True
    )
    tracked_items: Mapped[list[TrackedItem]] = relationship(
        back_populates="repository", cascade="all, delete-orphan", passive_deletes=True
    )


class ChannelMapping(TimestampMixin, Base):
    __tablename__ = "channel_mappings"
    __table_args__ = (
        UniqueConstraint("repository_id", "object_type", name="uq_channel_mappings_repo_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    object_type: Mapped[ObjectType] = mapped_column(
        _enum(ObjectType, "object_type"), nullable=False
    )
    discord_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="channel_mappings")


class TrackedItem(TimestampMixin, Base):
    __tablename__ = "tracked_items"
    __table_args__ = (
        # This is what actually stops a repeated webhook creating a second Discord thread.
        UniqueConstraint(
            "repository_id",
            "github_object_type",
            "github_object_id",
            name="uq_tracked_items_repo_type_object",
        ),
        Index("ix_tracked_items_discord_thread_id", "discord_thread_id"),
        # Comments and reviews are looked up by number. The unique constraint above leads with
        # repository_id, so without this the planner scans every item in the repository and
        # filters, which grows with the repository rather than staying flat.
        Index("ix_tracked_items_repo_number", "repository_id", "github_object_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    github_object_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_object_type: Mapped[ObjectType] = mapped_column(
        _enum(ObjectType, "object_type"), nullable=False
    )
    github_object_number: Mapped[int] = mapped_column(nullable=False)
    github_url: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    github_state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[Status] = mapped_column(
        _enum(Status, "item_status"), nullable=False, default=Status.NOT_REVIEWED
    )
    priority: Mapped[Priority] = mapped_column(
        _enum(Priority, "item_priority"), nullable=False, default=Priority.UNSET
    )
    github_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    repository: Mapped[Repository] = relationship(back_populates="tracked_items")
    # Nothing reads this: assignments are always fetched through ItemAssignmentStore, which
    # asks for one role at a time. It was eager loading on every single fetch of a tracked
    # item, which is the hottest path there is. `raise` keeps the mapping for the cascade
    # while making any accidental use an error rather than a quiet extra query.
    assignments: Mapped[list[ItemAssignment]] = relationship(
        back_populates="tracked_item",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )


class ItemAssignment(TimestampMixin, Base):
    __tablename__ = "item_assignments"
    __table_args__ = (
        UniqueConstraint(
            "tracked_item_id",
            "github_username",
            "role_type",
            name="uq_item_assignments_item_user_role",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tracked_item_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_items.id", ondelete="CASCADE"), nullable=False
    )
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role_type: Mapped[ActorRole] = mapped_column(_enum(ActorRole, "actor_role"), nullable=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tracked_item: Mapped[TrackedItem] = relationship(back_populates="assignments")


class WebhookEvent(Base):
    """A delivery GitHub handed us, and how far we have got with it.

    This is a queue rather than a log. GitHub never redelivers a failed webhook and gives up on
    one that takes more than ten seconds, so the body is kept here and the work happens behind
    the response. Without that, a slow Discord call loses the event outright.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("github_delivery_id", name="uq_webhook_events_github_delivery_id"),
        Index("ix_webhook_events_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # Nullable so the migration applies to a live table with nothing to backfill. Rows written
    # before this existed have no body, and the lease requires one, so they are never picked up.
    # none_as_null, or SQLAlchemy would store Python None as the JSON value `null`, which is a
    # thing IS NOT NULL happily matches. The lease query leans on that check to skip rows that
    # have no body to act on.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, server_default="0", default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Held by whichever worker is on this row. A worker that dies leaves the lease to expire
    # rather than stranding the delivery in PROCESSING forever.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserLink(TimestampMixin, Base):
    """Maps a GitHub login to a Discord account within one guild.

    Not in the original requirements table list. Added because reviewer pinging needs somewhere
    to read `discord_user_id` from before an assignment row exists.
    """

    __tablename__ = "user_links"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", "github_username", name="uq_user_links_guild_github"),
        UniqueConstraint("discord_guild_id", "discord_user_id", name="uq_user_links_guild_discord"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
