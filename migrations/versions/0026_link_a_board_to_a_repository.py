"""Link a project board to a repository

The board has been a process-wide pair of environment variables since the poller was written:
one number, one optional owner, one board for the whole deployment. That was never a link to a
repository, which is what issue #158 asks for - it was a link to a PROCESS, and the repository it
belonged to was inferred by scraping the owner out of whichever single repository happened to be
registered.

The inference is why the poller refuses to run at all with two repositories registered. Nothing
elected which one the board belonged to, so rather than mirror one server's board into one
server's channels and say nothing anywhere about the others, it stopped. That refusal is not a
guard against a hard problem; it is the shape of a missing column.

Two columns rather than a table of its own, deliberately. One repository gets one board. An item
appearing on several boards would need a table, an arbitration rule for two boards disagreeing
about a status, and a cap to keep N boards per minute inside one token's rate budget - and REST
cannot answer "which boards is this issue on" in the first place, so the discovery half of it is
not buildable here at all.

Both nullable, no backfill, on the pattern `0019` set for `private`. Null `project_number` means
no board, which is what every existing row is. Null `project_owner` means the repository's own
owner, which is exactly the fallback the poller already implements - so the meaning does not
change, only where it is read from.

The environment variables are NOT migrated into these columns. Alembic would have to guess which
row the operator meant, and the one case where the answer is unambiguous - exactly one registered
repository - is the case the runtime fallback already covers, where it can be re-evaluated on
every pass rather than frozen into a row at upgrade time.

Revision ID: 0026
Revises: 0025
Create Date: 2026-09-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repositories", sa.Column("project_number", sa.Integer(), nullable=True))
    op.add_column("repositories", sa.Column("project_owner", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("repositories", "project_owner")
    op.drop_column("repositories", "project_number")
