"""Remember which lines a thread asked for

Issue #163. Besides its block, an item's thread gets a line for each thing that happens to the
item, and `/track` lets one thread turn each kind of line on or off. This is where the answer is
kept.

One JSON column on the item rather than a table of choices. Every place that decides whether to
post a line already holds the item's row - the sync has it locked, the comment and CI paths load
it to find the thread, the board poller selects it - so a column rides along for nothing, where a
table would cost a query or a join on every delivery. And the kinds are this project's to grow, so
a kind added later is a new key rather than a migration, on `0030`'s reasoning for `shown_fields`.

Only what somebody chose is written, and nothing is backfilled: `{}` means nobody in that thread has
said, and every kind then follows the defaults the code holds. That is deliberate, and it is the
part of this that changes what existing threads see. The defaults turn tag, status and priority
lines and commit lines off, so from the moment this ships those go quiet in every thread until
somebody there asks for them, which is what the issue asked for.

Not null, with the empty object as the server default, so there is one spelling of "nobody chose"
and an update can merge into it without asking whether it is there. On the item and not on its
thread pointer, so a thread that is rebuilt or moved keeps the choice; nothing that clears the
pointer touches it.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_items",
        sa.Column(
            "tracking",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    """Every choice goes with the column, and every thread is back to being told everything the
    code before this told it."""
    op.drop_column("tracked_items", "tracking")
