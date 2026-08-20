"""Remember which column a card was in

The board poller compared a card's column against the item's stored status and moved the item
whenever the two differed. That is not the same question as "has this card moved", and the
difference is not academic: a reviewer running /set_ready_for_merge on a pull request whose card
still sits in `In Progress` had their decision reverted by the next poll, silently, within the
interval. The board won every disagreement because a disagreement was all the poller could see.

What it needs to see is a change, which means remembering what it saw last time. This column is
that memory: the board column as of the last poll that looked at the card.

Nullable, and null means never seen. That case is decided rather than assumed: on first sight the
column is recorded, and applied only if the item is still NOT_REVIEWED, so a board fills in an
item nobody has said anything about and never overwrites a decision somebody made.

Nothing is backfilled. An empty column reads as never seen, which is true of every row at the
moment this runs, and the first poll after it records what the board says without acting on it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_items",
        sa.Column("project_column", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracked_items", "project_column")
