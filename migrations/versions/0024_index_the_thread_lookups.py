"""Index the columns the thread lookups actually search by

0004 dropped `ix_tracked_items_discord_thread_id` on the grounds that nothing queried by thread
id, and said the index would come back in the same revision as the query that needed it.
`get_by_thread` arrived eight days later; the index did not. Every workflow command, every
`/label` autocomplete keystroke and every thread Discord reports as deleted now scans
`tracked_items` end to end.

`discord_channel_id` is the same table and the same shape: the sweep that lets go of a whole
channel's threads has nothing to go on either, and it runs once per channel deletion over every
item the server has ever tracked.

`logged_conversations.discord_thread_id` is read once per captured message in an armed thread,
on a table that only ever grows. Partial over the open conversations, because both queries that
search by thread ask for `stopped_at IS NULL` as well and the open ones are a handful of the
rows.

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-21

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

THREAD = "ix_tracked_items_discord_thread_id"
CHANNEL = "ix_tracked_items_discord_channel_id"
CAPTURING = "ix_logged_conversations_open_thread"


def upgrade() -> None:
    op.create_index(THREAD, "tracked_items", ["discord_thread_id"], unique=False)
    op.create_index(CHANNEL, "tracked_items", ["discord_channel_id"], unique=False)
    op.create_index(
        CAPTURING,
        "logged_conversations",
        ["discord_thread_id"],
        unique=False,
        postgresql_where="stopped_at IS NULL",
    )


def downgrade() -> None:
    op.drop_index(CAPTURING, table_name="logged_conversations")
    op.drop_index(CHANNEL, table_name="tracked_items")
    op.drop_index(THREAD, table_name="tracked_items")
