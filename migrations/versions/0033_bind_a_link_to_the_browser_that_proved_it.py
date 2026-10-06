"""Bind a link to the browser that proved it

Found reviewing #201. A one-time link was a bearer credential: it pointed straight at GitHub's
consent page, the callback trusted nothing but the state in its query string, and GitHub skips that
page for an application somebody has already authorised. So a link forwarded to someone who had
authorised the app before signed its ISSUER in as whoever clicked it - a name under `/link`, the
evidence `/register` and `/unregister` act on, and for a board a `project` token that acts as the
person who clicked. Every purpose shared it.

A link now opens on this bot first and goes to Discord, and only once Discord names the member the
row was issued for is the browser written down here: as a keyed hash of a cookie that browser alone
holds, never the cookie itself. The GitHub half is then spent only by that same browser, and the
state on its own completes nothing.

Nullable with no backfill, on the pattern `0025` and `0032` set on this table - and here null is the
safe answer rather than a gap. It means no browser has proved anything, which is exactly what every
link handed out before this must read as: it pointed straight at GitHub, so its callback now finds a
row nothing has bound and is told the link has expired, which costs the person ten minutes at most.

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "identity_verifications", sa.Column("bound_browser", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    """Dropping the column is the whole of it, as it was for `0032` on the same table.

    A link handed out by the newer code points at `/oauth/start`, which the older code answers with
    a 404, so the person runs the command again and gets a link the older code can finish.
    """
    op.drop_column("identity_verifications", "bound_browser")
