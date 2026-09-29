"""Remember which board card wraps an item

Moving a card on a project board means addressing it by its own id, and nothing in this project
has ever recorded one for an issue or a pull request. It did not need to while the board was read
and never written: a card carries its content id, the content id finds the tracked row, and the
card id was used to look the row up and then thrown away.

It cannot be derived later. GitHub's REST API answers no per-item project lookup - the projectsV2
endpoints are addressed by owner and board number, so "which card wraps this issue" can only be
answered by reading a whole board and inverting it. That is exactly what the poller already does
once a minute, and it is the only place in this codebase where a card and an item are ever in
scope together, so it is the only thing that can write this down.

Nullable with no backfill, on the pattern `0019` set for `private` and `0026` for the board. Null
means no card has been seen for this item, which is every existing row and also every item whose
board has not been polled yet. A write with no card id does nothing rather than guessing.

BigInteger rather than Integer: card ids share a space with content ids, and the content id in
this project's own fixtures is already 2807646438, past 2**31.

No unique index, deliberately. A card id is unique within one board, and this project already
means to let a repository carry more than one - a constraint written now would have to be dropped
by the migration that does it, and a dropped constraint is worse than one nobody added.

Revision ID: 0027
Revises: 0026
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tracked_items", sa.Column("project_item_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_items", "project_item_id")
