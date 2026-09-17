"""Remember what the block showed a reader

An item opened with labels already on it is not one delivery. GitHub fires `opened` and a
`labeled` for each label together, which this project has recorded since the burst of six that
crashed all but one of them, and the tag line added much later never learned it. So opening an
issue with two labels posted the metadata block listing both, and then two lines announcing the
labels the block had just listed, for a change nobody made after the item existed.

The item's own labels cannot answer whether to say anything, because the block carries those and
they are identical either way. The question is narrower and nothing was asking it: which names has
a reader of this thread actually been shown.

Which is why only a POSTED block writes this, and an edit never does. Discord says nothing about
an edit — no message, no notification, no bump — so a `/set_done` that re-renders the block
seconds before its own `labeled` webhook arrives has shown the reader nothing, and the status line
that webhook produces is the only thing anybody but the person who ran the command will ever see.
Writing the column on every render would have silenced every workflow command in the bot, which is
a worse fault than the one being fixed.

Nullable, and null means no evidence rather than no labels. An item mirrored before this existed
announces every tag exactly as it does today, and gains the gate the first time its block is
posted again. The burst this fixes only happens to items opened after the deploy, so nothing is
left half mended.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_items",
        sa.Column("shown_labels", postgresql.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracked_items", "shown_labels")
