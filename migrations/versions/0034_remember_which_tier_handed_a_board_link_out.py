"""Remember which tier handed a board link out

Found reviewing #201. A board link does something the moment it is followed: it keeps a GitHub
authorisation that acts as the member, and for `/board link` it points the server at a board. The
member's Discord role was checked when the command ran, which can be ten minutes before the link
is followed, and nothing asked again. Now the tiers the command was gated on are written beside
the link, and the callback asks Discord, before anything is kept, whether the member still holds
one of them.

Nullable with no backfill, on the pattern `0032` and `0033` set on this table. Identity links
never carry one: a `/register` or `/unregister` link records a proof and nothing more, and running
the command again asks for the role again, and a `/link` link binds the account, but linking
yourself takes no role. A board link handed out before this holds null as well, and is asked
about as administrators only - the narrowest answer, for ten minutes at most.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("identity_verifications", sa.Column("tier", sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Dropping the column is the whole of it, as it was for `0033` on the same table.

    A board link handed out by the newer code is then finished without the second question, as
    every board link was before this.
    """
    op.drop_column("identity_verifications", "tier")
