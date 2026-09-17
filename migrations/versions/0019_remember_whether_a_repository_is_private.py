"""Remember whether a repository is private

Nothing in this project has ever recorded a repository's visibility, and nothing has ever asked.
That was reasonable while only public repositories could be registered: there was one answer and
it was the same everywhere.

With the GitHub App in place, both kinds arrive, and the difference matters to somebody running
this rather than to the code. "Is there private code in this database" is the first question asked
of a deployment holding a backup, and answering it otherwise means a GitHub call per repository
against a token that may no longer have access. It is also how a repository being flipped from
public to private becomes something the bot can notice rather than something it reads straight past.

Nullable, with no backfill, and read as no evidence rather than as public. Nothing can invent the
answer for a row written before this column existed, and defaulting to false would state something
nobody checked - the same mistake `0012` avoided by leaving `github_user_id` null rather than
guessing at it.

It needs no backfill anyway, which is the part worth knowing. The value is written from the
repository object on every sync, and every registered repository is synced on its next delivery, so
the column fills itself in during ordinary use and an operator who wants it sooner can run
`/refresh`.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repositories", sa.Column("private", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("repositories", "private")
