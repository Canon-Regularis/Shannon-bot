"""Remember whose board a link was chosen under

Found reviewing #201. A board link handed out for a bare number means "this repository's own
owner's board", and that is only worked out when the link is followed - up to ten minutes later.
A repository transferred in between turned the bare number into the NEW owner's board of the same
number: not the board the member picked from a list of the old owner's, read under the
authorisation they had just granted. So the owner the bare number meant is written beside the link
when it is handed out, and following it is refused where the repository has moved away from it.

Nullable with no backfill, on the pattern `0032`, `0033` and `0034` set on this table. Null where
an owner was named, which says the same thing whenever it is read, and on every link handed out
before this, which is finished as it would have been.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "identity_verifications",
        sa.Column("board_chosen_under", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Dropping the column is the whole of it, as it was for `0034` on the same table.

    A link handed out by the newer code is then followed without the question, as every board link
    was before this.
    """
    op.drop_column("identity_verifications", "board_chosen_under")
