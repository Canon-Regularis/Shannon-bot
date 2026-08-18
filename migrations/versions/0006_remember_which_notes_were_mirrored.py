"""Remember which notes were mirrored

The delivery queue is at-least-once: a delivery whose status write fails stays leased, comes
back when the lease expires, and is handled again from the top. Mirroring kept no record of
having posted, so that replay put the same comment in the thread twice.

A row here is claimed before the post and handed back if the post does not land. Recording it
after the post would leave the identical gap one step further along, which is why
`item_assignments.notified_at` is claimed the same way.

The key carries the kind because GitHub numbers comments and reviews separately and the two
collide. Nothing is backfilled: every existing note has already been through the queue, so an
empty table cannot cause a repost.

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
