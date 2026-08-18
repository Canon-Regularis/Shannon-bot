"""Index the queue for the queries it actually runs

The lease predicate is `next_attempt_at IS NULL OR next_attempt_at <= now()`, which no b-tree
can use, so (status, next_attempt_at) only ever contributed its leading column. That is
selective enough on a near-empty queue. Once a few hundred deliveries are backing off, the
planner prefers `ORDER BY id LIMIT 10` through the primary key and walks the table: 126 ms and
24,637 buffers to return nothing, on 200,000 rows. Keep this index partial over the live rows;
that is the only part of the predicate the planner can prove, and it stays a few kB whatever
the table does.

The hourly prune had nothing to go on either and scanned every row, including in the hours it
deletes nothing.

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
