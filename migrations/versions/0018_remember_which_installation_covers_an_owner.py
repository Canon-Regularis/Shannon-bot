"""Remember which installation covers an owner

This bot has authenticated to GitHub with one personal access token since it was written, and the
README has always admitted what that costs: one token "has to see every repository". While every
repository was public that was untidy rather than dangerous, because the token could reach nothing
anybody else could not already read.

Private repositories change that completely. `/register` is open to anyone holding the Admin or
Project Manager role, and a guild Administrator bypasses the role check outright, so a token that
can see private code means any administrator of any server this bot has been invited to can mirror
any of it into a channel they control. The fix is not a bigger token. It is not to have one.

A GitHub App mints a token per installation, and an installation can only see what it was granted.
It is also the proof of control that a Discord role is not: installing an App requires admin on the
repository or the organisation, which is exactly the thing `/register` had no way to check.

This table is the map from a GitHub account to its installation. Keyed on the ACCOUNT, because an
installation is on an account rather than on a repository, and the route from a Discord server to a
token is guild, then repository, then owner, then here. Deliberately not keyed on the guild as well:
that would tie one account's installation to one server and break the moment two servers mirror two
repositories under the same owner, which the schema otherwise handles fine.

It is a fast path and not the source of truth. GitHub is authoritative, every App delivery carries
its installation id, and the resolver can ask GitHub directly, so a row that is missing or stale
costs one request rather than a broken mirror. That is deliberate: a cache nothing depends on is a
cache that cannot be wrong in a way anybody has to debug.

`account_id` is nullable for the reason `user_links.github_user_id` is nullable. A row written from
a payload that carried no account block has no evidence of the id, and false evidence is worse than
none. A login is not an identity - GitHub frees one the moment it is renamed - so the id is how a
rename is told apart from a stranger taking the name.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "github_installations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("account_login", sa.String(length=255), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=True),
        sa.Column("suspended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        # Both directions are one to one: one installation per account, one account per
        # installation. GitHub enforces the same thing, and saying so here means a duplicate
        # arriving from two deliveries at once settles in the database rather than in Python.
        sa.UniqueConstraint("installation_id", name="uq_github_installations_installation_id"),
        sa.UniqueConstraint("account_login", name="uq_github_installations_account_login"),
    )


def downgrade() -> None:
    op.drop_table("github_installations")
