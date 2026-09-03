"""Remember which channel a thread is in

A thread deleted on its own is reported by Discord and the pointer to it is let go. A thread
deleted because its CHANNEL was deleted is reported only if discord.py still had it cached, and it
drops a thread from that cache the moment the thread archives. So a channel deletion says nothing
at all about the quiet threads inside it.

For a pull request or an issue that costs nothing, because the next webhook rebuilds either way.
For a draft card on a project board it is the end: its only visitor is the poller, which decides
from a stored timestamp and a stored pointer without asking Discord, so a card parked in a column
nobody touches again keeps pointing at a thread that no longer exists and is mirrored nowhere,
permanently, without a line in the log.

Clearing those pointers needs to know which items were in the channel that went, and nothing knew.
The channel mapping cannot answer it: `/set_channel` changes where NEW threads go and leaves the
existing ones where they were, so a mapping that has been changed since describes neither the old
threads nor reliably the new ones. Clearing by mapping would let go of threads that are alive in
the previous channel and open a second one beside each.

So the row records where its thread actually is, written at the moment the thread is claimed,
which is the only moment that is known for certain.

Nullable, because every row written before this has a thread whose channel nobody recorded. A null
is read as "not known to be in that channel" and is left alone, which is exactly what happened
before, so an existing item keeps the behaviour it had and starts being covered the first time its
thread is rebuilt.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tracked_items", sa.Column("discord_channel_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_items", "discord_channel_id")
