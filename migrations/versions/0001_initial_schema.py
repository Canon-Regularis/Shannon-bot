"""Initial MVP 1 schema

Revision ID: 0001
Revises:
Create Date: 2026-08-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Values are spelled out rather than imported from shannon.domain so this revision keeps
# describing the schema as it was, even after the application enums grow.
OBJECT_TYPE = sa.Enum("PR", "ISSUE", "TICKET", name="object_type", native_enum=False, length=32)
ITEM_STATUS = sa.Enum(
    "NOT_REVIEWED",
    "IN_REVIEW",
    "READY_FOR_MERGE",
    "BACKLOG",
    "DONE",
    name="item_status",
    native_enum=False,
    length=32,
)
ITEM_PRIORITY = sa.Enum(
    "HIGH", "MEDIUM", "LOW", "UNSET", name="item_priority", native_enum=False, length=32
)
ACTOR_ROLE = sa.Enum(
    "AUTHOR",
    "ASSIGNEE",
    "REVIEWER",
    "PROJECT_MANAGER",
    name="actor_role",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_repo_id", sa.BigInteger(), nullable=False),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
        sa.UniqueConstraint("discord_guild_id", name="uq_repositories_discord_guild_id"),
        sa.UniqueConstraint("github_repo_id", name="uq_repositories_github_repo_id"),
    )

    op.create_table(
        "channel_mappings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("object_type", OBJECT_TYPE, nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_channel_mappings_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_mappings")),
        sa.UniqueConstraint("repository_id", "object_type", name="uq_channel_mappings_repo_type"),
    )

    op.create_table(
        "tracked_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repository_id", sa.Integer(), nullable=False),
        sa.Column("github_object_id", sa.BigInteger(), nullable=False),
        sa.Column("github_object_type", OBJECT_TYPE, nullable=False),
        sa.Column("github_object_number", sa.Integer(), nullable=False),
        sa.Column("github_url", sa.String(length=512), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("github_state", sa.String(length=32), nullable=False),
        sa.Column("discord_message_id", sa.BigInteger(), nullable=True),
        sa.Column("discord_thread_id", sa.BigInteger(), nullable=True),
        sa.Column("status", ITEM_STATUS, nullable=False),
        sa.Column("priority", ITEM_PRIORITY, nullable=False),
        sa.Column("github_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_tracked_items_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracked_items")),
        # One row per GitHub object per repository. Repeated webhook deliveries hit this
        # constraint instead of creating a second Discord thread.
        sa.UniqueConstraint(
            "repository_id",
            "github_object_type",
            "github_object_id",
            name="uq_tracked_items_repo_type_object",
        ),
    )
    op.create_index(
        "ix_tracked_items_discord_thread_id", "tracked_items", ["discord_thread_id"], unique=False
    )

    op.create_table(
        "item_assignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracked_item_id", sa.Integer(), nullable=False),
        sa.Column("github_username", sa.String(length=255), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("role_type", ACTOR_ROLE, nullable=False),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tracked_item_id"],
            ["tracked_items.id"],
            name=op.f("fk_item_assignments_tracked_item_id_tracked_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_item_assignments")),
        sa.UniqueConstraint(
            "tracked_item_id",
            "github_username",
            "role_type",
            name="uq_item_assignments_item_user_role",
        ),
    )

    op.create_table(
        "webhook_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("github_delivery_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
        sa.UniqueConstraint("github_delivery_id", name="uq_webhook_events_github_delivery_id"),
    )

    op.create_table(
        "user_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("github_username", sa.String(length=255), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_links")),
        sa.UniqueConstraint(
            "discord_guild_id", "github_username", name="uq_user_links_guild_github"
        ),
        sa.UniqueConstraint(
            "discord_guild_id", "discord_user_id", name="uq_user_links_guild_discord"
        ),
    )


def downgrade() -> None:
    op.drop_table("user_links")
    op.drop_table("webhook_events")
    op.drop_table("item_assignments")
    op.drop_index("ix_tracked_items_discord_thread_id", table_name="tracked_items")
    op.drop_table("tracked_items")
    op.drop_table("channel_mappings")
    op.drop_table("repositories")
