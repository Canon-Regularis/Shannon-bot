"""Remember which board a link was for

Issue #201. Linking a board for the first time used to be two commands with a trip to GitHub
between them: authorise, come back, then link. `/board link` hands out ONE link instead, and
following it both authorises and links the board. So the board somebody chose has to survive the
trip to GitHub and back, and the callback is a browser arriving with nothing but a state - the
same position `0025` was in about which command asked.

It rides on the server-side row and never in the URL. The state stays the only thing in the link,
so nobody can edit the board on its way to GitHub and back, and the link reads exactly as it did.

Only a board link carries one. The owner is null rather than blank where nobody named one, which is
how `repositories.project_owner` spells the same absence.

Both nullable with no backfill, on the pattern `0019` set for `private`, `0026` for the board and
`0031` for whoever authorised it - and here the null is a meaning rather than a gap. Null means
authorise only, which is what every link handed out before this was, so every row that exists when
this runs already says the right thing: a link in flight across the upgrade finishes the way it was
issued to.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-04

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identity_verifications", sa.Column("board_number", sa.Integer(), nullable=True))
    op.add_column(
        "identity_verifications", sa.Column("board_owner", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    """Dropping the columns is the whole of it, as it was for `0025` on the same table.

    No row outlives the day, so there is no history here to lose. A board link in flight across the
    downgrade authorises without linking, which is all the code it is going back to ever did.
    """
    op.drop_column("identity_verifications", "board_owner")
    op.drop_column("identity_verifications", "board_number")
