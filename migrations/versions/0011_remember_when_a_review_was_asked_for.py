"""Remember when a review was asked for

A review request row carried two stamps, both on our clock: when the ping went out, and when a
review closed it. Neither says when the request itself was made, and two separate defects came
from having to guess at it.

The first: a `pull_request_review` delivery runs the ledger before its Discord post and again on
every retry, so a re-request made during that delivery's backoff was closed a second time, with
the review's own timestamp. The stamp then read as an answered request, and the next ordinary
event with a later timestamp reopened it and pinged for an ask nobody had made.

The second: telling a re-request from the same request arriving twice was done by comparing the
payload against the tracked item's high-water mark, which is a question about the item and not
about the row. GitHub fires several events for one action inside the same second, so an item
already brought up to that second swallowed a genuine re-request.

This column answers both, on GitHub's clock on both sides: when GitHub says the request this row
represents was made. Set when the row is written, and moved forward when the request is made
again.

Nullable, because rows written before this have no answer and nothing can invent one. Null is read
as "no evidence", which reopens rather than refuses: a pull request is short lived, so the window
in which any row still carries one is the deployment itself.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "item_assignments",
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("item_assignments", "requested_at")
