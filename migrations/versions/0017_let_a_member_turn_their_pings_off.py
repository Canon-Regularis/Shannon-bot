"""Let a member turn their pings off

This bot notifies somebody for nearly everything that happens to an item they are on. The metadata
block mentions the assignees and the reviewers, a line asks a new reviewer for a review, and an
`@login` typed into a GitHub comment is rewritten into a live Discord mention on the way through.
For anybody on a lot of items that is a steady stream, and until now there was no way to turn it
down: every one of those is a real `<@id>` and the client allows all of them.

What a member asked for is to stop being notified without disappearing from the thread, and
Discord has the mechanism. An id left off a message's `allowed_mentions` still renders as a
mention chip carrying their nickname; only the notification goes away. So the rendering does not
change at all, and this table is read on the way out to say who the message is permitted to reach.

The row is the whole of the fact. No column saying yes or no, because there is no third state:
somebody has asked to be left alone or they have not. That makes a backfill meaningless as well
as unnecessary, and a member who never runs the command keeps precisely the behaviour they have
today.

Not a column on `user_links`, which is where it would otherwise belong: that row is deleted and
rewritten every time anybody runs `/link`, because either half of it may be held by a different
row, and the warning about a login changing hands tells people to run it again. A preference kept
there would be wiped by the one action the bot asks for by name.

Keyed on the Discord account rather than on a link, so `/link` and `/mentions` may be run in
either order and somebody who has never linked still has a preference waiting for when they do.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "muted_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "discord_guild_id", "discord_user_id", name="uq_muted_members_guild_discord"
        ),
    )


def downgrade() -> None:
    op.drop_table("muted_members")
