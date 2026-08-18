"""Drop the unused index on tracked_items.discord_thread_id

Nothing queries by thread id. Every use of the column reads it off a row already found by
another predicate, or writes it, so the index was pure write cost on the hottest table. If a
command ever needs to resolve an item from the thread it ran in, the index comes back in the
same revision as the query that needs it.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-11

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_tracked_items_discord_thread_id", table_name="tracked_items")


def downgrade() -> None:
    op.create_index(
        "ix_tracked_items_discord_thread_id",
        "tracked_items",
        ["discord_thread_id"],
        unique=False,
    )
