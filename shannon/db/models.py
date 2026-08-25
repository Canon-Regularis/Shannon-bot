from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shannon.db.base import Base, TimestampMixin, varchar_enum
from shannon.domain.enums import ActorRole, DeliveryStatus, ObjectType, Priority, Status

_LIVE_STATUSES = ", ".join(f"'{status.value}'" for status in DeliveryStatus.live())

# How much of a tracked item's text the row will hold. Named because text that came from
# somewhere else has to be cut to fit before it is written, and a width written down twice is a
# width that drifts. GitHub caps an issue title at 256, but a project board's draft card has no
# cap at all, and a Status column is whatever somebody typed.
TITLE_WIDTH = 512
URL_WIDTH = 512
COLUMN_WIDTH = 128


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
        varchar_enum(ObjectType, "object_type"), nullable=False
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
        varchar_enum(ObjectType, "object_type"), nullable=False
    )
    github_object_number: Mapped[int] = mapped_column(nullable=False)
    github_url: Mapped[str] = mapped_column(String(URL_WIDTH), nullable=False)
    title: Mapped[str] = mapped_column(String(TITLE_WIDTH), nullable=False)
    github_state: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    discord_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_thread_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[Status] = mapped_column(
        varchar_enum(Status, "item_status"), nullable=False, default=Status.NOT_REVIEWED
    )
    priority: Mapped[Priority] = mapped_column(
        varchar_enum(Priority, "item_priority"), nullable=False, default=Priority.UNSET
    )
    github_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The board column as of the last poll that looked at this item. Null means never seen. The
    # poller needs to know whether a card MOVED, and comparing its column against the stored
    # status answers a different question: it cannot tell a card that has just been dragged from
    # one that has sat still while somebody set the status from Discord.
    project_column: Mapped[str | None] = mapped_column(String(COLUMN_WIDTH), nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="tracked_items")
    # Nothing reads this: assignments are fetched through ItemAssignmentStore, one role at a
    # time. `raise` keeps the mapping for the cascade while turning any accidental use into an
    # error instead of a quiet extra query on the hottest path there is.
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
    role_type: Mapped[ActorRole] = mapped_column(
        varchar_enum(ActorRole, "actor_role"), nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When GitHub says the request this row represents was made. The other two stamps are on our
    # clock and say what we did about the row; this one is on GitHub's and says what the row is,
    # which is the only thing that can tell a request made again from the same request arriving
    # twice, or stop a review closing a request that came after it. Null on rows written before
    # it existed, and read as no evidence rather than as an answer.
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the review this row asked for was submitted, in GitHub's clock rather than ours.
    # The row is kept rather than removed so a delivery captured before the review, and retried
    # after it, cannot resurrect the request and ping somebody to review what they just approved.
    # Cleared again by a request that is genuinely newer than the review, which is what a person
    # clicking re-request looks like.
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tracked_item: Mapped[TrackedItem] = relationship(back_populates="assignments")


class MirroredNote(TimestampMixin, Base):
    """A comment or review already posted into an item's thread.

    The queue is at-least-once on purpose: a delivery whose status could not be written stays
    leased and is handled again once the lease expires. Every other handler is idempotent under
    that on its own; posting a note is not, so this table carries the idempotency for it.

    The claim goes in before the post, never after. Recording afterwards moves the gap one step
    along instead of closing it.
    """

    __tablename__ = "mirrored_notes"
    __table_args__ = (
        UniqueConstraint("tracked_item_id", "note_key", name="uq_mirrored_notes_item_note"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tracked_item_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_items.id", ondelete="CASCADE"), nullable=False
    )
    # `comment:123` or `review:123`. GitHub numbers the two separately and they collide, so the
    # kind belongs in the key rather than in a column nothing would think to filter on.
    note_key: Mapped[str] = mapped_column(String(64), nullable=False)


class WebhookEvent(Base):
    """A delivery GitHub handed us, and how far we have got with it.

    This is a queue rather than a log. GitHub never redelivers a failed webhook and gives up on
    one that takes more than ten seconds, so the body is kept here and the work happens behind
    the response. Without that, a slow Discord call loses the event outright.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("github_delivery_id", name="uq_webhook_events_github_delivery_id"),
        # The lease reads `next_attempt_at IS NULL OR next_attempt_at <= now()`, which no index
        # can answer as a condition, so an index leading on status only ever contributed its
        # first column and the planner abandoned it for a full table scan as soon as a few
        # hundred deliveries were backing off. This is what the predicate can actually prove,
        # and it covers only the live rows so it stays small however long deliveries are kept.
        Index(
            "ix_webhook_events_live",
            "id",
            # Built from the enum so a sixth state cannot leave the index behind, and
            # `live()` returns an ordered tuple so the string comes out the same every time.
            #
            # Not checked by the schema diff in test_migrations, whatever this used to say:
            # alembic's PostgreSQL comparison ignores an index's WHERE clause, so widening this
            # predicate, narrowing it or deleting it outright all leave that test answering with
            # no differences. `test_the_live_index_covers_exactly_the_live_statuses` reads the
            # predicate back out of pg_indexes instead, which is what actually holds the two
            # together.
            postgresql_where=text(f"status IN ({_LIVE_STATUSES})"),
        ),
        # Pruning has to find the slice past the retention window without reading the rest.
        Index("ix_webhook_events_processed_at", "processed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    github_delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # Nullable so the migration applies to a live table with nothing to backfill; the lease
    # requires a body, so rows written before this existed are never picked up. none_as_null is
    # what makes that hold: without it SQLAlchemy stores Python None as the JSON value `null`,
    # which IS NOT NULL happily matches.
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


class TeamLink(TimestampMixin, Base):
    """Maps a GitHub team to a Discord role within one guild.

    The sibling of `user_links`, and separate from it because the two halves are different kinds
    of thing: a login belongs to one person and a slug belongs to a group, and Discord mentions
    them with different syntax. Folding them into one table would mean a column that is a user id
    on some rows and a role id on others, which is the shape that makes a query have to ask which
    kind of row it is looking at.

    Only one uniqueness rule, unlike `user_links`. A slug maps to one role, but two GitHub teams
    pointing at one Discord role is a reasonable thing to want: a server may have a single
    `@reviewers` role that several teams should reach.
    """

    __tablename__ = "team_links"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", "github_team", name="uq_team_links_guild_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_team: Mapped[str] = mapped_column(String(255), nullable=False)
    discord_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
