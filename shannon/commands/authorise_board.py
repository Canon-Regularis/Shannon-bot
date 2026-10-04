"""`/authorise_board`: let this bot reach a project board as you, and take it back.

Issue #170. Everything a board used to do went through one classic personal access token
belonging to one human account, shared by every server this bot is in. Nothing scoped it: the
credential supplier took the board's owner and threw it away. So a card moved from Discord
appeared on GitHub as the token's owner whoever had asked, and one leaked token was write access
to every project that account could see.

This is the command that replaces it. Each person authorises for themselves, and what they grant
is used for two things and nothing else: a board they link is read under their authorisation, and
a card THEY move is moved as them.

It goes through a different registered application from `/link` - a classic OAuth App rather than
the GitHub App - because GitHub publishes no App permission for a user-owned Projects v2 board,
and granting an installed App an organisation permission suspends its event delivery until an
admin accepts it. `OAuthClient` in the verification service carries the long version.

**No member argument, unlike `/link`.** That one takes one so it can refuse to issue a link for
anybody else; here there is no half that could, so the argument does not exist at all. The
authorisation URL is a bearer credential and whoever opens it is recorded as the person it was
issued for, so a parameter naming somebody else would be a way to collect their credential.

Ungated, like `/mentions`. Granting a credential of your own is yours to decide and a role gate
could only stop somebody volunteering one; withdrawing it is yours for the same reason, and anyone
can withdraw it on GitHub regardless, so a gate here would buy a false sense of control rather
than control. What the reply does instead is say what withdrawing costs the server.
"""

from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.discord_bot.responses import defer, done, owed, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import VerificationPurpose

NOT_CONFIGURED = (
    "This bot has no GitHub application registered for project boards, so there is nothing to "
    "authorise. An admin needs to set SHANNON_GITHUB_BOARD_CLIENT_ID, its client secret, and "
    "SHANNON_BOARD_CREDENTIAL_KEY."
)

# Said after the link rather than instead of it, because the thing worth knowing is what the grant
# is FOR. People are reasonably wary of an OAuth screen asking for project access, and "full
# control of projects" is what GitHub will show them.
ASKING = (
    "Open this link and sign in to GitHub.\n{url}\n\n"
    "-# GitHub will ask for access to your projects, which is what moving a card needs. It is "
    "used for two things: a board you link is read with it, and a card you move from Discord is "
    "moved as you rather than as somebody else. You can take it back at any time with "
    "/authorise_board withdraw."
)

WITHDRAWN = (
    "This bot has forgotten your authorisation. If it was reading a board for this server, that "
    "has stopped.\n\n"
    "-# Forgetting it here is not the same as revoking it on GitHub, and the honest version is "
    "that only you can do that: find this app under Settings, Applications, Authorized OAuth Apps "
    "and revoke it there too."
)

NOTHING_TO_WITHDRAW = (
    "This bot has no authorisation from you in this server, so there was nothing to forget."
)


class AuthorisesBoards(Protocol):
    """Handing out the one-time link, and whether this deployment can.

    Two members, the same shape `ProvesIdentity` has for `/link`. `can_authorise_a_board` is its
    own property rather than an argument to `configured`, because the two applications are
    registered separately and either can be missing on its own - a deployment with the App and no
    OAuth App must still be able to run `/link`.
    """

    @property
    def can_authorise_a_board(self) -> bool: ...

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str: ...


class ForgetsAuthorisations(Protocol):
    """Letting go of one person's authorisation in one server."""

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool: ...


def build_authorise_board_command(
    verification: AuthorisesBoards, authorisations: ForgetsAuthorisations
) -> SlashCommand:
    @app_commands.command(
        name="authorise_board",
        description="Let this bot read your GitHub project board, or take that back",
    )
    @app_commands.describe(withdraw="Forget the authorisation instead of granting one")
    @app_commands.guild_only()
    async def authorise_board(interaction: discord.Interaction, withdraw: bool = False) -> None:
        """Grant or withdraw, as one command rather than two.

        One command because the pair is one idea and a person looking for the second would have to
        know it existed. The parameter defaults to granting, which is what somebody who typed the
        name without reading the arguments meant.
        """
        if interaction.guild_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        if withdraw:
            # No deferral: one delete, and nothing that talks to GitHub.
            gone = await authorisations.forget(
                guild_id=interaction.guild_id, discord_user_id=interaction.user.id
            )
            await reply(interaction, done(WITHDRAWN) if gone else done(NOTHING_TO_WITHDRAW))
            return

        if not verification.can_authorise_a_board:
            await reply(interaction, refused(NOT_CONFIGURED))
            return

        await defer(interaction)
        # Issued for whoever ran it, never for an argument. The URL is a bearer credential; see
        # the module docstring.
        url = await verification.link_for(
            guild_id=interaction.guild_id,
            discord_user_id=interaction.user.id,
            purpose=VerificationPurpose.BOARD,
        )
        await reply(interaction, owed(ASKING.format(url=url)))

    # `app_commands.command()` leaves the command's binding type unknown, and
    # `discord_bot/slash.py` says why `Any` is the only truthful thing to put in.
    return authorise_board  # pyright: ignore[reportUnknownVariableType]
