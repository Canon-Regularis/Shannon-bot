"""Prove who is asking to unregister

Registering has been one way since it was written: one repository per server, one server per
repository, and no supported way to undo either. That was survivable while the only cost of a
mistake was a public repository mirrored into the wrong channel.

It is not survivable for private code. A repository bound to the wrong server has no escape short
of editing the database by hand, and the binding is what decides where private issue titles and
comment bodies are posted.

So `/unregister` exists. The question it forces is who may run it, and the honest answer is that a
Discord role cannot decide: the whole point of the command is that it undoes a binding, so gating
it on the same role that created the binding protects nobody from the case that matters, which is
somebody detaching a repository they have nothing to do with.

`/link` cannot help either, and it is worth saying why because it looks like it should. It records
a GitHub login against a Discord account after checking only that the login EXISTS. It is a claim
somebody makes about themselves, unverified by construction, so any guild administrator can link
themselves to the repository owner's login and pass any check built on it. A test asserting that
would pass today.

That leaves asking GitHub. The person runs `/unregister`, gets a one-time link, authorises the App,
and GitHub redirects back naming who they actually are. Then their permission on the repository is
read, and only `admin` unbinds.

Two tables, because the round trip has two halves.

`identity_verifications` is the outstanding link. The callback is unauthenticated - it is a browser
arriving with a code - so `state` is the entire thread back to the person who ran the command, and
it is therefore both the CSRF token and the session identifier, which is the ordinary OAuth pattern.
Single use, consumed by an UPDATE filtering on `consumed_at IS NULL` so that two clicks on one link
race in the database rather than in Python. Short lived, because an unused one left lying around is
a standing invitation to unbind somebody's repository.

`verified_identities` is the answer, kept briefly. Not re-proved on every command, because a proof
that costs a browser visit and is demanded twice in a minute is a safety check people route around
rather than use. How briefly is the service's business rather than the schema's, and it is short:
the row permits an irreversible command, and holding an account an hour ago says little about now.

Neither belongs on `user_links`, for exactly the reason `muted_members` does not.
`UserLinkStore.link` deletes and rewrites that row, so anything kept beside it is destroyed by
the one command the bot tells people to run by name. And they are a different kind of fact:
a link is a claim somebody made, and this is something GitHub vouched for.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_verifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=64), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
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
        # The state is looked up by value on every callback and must be unique, or "consume one
        # row" stops being a well-defined thing to ask for.
        sa.UniqueConstraint("state", name="uq_identity_verifications_state"),
    )
    # The pruner finds the slice past the expiry without reading the rest, the way the delivery
    # queue's own retention index does.
    op.create_index(
        "ix_identity_verifications_expires_at", "identity_verifications", ["expires_at"]
    )
    op.create_table(
        "verified_identities",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("discord_guild_id", sa.BigInteger(), nullable=False),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
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
        # One verified identity per person per server. Per server rather than globally, because
        # the rest of this schema is keyed that way: somebody proving who they are in one server
        # has said nothing to another, and `/unregister` acts on one server's binding.
        sa.UniqueConstraint(
            "discord_guild_id", "discord_user_id", name="uq_verified_identities_guild_discord"
        ),
    )


def downgrade() -> None:
    op.drop_table("verified_identities")
    op.drop_index("ix_identity_verifications_expires_at", table_name="identity_verifications")
    op.drop_table("identity_verifications")
