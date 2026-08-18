"""Keep a fulfilled review request instead of removing it

GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
`pull_request` event saying so. The ledger used to delete the assignment row to match, so a
later re-request re-created it with a null `notified_at` and pinged the reviewer again.

Deletion does not survive at-least-once delivery. A retried `pull_request` payload still lists
the reviewer, so a retry after the review re-inserted the row, pinged somebody who had already
approved, and left `notified_at` set, which silenced the next genuine re-request for the life
of the pull request.

The row stays now and carries the time of the review. A request older than that is a delivery
catching up and is left alone; a newer one is a person clicking re-request, and clears it. The
comparison runs in GitHub's clock; ours cannot tell the two apart.

Nothing is backfilled: null means no review has closed this request, true of every existing row.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-13

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "item_assignments",
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("item_assignments", "fulfilled_at")
