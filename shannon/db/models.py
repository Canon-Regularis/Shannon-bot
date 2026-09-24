from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shannon.db.base import Base, TimestampMixin, varchar_enum
from shannon.domain.enums import (
    ActorRole,
    DeliveryStatus,
    ObjectType,
    Priority,
    Status,
    VerificationPurpose,
)
from shannon.domain.json import JsonObject

_LIVE_STATUSES = ", ".join(f"'{status.value}'" for status in DeliveryStatus.live())

# How much of a tracked item's text the row will hold; text from elsewhere is cut to fit
# before it is written. GitHub caps an issue title at 256, but a project board's draft card
# has no cap at all, and a Status column is whatever somebody typed.
TITLE_WIDTH = 512
URL_WIDTH = 512
COLUMN_WIDTH = 128

# Discord's own ceiling on a message is 4000 characters, so this cuts nothing Discord carried.
TRANSCRIPT_LINE_WIDTH = 4000
# A Discord global name and a per-server nickname are 32 characters each; the rest is slack.
DISPLAY_NAME_WIDTH = 128


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
    # Null reads as no evidence rather than as public: nothing can invent the answer for a row
    # written before the column existed. Rewritten from the repository object on every sync, so
    # it corrects itself on the next delivery rather than needing a backfill.
    private: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # The project board mirrored into this repository's server, by the number in its URL. Null
    # means none, which is what every row written before this was and needs no backfill.
    project_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Who owns that board, where it is not this repository's own owner. Null means it is. A board
    # number is a sequence GitHub keeps per account, so the pair addresses a board and neither
    # half does alone - which is why this is stored beside the number rather than derived.
    project_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
        # What stops a repeated webhook creating a second Discord thread.
        UniqueConstraint(
            "repository_id",
            "github_object_type",
            "github_object_id",
            name="uq_tracked_items_repo_type_object",
        ),
        # Comments and reviews are looked up by number, and the unique constraint above leads
        # with repository_id, so without this the planner scans every item in the repository.
        Index("ix_tracked_items_repo_number", "repository_id", "github_object_number"),
        # Every workflow command resolves the item from the thread it was run in, and `/label`
        # does it per keystroke inside the three seconds Discord allows an autocomplete.
        Index("ix_tracked_items_discord_thread_id", "discord_thread_id"),
        # The sweep that lets go of a whole channel's threads searches by this and nothing else.
        Index("ix_tracked_items_discord_channel_id", "discord_channel_id"),
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
    # Which channel the item's thread is in; the channel mapping cannot answer it, because
    # `/set_channel` leaves existing threads where they were. Here so a deleted channel can let
    # go of the threads inside it, which Discord reports only while discord.py has them cached.
    discord_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Which delivery last wrote this item, in the order deliveries reached this bot. The
    # staleness guard compares it when two deliveries carry the same `updated_at`, which GitHub
    # stamps to the second and so they routinely do. Null for a write from a command or the board.
    last_delivery_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Which label names a reader has been shown, not which labels the item has. Written by a
    # POSTED block and by a tag line that was said, never by an edit, because Discord tells a
    # reader nothing about an edit. Cleared with the pointer; null means no evidence.
    shown_labels: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # The lock this bot last set on the thread it points at. Null means it has not set one, and
    # it is cleared when the pointer moves, because a replacement thread starts open. Asking
    # Discord on every delivery instead would retry a refused permission for ever.
    discord_thread_locked: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    status: Mapped[Status] = mapped_column(
        varchar_enum(Status, "item_status"), nullable=False, default=Status.NOT_REVIEWED
    )
    priority: Mapped[Priority] = mapped_column(
        varchar_enum(Priority, "item_priority"), nullable=False, default=Priority.UNSET
    )
    github_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The board column as of the last poll, null if never seen. The poller compares against
    # this rather than `status`, which cannot tell a card that has just been dragged from one
    # that sat still while somebody set the status from Discord.
    project_column: Mapped[str | None] = mapped_column(String(COLUMN_WIDTH), nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="tracked_items")
    # Nothing reads this: assignments are fetched through ItemAssignmentStore, one role at a
    # time. `raise` keeps the mapping for the cascade and turns accidental use into an error.
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
    # Its own copy rather than a read through `user_links`: this records who GitHub said was
    # asked, whether or not anybody has linked them, and the ping path resolves from it long
    # after the payload that made it has gone.
    github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    role_type: Mapped[ActorRole] = mapped_column(
        varchar_enum(ActorRole, "actor_role"), nullable=False
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When GitHub says the request was made, on GitHub's clock rather than ours. It is the only
    # thing that can tell a request made again from the same request arriving twice, or stop a
    # review closing a request that came after it. Null reads as no evidence.
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the review was submitted, on GitHub's clock. The row is kept rather than removed so a
    # delivery captured before the review and retried after it cannot resurrect the request and
    # ping somebody to review what they just approved. Cleared by a request newer than the review.
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tracked_item: Mapped[TrackedItem] = relationship(back_populates="assignments")


class MirroredNote(TimestampMixin, Base):
    """A comment or review already posted into an item's thread.

    The queue is at-least-once: a delivery whose status could not be written is handled again
    once its lease expires. Every other handler is idempotent under that on its own; posting a
    note is not, so the claim goes in before the post, never after.
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

    A queue rather than a log: GitHub never redelivers a failed webhook and gives up on one that
    takes more than ten seconds, so the body is kept here and the work happens behind the
    response.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("github_delivery_id", name="uq_webhook_events_github_delivery_id"),
        # The lease reads `next_attempt_at IS NULL OR next_attempt_at <= now()`, which no index
        # can answer as a condition: an index leading on status fell back to a full table scan
        # once a few hundred deliveries were backing off. Partial, so it stays small.
        Index(
            "ix_webhook_events_live",
            "id",
            # Built from the enum, in `live()`'s order, so a sixth state cannot leave the
            # index behind. Alembic ignores an index's WHERE clause, so test_migrations cannot
            # see this; test_the_live_index_covers_exactly_the_live_statuses reads pg_indexes.
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
    # `varchar_enum` renders a plain VARCHAR(32) and emits no CHECK constraint.
    status: Mapped[DeliveryStatus] = mapped_column(
        varchar_enum(DeliveryStatus, "delivery_status"), nullable=False
    )

    # Nullable so the migration applies to a live table with nothing to backfill; the lease
    # requires a body, so older rows are never picked up. Without none_as_null SQLAlchemy stores
    # Python None as the JSON value `null`, which IS NOT NULL happily matches.
    payload: Mapped[JsonObject | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, server_default="0", default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Held by whichever worker is on this row. A worker that dies leaves the lease to expire
    # rather than stranding the delivery in PROCESSING forever.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class UserLink(TimestampMixin, Base):
    """Maps a GitHub login to a Discord account within one guild.

    Reviewer pinging needs somewhere to read `discord_user_id` from before an assignment row
    exists.
    """

    __tablename__ = "user_links"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", "github_username", name="uq_user_links_guild_github"),
        UniqueConstraint("discord_guild_id", "discord_user_id", name="uq_user_links_guild_discord"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_username: Mapped[str] = mapped_column(String(255), nullable=False)
    # Who that login belonged to when it was linked. GitHub frees a name the moment it is
    # renamed or deleted and lets anybody take it, so the name alone points at whoever holds it
    # now. Null reads as no evidence rather than as an answer.
    github_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class MutedMember(TimestampMixin, Base):
    """One member of one server who asked this bot not to notify them.

    The row is the fact: an absent row reads as pinged, so there is nothing to backfill. Its own
    table because `UserLinkStore.link` deletes and rewrites the `user_links` row, which would
    silently forget a preference kept there. Muting keeps the mention in the thread and takes
    away only the notification, unlike `quiet_metadata`, which strips the mention itself.
    """

    __tablename__ = "muted_members"
    __table_args__ = (
        UniqueConstraint(
            "discord_guild_id", "discord_user_id", name="uq_muted_members_guild_discord"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class TeamLink(TimestampMixin, Base):
    """Maps a GitHub team to a Discord role within one guild.

    One uniqueness rule, unlike `user_links`: a slug maps to one role, but several teams may
    point at the same Discord role.
    """

    __tablename__ = "team_links"
    __table_args__ = (
        UniqueConstraint("discord_guild_id", "github_team", name="uq_team_links_guild_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_team: Mapped[str] = mapped_column(String(255), nullable=False)
    discord_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GitHubInstallation(TimestampMixin, Base):
    """Which App installation covers one GitHub account.

    An installation is on an ACCOUNT rather than on a repository: install it on `octocat` and it
    covers whichever of that account's repositories were granted, so the route from a Discord
    server to a token is guild, then repository, then owner, then here. A fast path rather than
    the source of truth: the resolver falls back to asking GitHub, so a stale row costs a request.
    """

    __tablename__ = "github_installations"
    __table_args__ = (
        UniqueConstraint("installation_id", name="uq_github_installations_installation_id"),
        UniqueConstraint("account_login", name="uq_github_installations_account_login"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Stored lowercased: GitHub echoes back whatever case a payload was written with, so a
    # case-sensitive lookup would miss its own row.
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # GitHub suspends an installation rather than deleting it when somebody pauses the App, and
    # the token mint fails while it is suspended.
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class IdentityVerification(TimestampMixin, Base):
    """One outstanding "prove who you are on GitHub" link, and the only thing tying it back.

    Two commands hand these out and they are finished differently, which is what `purpose` is
    for: `/link` is done the moment the link is followed, and `/unregister` destroys a binding
    and everything mirrored under it, so it waits to be run again where there is somebody to
    report the answer to. Rows here outlive the
    unbinding they authorised: this table is keyed by guild, not by repository. The callback GitHub
    redirects to is unauthenticated, so `state` is CSRF token and session identifier at once.
    Consumed by an UPDATE filtering on `consumed_at IS NULL`, so two clicks race in the database.
    """

    __tablename__ = "identity_verifications"
    __table_args__ = (
        UniqueConstraint("state", name="uq_identity_verifications_state"),
        # The pruner looks for the slice past the expiry without reading the rest.
        Index("ix_identity_verifications_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Which command handed this out, because the callback cannot tell and has to answer the
    # browser with a next step. No Python-side default: both callers name a purpose, and a
    # default here would be a way for a third one to forget. The server default exists for the
    # ALTER and for a process still running the old code, not so that anything reads it.
    purpose: Mapped[VerificationPurpose] = mapped_column(
        varchar_enum(VerificationPurpose, "verification_purpose"),
        nullable=False,
        server_default=text("'LINK'"),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VerifiedIdentity(TimestampMixin, Base):
    """Who a Discord account proved they are on GitHub, and when they proved it.

    Kept rather than re-proved on every command, because the proof costs a browser visit, and the
    freshness rule lives with the service rather than here. Its own table for the reason
    `muted_members` is: `UserLinkStore.link` rewrites the `user_links` row from scratch.
    """

    __tablename__ = "verified_identities"
    __table_args__ = (
        UniqueConstraint(
            "discord_guild_id",
            "discord_user_id",
            name="uq_verified_identities_guild_discord",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    discord_guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    github_login: Mapped[str] = mapped_column(String(255), nullable=False)
    # Not nullable here, unlike the installation above: this row only ever comes from
    # `GET /user` answering about the account that just authorised, which always carries an id.
    github_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LoggedConversation(TimestampMixin, Base):
    """One Discord thread whose messages are being published to its GitHub item.

    A row is kept after it stops, so who turned logging on and when stays on record.
    `discord_thread_id` records which thread was armed, because a rebuild or a relocation can
    replace the item's own pointer, and the conversation would otherwise go on capturing in a
    thread the item no longer points at.
    """

    __tablename__ = "logged_conversations"
    __table_args__ = (
        # Partial, so the rule is one open conversation per item rather than one ever.
        Index(
            "uq_logged_conversations_open_item",
            "tracked_item_id",
            unique=True,
            postgresql_where=text("stopped_at IS NULL"),
        ),
        # Read once per captured message, on a table that only grows. Partial for the same
        # reason as the one above: both queries that search by thread ask for an open one.
        Index(
            "ix_logged_conversations_open_thread",
            "discord_thread_id",
            postgresql_where=text("stopped_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tracked_item_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_items.id", ondelete="CASCADE"), nullable=False
    )
    discord_thread_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_by_discord_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Null means it is still running.
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_by_discord_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # The batch in flight, and how far along the message rows it reaches. Held here so that one
    # flush at a time per conversation is a property of the schema rather than a rule somebody
    # keeps. A claim older than the retry window belongs to a process that died holding it.
    flush_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    flush_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    flush_through_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Consecutive failures, to bound a conversation GitHub will never accept. Reset by a flush
    # that lands.
    failed_flushes: Mapped[int] = mapped_column(nullable=False, server_default=text("0"), default=0)


class LoggedMessage(TimestampMixin, Base):
    """One captured Discord message, waiting to be published.

    Deleted once the comment carrying it lands: these rows hold what people said. Buffered in a
    table rather than in memory because GitHub can be down, and an in-memory buffer facing a
    failed write either grows without bound or drops part of a conversation silently.
    """

    __tablename__ = "logged_messages"
    __table_args__ = (
        # Capture is idempotent on this, and its index leads with `conversation_id`, which the
        # flush's ordered read and the delete that follows also use.
        UniqueConstraint(
            "conversation_id", "discord_message_id", name="uq_logged_messages_conversation_message"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("logged_conversations.id", ondelete="CASCADE"), nullable=False
    )
    discord_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discord_author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_display_name: Mapped[str] = mapped_column(String(DISPLAY_NAME_WIDTH), nullable=False)
    content: Mapped[str] = mapped_column(String(TRANSCRIPT_LINE_WIDTH), nullable=False)
    # Who this message tagged, by Discord id, with the name each had when it was said. The
    # content carries `<@123>` and this says who 123 is; without it the flush could name a
    # linked person and nobody else. JSON has no integer keys, so the ids are decimal strings.
    mentions: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    # Discord's clock rather than ours. The rendered line is stamped with it and the quiet gap is
    # measured against it, so a flush held up by an outage does not read as a thread that went
    # quiet and publish the moment the outage ends.
    said_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
