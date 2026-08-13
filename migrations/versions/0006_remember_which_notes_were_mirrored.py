"""Remember which notes were mirrored

The delivery queue is at-least-once by design. A delivery whose status could not be written
stays leased, comes back when the lease expires, and is handled again from the top. Every other
handler survives that: syncing an item upserts its row, swaps the thread pointer from the id it
read rather than writing over whatever is there, and claims a ping before sending it.

Mirroring a comment or a review had none of that. It posted, and kept no record of having
posted, so a status write that failed after a successful post put the same comment in the
thread twice. Reproduced against a live database by failing the status write once and letting
the lease run out.

This is the record. It is claimed before the post and handed back if the post does not land,
which is the same shape as `item_assignments.notified_at`, for the same reason: recording it
afterwards would leave the identical gap one step further along.

The key carries the kind because GitHub numbers comments and reviews separately and the two
collide. Nothing is backfilled. An empty table means the first delivery of each existing note
claims it, and every one of those has already been through the queue, so there is nothing left
to post twice.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mirrored_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tracked_item_id", sa.Integer(), nullable=False),
        sa.Column("note_key", sa.String(length=64), nullable=False),
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
        sa.ForeignKeyConstraint(["tracked_item_id"], ["tracked_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tracked_item_id", "note_key", name="uq_mirrored_notes_item_note"),
    )


def downgrade() -> None:
    op.drop_table("mirrored_notes")
