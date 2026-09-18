"""Stop a backlog of assignees being pinged

Issue #105 gives a pull request's assignees a notifier, so that somebody put on one from Discord is
told about it. Nothing told them before: the block names them, and every block after the first is an
edit, which Discord does not notify.

Switching that on without this would be a mass notification. `PullRequestPolicy.assignments` has
written an `ASSIGNEE` row for every pull request since it was written, and until now nothing ever
claimed one, so every one of those rows has `notified_at` empty. `claim_notifications` asks nothing
else, so the next delivery on each open pull request would ping everybody already assigned to it,
about work they were assigned days or months ago.

So the rows that predate the notifier are stamped as already told, which is true: whatever was going
to tell them has already not happened.

Pull requests only. A null on an issue row is a ping genuinely still owed, because the issue
sync has carried this notifier all along and clears the stamp whenever a post fails.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Lifted out so a test can run the statement this migration actually runs, rather than a copy of
# it kept in step by hand. What it does is easy to get subtly wrong and impossible to see going
# wrong: stamping one row too many silences a ping somebody was owed, and one too few is the storm.
STAMP = sa.text(
    """
    UPDATE item_assignments SET notified_at = now()
    WHERE role_type = 'ASSIGNEE'
      AND notified_at IS NULL
      AND tracked_item_id IN (
          SELECT id FROM tracked_items WHERE github_object_type = 'PR'
      )
    """
)


def upgrade() -> None:
    op.execute(STAMP)


def downgrade() -> None:
    """Deliberately nothing.

    Clearing the stamps back would not restore what was there before: it cannot tell a row this
    migration stamped from one the notifier has legitimately claimed since, so it would empty both
    and cause the exact ping storm the upgrade exists to prevent. A downgrade that re-creates a
    bug is not reversibility, and leaving people un-pinged for work they were already assigned
    costs nothing.
    """
