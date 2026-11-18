"""Remember whether a proof came through Discord

Issue #94. A proof is about to be able to let somebody in, where until now it could only keep
somebody out: with GitHub access standing in for a command tier, the account a member proved is
the account GitHub is asked about, and a write or admin answer for it opens commands their Discord
roles do not. That makes it matter whose account a row really names, which never mattered before.

`0033` is why it might not be theirs. Until then a one-time link pointed straight at GitHub's
consent page and the callback trusted nothing but its state, so a link forwarded to somebody who
had authorised the app before signed its ISSUER in as whoever clicked it. A row written that way
names somebody else's account under this member's name. For #158 that was harmless, because a
proof there can only refuse: the worst a borrowed account does is let through somebody who already
holds the Discord role. For a grant it is the whole risk, since the borrowed account's access is
what would be handed out.

Nothing on an existing row tells the two apart, so every existing row reads as false and nothing
is backfilled: no evidence must read as no when the answer raises access. Rows written after
`0033` are as genuine as the ones after this, and are treated the same anyway, which costs each
member one more `/link` before GitHub access counts for them. Every proof written from here on is
written true, because `redeem` is the only writer and since `0033` only the browser Discord bound
can reach it.

Not null with false as the server default, so there is one spelling of "not known to have come
through Discord" and a reader that forgets to ask is answered no. Nothing that only restricts
reads it: #158, `/linked` and the link's own standing go on as they were.

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verified_identities",
        sa.Column("through_discord", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )


def downgrade() -> None:
    """Every proof goes back to being one that can only refuse, which is all the code before this
    asked of one, so dropping the column loses nothing it reads."""
    op.drop_column("verified_identities", "through_discord")
