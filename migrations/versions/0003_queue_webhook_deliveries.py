"""Turn webhook_events into a queue

GitHub never redelivers a failed webhook and records a failure if the endpoint takes more than
ten seconds. The handler was doing all its Discord work inline inside that budget, so a slow
call lost the event outright and nothing on this side could replay it, because only a hash of
the body was kept.

Every column here is nullable or defaulted, so this applies to a live table with no backfill.
Rows written before it have no payload, and the lease requires one, so none of them can be
picked up.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "webhook_events",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "webhook_events",
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "webhook_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "webhook_events",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("webhook_events", sa.Column("last_error", sa.Text(), nullable=True))

    op.create_index(
        "ix_webhook_events_status_next_attempt",
        "webhook_events",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_events_status_next_attempt", table_name="webhook_events")
    op.drop_column("webhook_events", "last_error")
    op.drop_column("webhook_events", "locked_until")
    op.drop_column("webhook_events", "next_attempt_at")
    op.drop_column("webhook_events", "attempts")
    op.drop_column("webhook_events", "payload")
