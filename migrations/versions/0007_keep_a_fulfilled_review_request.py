"""Keep a fulfilled review request instead of removing it

GitHub drops a reviewer from `requested_reviewers` the moment they submit, and sends no
`pull_request` event saying so, so the ledger followed that by deleting the assignment row. A
later re-request then re-created it with a null `notified_at` and the reviewer was asked again,
which is the whole point of the feature.

Deleting it turned out to be too much. The queue is at-least-once: a `pull_request` delivery
whose Discord step failed is retried with the payload it was captured with, and that payload
still lists the reviewer. Retried after the review, it found no row, inserted one, and pinged
somebody to review a pull request they had already approved. Worse, the row then existed with
`notified_at` set, which is exactly the state the ledger exists to prevent, so the next genuine
re-request found it already there and told nobody for the life of the pull request. Reproduced
end to end before this was written.

The row stays now and carries the time of the review instead. A request older than that is a
delivery catching up and is left alone; one newer is a person clicking re-request and clears it.
Comparing in GitHub's clock rather than ours is what makes the two distinguishable at all.

Nothing is backfilled: null means no review has closed this request, which is true of every row
written before this.

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
