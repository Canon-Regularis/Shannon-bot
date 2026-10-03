"""Remember a card's board fields

Issue #182. A project card carries a creator, assignees, a priority, a story point, an iteration
and an area, and the block at the top of its thread shows them now. Saying what CHANGED needs the
previous values, and nothing was keeping them: the row held a card's column and its timestamp and
nothing else about the board.

One JSON column rather than a column for each field. The set of fields belongs to whoever owns the
board - they can add an `Area` this afternoon and rename `Story Point` tomorrow - so a column apiece
means a migration every time somebody does, and a schema that lags a board by a deploy. The values
stored are the board's own text, which is what the block shows, so the comparison and the row cannot
disagree about what a field says.

Nullable with no backfill, on the pattern `0019` set for `private`, `0026` for the board, `0027` for
the card and `0029` for the message. Null here carries a meaning the others do not, and it is the
one that matters most: it means this card's fields have never been seen, which is what makes the
first poll after this ships record them and say nothing. Without that, every card on a board
announces every field it has, all at once, the minute this is deployed. An empty object is a
different answer and means seen with nothing set.

Tickets only in practice. An issue or a pull request reaches its thread from its own webhooks and
has no board fields to compare, so its row keeps the null.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_items",
        sa.Column("shown_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tracked_items", "shown_fields")
