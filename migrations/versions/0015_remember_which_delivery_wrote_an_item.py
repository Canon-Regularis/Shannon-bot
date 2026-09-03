"""Remember which delivery wrote an item

Two deliveries for one item routinely carry the same `updated_at`, because GitHub stamps it to the
second and an item opened with a reviewer already on it is two events milliseconds apart. The
staleness guard reads equal as current, deliberately, since several real changes share a second and
all of them happened. So neither delivery is turned away and whichever runs last is believed in
full, including its view of who is on the item.

That is fine while they run in the order they arrived, and the worker leases in arrival order. It
stops being fine the moment anything reorders them, which one transient failure does: a delivery
that backs off is skipped by the lease until its next attempt, so the one behind it goes first.

The timestamps cannot separate them, and nothing else on the item could either. But the deliveries
themselves are numbered: `webhook_events.id` is assigned when the delivery is written down, which
is the order they reached this bot. Recording which one last wrote the item gives the guard
something to compare when the clocks tie.

Nullable, because every row written before this was written by a delivery whose number was never
kept, and there is no way to work out which. A null on either side means the question cannot be
answered and the timestamps decide alone, which is exactly what happened before, so an existing
item keeps the behaviour it had and gains the guard from its next delivery onwards.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tracked_items", sa.Column("last_delivery_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracked_items", "last_delivery_id")
