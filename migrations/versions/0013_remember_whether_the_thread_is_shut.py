"""Remember whether the thread is shut

The lock on a thread this bot opened was asked for once, on the one delivery attempt that
actually created it, and never again. Three ordinary things can fail after the thread is claimed
onto the row and before the lock lands: the lock itself refused, the reviewer ping refused, a
thread that opens but cannot be written to. Any of them and the retry finds the thread already
there, decides there is nothing to ask for, and records the delivery handled. A finished pull
request is then left with a thread anybody can post in, above a block reading DONE, and nothing
anywhere ever revisits it, because `PullRequestPolicy.locked` answers None for every payload and
the sync is the only thing that locks one automatically.

Asking Discord on every delivery instead is not the answer: a lock a permission will never grant
would then be asked for again on every event for as long as the permission is missing, which is
the unbounded retry an earlier look wrote a guard against.

So the row remembers. Null means this bot has not set the lock on the thread it currently points
at, which is what a brand new thread is and what every existing row is; true and false are the
two states it has put a thread into. It is cleared whenever the pointer moves, because a
replacement thread starts open however the one it replaces ended.

Nullable, and null is read as "not shut" rather than as "unknown", so an existing row for a
finished item is locked once on its next delivery and then left alone.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-01

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tracked_items", sa.Column("discord_thread_locked", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_items", "discord_thread_locked")
