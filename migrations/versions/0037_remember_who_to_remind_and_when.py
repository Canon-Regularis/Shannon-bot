"""Remember who to remind and when

Issue #229. `/remind` pings somebody once, after a time somebody gives, and that time can be a year
away - far longer than any process this bot runs as lives. So the request is written down the
moment it is made, and a sender reads it back when it falls due. Kept only in memory, every
reminder set before a deploy, a crash or a restart would be lost, with nothing anywhere to say so.

A row is deleted once it has gone out, or once it has been given up on, so the table is the queue
and nothing else: what is in it is what is still owed. `logged_messages` is emptied the same way,
and for the same reason - a row that has done its job is only a copy of somebody's words.

`claimed_at` is a lease in the delivery queue's shape. A sender takes a reminder by stamping it,
and a stamp older than the retry window belongs to a sender that stopped before finishing, so the
next one takes it over. That makes a reminder go out at least once rather than at most once: a
process dying between Discord taking the post and the row being deleted sends it again. A
duplicate is visible and harmless; a lost reminder is neither.

No foreign keys. The server, the channel and both people are Discord's ids, and nothing here owns
them. One index, on `due_at`, for the one question the sender asks on every tick.

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-07

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("set_by_discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("set_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])


def downgrade() -> None:
    """Any reminder still waiting goes with the table, which is the only place it was written."""
    op.drop_index("ix_reminders_due_at", table_name="reminders")
    op.drop_table("reminders")
