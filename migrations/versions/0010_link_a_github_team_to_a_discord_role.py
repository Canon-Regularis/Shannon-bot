"""Link a GitHub team to a Discord role

A review can be asked of a team rather than a person, and until now that request was recorded and
shown and nobody was told about it. There was nowhere to look up who a team is on Discord's side:
`user_links` binds a GitHub login to an account, and a team has no login to bind.

Its own table rather than a column on `user_links`, because the two are different kinds of thing.
A login belongs to one person and a slug belongs to a group, Discord mentions them with different
syntax, and one column holding a user id on some rows and a role id on others is the shape that
makes every query ask which sort of row it has.

One uniqueness rule where `user_links` has two. A team maps to one role, but two teams pointing at
one role is reasonable: a server may have a single reviewers role that several teams should reach.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-20

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "team_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("github_team", sa.String(length=255), nullable=False),
        sa.Column("discord_role_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_guild_id", "github_team", name="uq_team_links_guild_team"),
    )


def downgrade() -> None:
    op.drop_table("team_links")
