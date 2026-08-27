"""Remember who a login belonged to

A GitHub login is not an identity. GitHub frees an account name the moment it is renamed or
deleted, redirects the old one so nothing appears to break, and lets anybody register it. Both
tables that turn a person into a Discord mention were keyed on that name alone.

So a contributor renames their account, nobody re-runs `/link` because nothing anywhere says the
link has gone stale, and months later somebody else takes the freed name. From then on every
mention built for that name resolves to the first person's Discord account: the review ping
notifies them for a review they were never asked for, the reviewers line of the metadata block
carries their mention, and a stranger's mirrored comments are headed by it. Meanwhile the person
who renamed is no longer resolved at all.

The stable identity was in hand the whole time and thrown away. Every payload carries the account's
numeric id, `mapping.actor` parses it into `Actor.github_user_id`, and nothing stored it. These two
columns store it, and the resolution asks it rather than the name.

Nullable on both, because rows written before this have no answer and nothing can invent one:
GitHub can say what a login is called now, not what it was called when somebody linked it. A null
id is read as no evidence and the name is used, which is exactly what happened before, so an
existing link keeps working. It stops being a guess the first time anybody re-runs `/link`.

`item_assignments` needs its own copy rather than reading through to `user_links`, because the two
answer different questions: the assignment row records who GitHub said was asked, whether or not
anybody has ever linked them, and the ping path resolves from that row long after the payload that
made it has gone.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_links", sa.Column("github_user_id", sa.BigInteger(), nullable=True))
    op.add_column("item_assignments", sa.Column("github_user_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("item_assignments", "github_user_id")
    op.drop_column("user_links", "github_user_id")
