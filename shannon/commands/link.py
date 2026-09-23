"""`/link`: connect your GitHub account, with GitHub deciding which one.

Issue #144. This and `/verify` were the same command with different amounts of evidence behind
them. `/link` recorded a login somebody typed and asked GitHub only whether it existed, never
whose it was; `/verify` asked, and wrote the answer. Two commands, one of which could be wrong
about who somebody is, and nothing but the second one to tell them apart afterwards.

So there is one, and it is the one that asks. Nobody types a login: the account written down is
whatever `GET /user` answered for whoever signed in, so a typo binds nothing and a stranger's name
cannot be bound by mistake. Following the link is the whole of it — the row is written by the
click, in `redeem`, where GitHub's answer is.

The member argument exists so that **no** link is issued for somebody else, which is the inverse
of what it used to do. The authorisation URL is a bearer credential: whoever opens it is recorded
as the person it was issued for, so handing one to anybody but that person hands them that
person's identity. Naming a member posts a note in the channel asking them to run this themselves
and issues nothing.

Gated on one of its two halves, which is unusual here and is the point. Connecting your own
account needs no role, because GitHub decides it and a gate would only stop somebody proving who
they are. Asking somebody else pings them in public, and a bot that will ping anybody on anybody's
say-so is a spam tool, so that half takes the tier that speaks for the server.
"""

from __future__ import annotations

import logging
from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.commands._permissions import REGISTER_ROLES
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, in_the_channel, owed, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import VerificationPurpose

logger = logging.getLogger(__name__)

NOT_CONFIGURED = (
    "This bot cannot check who you are on GitHub, so there is nothing to connect to. An admin "
    "needs to set the GitHub App's client secret and this deployment's public URL."
)

CANNOT_POST = (
    "This bot cannot post in this channel, so nobody was asked. Give it Send Messages here, or "
    "ask them yourself: they run /link and nobody else has to do anything."
)


class ProvesIdentity(Protocol):
    """Asking GitHub who somebody is, which is the whole of what this command needs.

    Two members, not three. There is no "have they proved it yet" question any more, because
    there is no second run to ask it on: the link finishes the job when it is followed.
    """

    @property
    def configured(self) -> bool: ...

    async def link_for(
        self, *, guild_id: int, discord_user_id: int, purpose: VerificationPurpose
    ) -> str: ...


def build_link_command(verification: ProvesIdentity, gate: PermissionGate) -> SlashCommand:
    """Gated for one of its two halves, and no try/except.

    It keeps a `gate` parameter, so `_permissions.UNGATED` stays a list of commands anybody may
    run whatever the arguments, and the tier this applies is held by the command's own tests
    rather than by that table.

    Nothing on this path raises a `ShannonError`: there is no login to doubt any more, which is
    what taking the argument away bought. A clause catching one would be a branch nothing can
    exercise under a coverage floor of a hundred per cent.
    """

    @app_commands.command(
        name="link", description="Connect your GitHub account to your Discord account"
    )
    @app_commands.describe(member="Ask somebody else to connect theirs, in this channel")
    @app_commands.guild_only()
    async def link(interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        # The guild check by hand rather than through `in_a_server`, which exists to apply a tier
        # and there is only a tier on one branch of this. `/mentions` does the same.
        guild_id = interaction.guild_id
        if guild_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        # Above the branch, because neither half works without it: a deployment that cannot run
        # the round trip cannot link anybody, so asking somebody to go and try is no kinder.
        if not verification.configured:
            await reply(interaction, refused(NOT_CONFIGURED))
            return

        # On identity rather than on presence. Naming yourself is a self-link, or somebody who
        # typed their own name would be refused for something they are allowed to do.
        if member is not None and member.id != interaction.user.id:
            await _ask_them(interaction, member, gate)
            return

        await defer(interaction)
        url = await verification.link_for(
            guild_id=guild_id,
            discord_user_id=interaction.user.id,
            purpose=VerificationPurpose.LINK,
        )
        await reply(
            interaction,
            owed(
                f"Open this link and sign in to GitHub.\n{url}"
            ),
        )

    # `app_commands.command()` leaves the command's binding type unknown; one line here rather
    # than a suppression over the whole file.
    return link  # pyright: ignore[reportUnknownVariableType]


async def _ask_them(
    interaction: discord.Interaction, member: discord.Member, gate: PermissionGate
) -> None:
    """Ask somebody else to connect their account, and issue nothing.

    The note is public because it is addressed to them, and they are not the one watching for a
    reply. It carries no link, which is the security property this command is built around: a
    link issued for somebody else is that person's identity in whoever's hands hold the URL.
    """
    if not gate.allows(interaction.user, REGISTER_ROLES):
        await reply(interaction, refused(gate.denial("link", REGISTER_ROLES)))
        return

    try:
        await in_the_channel(
            interaction,
            f"<@{member.id}> — this server mirrors GitHub, and connecting your account is how "
            "its messages reach you by name. Run /link here to do it: you will get a "
            "private link to sign in with, and clicking it is the whole of it.",
        )
    except discord.HTTPException as refusal:
        # An ephemeral reply always lands and a public one needs Send Messages, so this is the
        # one refusal this command has to answer for. Left to the tree's handler it would read
        # as "Something went wrong here", and the person owed an explanation would get none.
        logger.warning("could not ask %s to link in the channel: %s", member.id, refusal)
        await reply(interaction, refused(CANNOT_POST))
