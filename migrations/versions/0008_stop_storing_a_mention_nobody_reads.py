"""Stop storing a mention nobody reads

`item_assignments.discord_user_id` was written on the ping hot path and read by nothing.
`ActorNotifier` resolves mentions from `user_links` at render time, every time, which is what
`user_links` exists for: its own docstring says pinging needs somewhere to read a Discord id from
before an assignment row exists. So this column could only ever hold a stale copy of the table
that was already being consulted.

The one thing that ever read it was a test assertion.

Nothing is backfilled on the way down. The column comes back nullable and empty, which is what a
re-linked account would have left it as anyway, and no code path fills it any more. Dropping a
column takes a brief exclusive lock but rewrites nothing; PostgreSQL marks it dropped and
reclaims the space on later writes.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-19

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("item_assignments", "discord_user_id")


def downgrade() -> None:
    op.add_column(
        "item_assignments",
        sa.Column("discord_user_id", sa.BigInteger(), nullable=True),
    )
