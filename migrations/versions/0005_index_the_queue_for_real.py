"""Index the queue for the queries it actually runs

Two measured plans, on a 200,000 row webhook_events:

The lease runs every two seconds forever, and had no index it could use. The one meant to serve
it, (status, next_attempt_at), can only ever contribute its first column, because the predicate
says `next_attempt_at IS NULL OR next_attempt_at <= now()` and neither half is an index
condition. While the queue is nearly empty `status` alone is selective enough and nothing looks
wrong. Once a few hundred deliveries are backing off, the planner decides `ORDER BY id LIMIT 10`
is cheaper through the primary key and walks the whole table: 126 ms and 24,637 buffers to
return nothing, and it gets worse as the retention window fills. A partial index over the live
rows is what the predicate can actually prove, and stays a few kB whatever the table does.

The prune runs hourly and had nothing to go on either, so it read every row to find the handful
past the retention window, and paid the same full scan in the hours it deletes nothing at all.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-13

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LIVE = "ix_webhook_events_live"
PROCESSED_AT = "ix_webhook_events_processed_at"
OLD = "ix_webhook_events_status_next_attempt"


def upgrade() -> None:
    op.create_index(
        LIVE,
        "webhook_events",
        ["id"],
        unique=False,
        postgresql_where="status IN ('PENDING', 'PROCESSING')",
    )
    op.create_index(PROCESSED_AT, "webhook_events", ["processed_at"], unique=False)
    op.drop_index(OLD, table_name="webhook_events")


def downgrade() -> None:
    op.create_index(OLD, "webhook_events", ["status", "next_attempt_at"], unique=False)
    op.drop_index(PROCESSED_AT, table_name="webhook_events")
    op.drop_index(LIVE, table_name="webhook_events")
