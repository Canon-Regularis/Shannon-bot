"""Retire READY_FOR_MERGE, because the board's column order replaces it

The status existed to carry one rule: a pull request had to be `Ready for merge` before it could be
marked `Done`, so nobody could declare work finished before a reviewer said it could be merged. The
rule was right and the way it was written was not. It named a column, and GitHub's own default
board template does not have one - it ships Backlog, Ready, In progress, In review, Done - so on
the commonest board there is, `/status Ready for merge` could only ever refuse and the gate in
front of DONE could never be satisfied except by a status no column would accept.

The rule now comes from the board: a card may move forward one column at a time and back as far as
you like, measured against the order the board's own Status options come in. On that same template
`In review -> Done` is one step and `Ready -> Done` skips two, so the protection survives without
this codebase holding an opinion about what anybody's columns are called.

IN_REVIEW is the replacement, and it is a judgement rather than a default. An item that was
`Ready for merge` has been reviewed and has not been merged, which is what IN_REVIEW says; DONE
would claim work was finished that is not, and NOT_REVIEWED would throw away a review that
happened. Both of those are wrong in a way somebody would have to notice by hand.

This rewrite is not optional, and that is the reason this migration exists at all rather than the
enum simply losing a member. `tracked_items.status` is a non-native `sa.Enum`, so the database
holds a plain varchar with no CHECK constraint and will keep a value the enum no longer knows -
but SQLAlchemy validates on the way out and raises `LookupError` for the WHOLE query rather than
for the one row, as `db/base.py` says in as many words. One surviving row would therefore take
every read of `tracked_items` down: the poller, every command, every webhook.

The downgrade cannot undo it. Nothing records which IN_REVIEW rows were once READY_FOR_MERGE, and
inventing that from the current state would move work the wrong way for rows that were never the
old status. So down() is deliberately empty: going back leaves the rows where this put them, and
the old code reads IN_REVIEW perfectly well.

Revision ID: 0028
Revises: 0027
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out rather than imported from `shannon.domain.enums`. The member is gone from that enum
# by the time this runs, and a migration that imported it would stop importing - a migration
# describes the schema as it was at one moment, so it carries its own strings.
_RETIRED = "READY_FOR_MERGE"
_REPLACEMENT = "IN_REVIEW"


def upgrade() -> None:
    op.execute(
        sa.text("UPDATE tracked_items SET status = :to WHERE status = :frm").bindparams(
            to=_REPLACEMENT, frm=_RETIRED
        )
    )


def downgrade() -> None:
    """Nothing. See the note above: which rows to put back is not recorded anywhere."""
