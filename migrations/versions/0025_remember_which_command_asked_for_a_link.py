"""Remember which command asked for a link

Two commands hand out a one-time authorisation link and the callback is shared, so when somebody
follows one the route has to answer a browser with a next step it cannot work out. Until now the
row said nothing about where the link came from, and the page said so in as many words: it told
everybody to "run the command again" because naming the wrong one would send somebody to a command
they cannot run.

That was survivable while running the command again was what both flows wanted. It stops being so
once `/link` is finished by the click itself, which is the whole of issue #144: the person opens
the link, GitHub says who they are, and the bot writes their link there and then. `/unregister`
must keep waiting to be run a second time, because the permission check and the unbinding need
somebody to report the answer to, and a browser page is not that.

So the row records which it was. A varchar rather than a native enum, like every other enum here:
a third purpose is then a value the application knows about rather than an ALTER TYPE.

Nothing is backfilled, and the reason is the table rather than laziness. A link is followable for
ten minutes; a spent or expired one survives at most a day more, because consuming stamps the row
rather than deleting it and the worker's hourly sweep is what clears them. Nothing ever reads a
spent row again, so whatever value those take is arbitrary.

The rows that matter are the ten minutes of followable ones, and their issuer cannot be recovered
from the row. `LINK` is the safe guess in both directions. A link handed out by the command that
used to be `/verify` was a link in everything but name, so calling it one is correct. A
`/unregister` link mislabelled this way still records the proof, because that happens whatever the
purpose says, so the admin's second run still finds it and still works; what it costs is one
`user_links` row written unasked, and that row is backed by the same GitHub answer the proof is.
The reverse default has no such fallback: it would tell somebody who has no business unregistering
anything to go and run `/unregister`, which is the exact confusion this column exists to end.

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values written out rather than imported from the application enum. A revision describes the
# schema as it was when it ran, so importing one would have this migration quietly change shape
# the day somebody adds a third purpose.
PURPOSE = sa.Enum(
    "LINK",
    "UNREGISTER",
    name="verification_purpose",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    # NOT NULL with a default, which on a modern PostgreSQL is a metadata-only change rather than
    # a rewrite. The default is also what lets a process still running the old code insert a row
    # without the column while the new one is rolling out.
    op.add_column(
        "identity_verifications",
        sa.Column("purpose", PURPOSE, nullable=False, server_default=sa.text("'LINK'")),
    )


def downgrade() -> None:
    """Dropping the column is the whole of it.

    Nothing to put back, unlike the backfill in `0021`: no row outlives the day, so there is no
    history here to lose. What returns is the callback page that cannot name a command.
    """
    op.drop_column("identity_verifications", "purpose")
