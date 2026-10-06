"""Remember which account owns a repository

Found reviewing #201. A board stored with no owner means "this repository's own owner", and a board
number is a sequence GitHub keeps per account. So when a repository was transferred to another
account, the next delivery renamed the row and that null quietly re-pointed the server at the NEW
owner's board of the same number - a stranger's cards, read under the linker's authorisation. A
login cannot tell that apart from an account that was only renamed; the account id can. So the id
is kept here, and where the account behind the name changes the board is written down under the
old owner's login on the way, and stays where it was linked.

Nullable with no backfill, like `private` in `0019`: the next delivery fills it in. A row that has
not learned it yet and then moves to another owner's name cannot be told from a renamed account,
so its board is kept under the old login and taken off its linker - a board that stops opening
until somebody runs /board link, rather than one read under a login GitHub may have released.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repositories", sa.Column("github_owner_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Dropping the column is the whole of it, as it was for `0019`.

    A board an earlier transfer pinned to its old owner keeps that owner: it is an ordinary named
    owner by then, which the older code reads as it reads any other.
    """
    op.drop_column("repositories", "github_owner_id")
