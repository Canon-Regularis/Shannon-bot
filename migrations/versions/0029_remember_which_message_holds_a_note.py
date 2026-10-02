"""Remember which Discord message holds a mirrored note

Issue #165. A comment edited on GitHub left the Discord thread showing text that existed nowhere any
more, because the mirror posted the note and threw away the message it had posted. There was nothing
to edit: `mirrored_notes` recorded only that a note had been mirrored, never where.

The id cannot be derived later. Nothing on GitHub's side records which Discord message holds a
comment, and Discord offers no way to search a thread for the message mirroring a given one - the
only moment the two are in scope together is the instant the post returns, which is where this is
now written.

A second write rather than part of the claim, deliberately. The claim goes in BEFORE the post, for
the reason `MirroredNote` gives: the queue is at-least-once, so recording after the post would put
the same comment in a thread twice when a delivery was retried between the two. The message id only
exists after the post, so it lands as an update to the row the claim created.

Nullable with no backfill, on the pattern `0019` set for `private`, `0026` for the board and
`0027` for the card. Null means a note mirrored before this column existed. An edit for such a row
has nothing to point at and says so rather than guessing; it self-heals, because every note
mirrored from now on records one.

BigInteger, not Integer: a Discord snowflake is past 2**31, which is why every other Discord id in
this schema is one.

No index. The column is only ever read by a lookup that already has the row - `tracked_item_id` and
`note_key` are the unique key, and this is a field on what that finds.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("mirrored_notes", sa.Column("discord_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("mirrored_notes", "discord_message_id")
