"""Stream a Discord conversation into a GitHub comment

Issue #103. Everything this bot has done until now runs one way: GitHub sends a webhook and a
Discord thread is written to. These two tables carry the other direction.

`logged_conversations` is which threads are being captured. The row is what survives a restart, and
it is kept after the conversation stops rather than deleted: publishing what people said into a
public repository is worth being able to say afterwards who turned it on and when. The uniqueness
rule is therefore partial, one OPEN conversation per item rather than one ever, or an item could
never be logged a second time.

`logged_messages` is the buffer. It exists because GitHub can be down, and an in-memory buffer
facing a failed write either grows without bound or drops the batch, which loses part of a
conversation with nothing anywhere saying so. Rows are deleted as soon as the comment carrying
them lands, and kept no longer, because they hold what people said.

The flush claim lives on the conversation rather than on the message rows, so one row is updated
per batch instead of all of them, and one flush at a time per conversation is a property of the
schema rather than a rule somebody has to keep.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "logged_conversations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracked_item_id", sa.Integer(), nullable=False),
        sa.Column("discord_thread_id", sa.BigInteger(), nullable=False),
        sa.Column("started_by_discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_by_discord_user_id", sa.BigInteger(), nullable=True),
        sa.Column("flush_id", sa.String(length=36), nullable=True),
        sa.Column("flush_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flush_through_id", sa.BigInteger(), nullable=True),
        sa.Column("failed_flushes", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
            name=op.f("fk_logged_conversations_tracked_item_id_tracked_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_logged_conversations")),
    )
    # Partial rather than a plain unique constraint, so an item can be logged again once the
    # first conversation has stopped. This is also what the start command's refusal reads.
    op.create_index(
        "uq_logged_conversations_open_item",
        "logged_conversations",
        ["tracked_item_id"],
        unique=True,
        postgresql_where=sa.text("stopped_at IS NULL"),
    )
    op.create_table(
        "logged_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column("discord_message_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_author_id", sa.BigInteger(), nullable=False),
        sa.Column("author_display_name", sa.String(length=128), nullable=False),
        sa.Column("content", sa.String(length=4000), nullable=False),
        sa.Column("said_at", sa.DateTime(timezone=True), nullable=False),
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
            ["conversation_id"],
            ["logged_conversations.id"],
            name=op.f("fk_logged_messages_conversation_id_logged_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_logged_messages")),
        # Capture is idempotent on this, and its index also serves the ordered read the flush
        # does and the delete that follows it.
        sa.UniqueConstraint(
            "conversation_id", "discord_message_id", name="uq_logged_messages_conversation_message"
        ),
    )


def downgrade() -> None:
    op.drop_table("logged_messages")
    op.drop_index("uq_logged_conversations_open_item", table_name="logged_conversations")
    op.drop_table("logged_conversations")
