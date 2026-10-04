"""A board is read as whoever authorised it

Issue #170. Until now every project board in every server was read - and every card moved - through
one classic personal access token belonging to one human account, with the `project` scope, which
is read AND write across every project that account can see. Nothing scoped it per server: the
credential supplier accepted the board's owner and threw it away. A card moved from Discord
appeared on GitHub as the token's owner whoever asked for it, and one leaked token was write access
to every board.

This is the schema half of replacing that with an authorisation each person grants for themselves.

`board_authorizations` holds the first credential this project stores. Everything else it keeps
about somebody is a fact ABOUT them; this is a thing that ACTS as them, so the token column is
encrypted with a key that lives in the environment and never in the database. A stolen dump of
this table is worth nothing on its own.

Its own table rather than columns on `repositories`, on the reasoning `muted_members` already
sets out: rows that several commands rewrite will one day forget something kept beside them, and a
credential must never be that something. It also keeps the lifetimes apart, since a repository
outlives any authorisation granted for it.

Keyed on the Discord member, one row per person per server. Both readers arrive there: a card move
by the member running the command, a poll by the member named on the board's own row.

`repositories.project_linked_by` is that name. Nullable with no backfill, on the pattern `0019`
set for `private`, `0026` for the board, `0027` for the card and `0030` for the fields - and here
the null carries the migration's whole upgrade story. Null means nobody has authorised this board,
which reads as a board that cannot be read. Every row that exists when this runs is null, so no
board quietly carries on under the old shared credential: each one waits to be authorised by
somebody, which is the point of the issue. A deployment with a linked board therefore has to run
the command again, and that is deliberate rather than a wart.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-03

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board_authorizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        # Text rather than a width: a Fernet token's length tracks the plaintext it wraps, and
        # GitHub has lengthened its tokens before. A width here is a future migration for nothing.
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "discord_guild_id", "discord_user_id", name="uq_board_authorizations_guild_discord"
        ),
    )
    op.add_column("repositories", sa.Column("project_linked_by", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("repositories", "project_linked_by")
    op.drop_table("board_authorizations")
