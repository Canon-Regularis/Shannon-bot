"""`/linked`: whether you are linked to a GitHub account in this server, and what stands behind it.

Issue #232. People forget whether they ever linked, and finding out used to mean running `/link`
again and going through the whole round trip to learn the answer. This reads back what this
server holds about whoever runs it - which account, since when, whether GitHub vouched that it is
theirs, and which repository the server mirrors - and asks GitHub nothing.

"Linked" has states that look alike from outside, and the useful thing is to tell them apart. A
link nothing proves still pings, but `/assign` notes it, and a deployment requiring proved links
refuses it. A link by name alone, from before this bot asked GitHub whose a login was, never pings
at all. A name GitHub has handed to another account since leaves the link pointing at an account
nobody can sign in as. And a sign-in for `/register`, `/unregister` or `/board` proves an account
without linking it, which is the likeliest way to believe you are linked when you are not. Each is
said, with what to do about it: run `/link` where this deployment can, and where it cannot, that
an admin has to finish setting it up first, rather than sending somebody to a command that will
only refuse them.

Everything here is stored, so a GitHub rename since the link was made is not seen. The answer
says what it can know and no more: whom the link points at, never whether GitHub still calls them
that.

Ungated for `/mentions`' reason: it is about whoever runs it and decides nothing about the server.
No member argument, because another member's link names their GitHub account, which is theirs.
"""

from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER
from shannon.discord_bot.formatting import as_date
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.responses import defer, refused, reply
from shannon.discord_bot.slash import SlashCommand
from shannon.services.linking import LinkStanding


class RemembersWhoIsLinked(Protocol):
    """What this server holds about one member's GitHub account, read back for that member."""

    async def standing(self, *, guild_id: int, discord_user_id: int) -> LinkStanding: ...


class OffersLinks(Protocol):
    """Whether this deployment can hand out the link `/link` would, which is what to advise."""

    @property
    def can_prove_identity(self) -> bool: ...


_CANNOT_LINK = (
    "This bot cannot link anybody until an admin finishes setting up sign-in with GitHub and "
    "Discord."
)


def build_linked_command(service: RemembersWhoIsLinked, linking: OffersLinks) -> SlashCommand:
    """Ungated, as `/mentions` is and for the same reason, which `_permissions.UNGATED` records.

    `linking` is read when the command runs, never when it is built: whether sign-in is set up is
    a fact about the deployment that the answer has to follow, and a factory touching its
    arguments could not be built from the stand-ins `test_permission_tables` builds it with.

    No try/except: the service raises no `ShannonError`, the tree's own handler already answers
    an unexpected error, and a clause only a fake could reach is a branch nothing exercises under
    a coverage floor of a hundred per cent.
    """

    @app_commands.command(
        name="linked",
        description="Whether you are linked to a GitHub account in this server, and which one",
    )
    @app_commands.guild_only()
    async def linked(interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        if guild_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        await defer(interaction)
        standing = await service.standing(guild_id=guild_id, discord_user_id=interaction.user.id)
        # An answer to a question carries no mark, as `/mentions` reading its setting carries
        # none: a tick would claim that something changed, and nothing did.
        await reply(interaction, Panel.of_text(_said(standing, linking.can_prove_identity)))

    # `app_commands.command()` leaves the command's binding type unknown; one line here rather
    # than a suppression over the whole file.
    return linked  # pyright: ignore[reportUnknownVariableType]


def _said(standing: LinkStanding, can_link: bool) -> str:
    """Where they stand and what to do about it, then the repository as small print beneath."""
    advice = _advice(standing) if can_link else (None if standing.proved else _CANNOT_LINK)
    answer = _where(standing) if advice is None else f"{_where(standing)} {advice}"
    return f"{answer}\n\n-# {_mirrored(standing)}"


def _name_changed_hands(standing: LinkStanding) -> bool:
    """Whether they last signed in as an account GitHub now calls by the linked login.

    A name GitHub gave up when its account was renamed or deleted, then handed on: the link points
    at the account it was linked to, and the account answering to the name is somebody else's -
    possibly the member's own, re-created. Compared ignoring case, because a link is stored
    lowercased and a proof keeps GitHub's own.
    """
    link, proof = standing.link, standing.proof
    return (
        link is not None
        and link.account.github_user_id is not None
        and proof is not None
        and not standing.proved
        and proof.login.lower() == link.account.login
    )


def _where(standing: LinkStanding) -> str:
    """Whether they are linked, to which login and since when, and whatever stands in the way."""
    link, proof = standing.link, standing.proof
    if link is None:
        if proof is None:
            return "You are not linked to a GitHub account in this server."
        return (
            "You are not linked to a GitHub account in this server, although you last signed in "
            f"to GitHub from here as {proof.login}."
        )

    linked_to = f"You have been linked to {link.account.login} on GitHub"
    day = as_date(link.linked_at)
    if link.account.github_user_id is None:
        return (
            f"{linked_to} by name alone since {day}, from before this bot asked GitHub whose "
            "login it was."
        )
    if standing.proved:
        return f"{linked_to} since {day}, and GitHub has vouched that it is yours."
    if proof is None:
        return (
            f"{linked_to} since {day}, but nothing proves it is yours: it was linked without "
            "signing in to GitHub."
        )
    if _name_changed_hands(standing):
        return (
            f"{linked_to} since {day}, but the {proof.login} you last signed in to GitHub from "
            "here as is a different account: GitHub hands a name on once the account holding it "
            "is renamed or deleted."
        )
    return (
        f"{linked_to} since {day}, but you last signed in to GitHub from here as {proof.login}, "
        f"so nothing proves {link.account.login} is yours."
    )


def _advice(standing: LinkStanding) -> str:
    """What to run about it, on a deployment that can run `/link`."""
    link, proof = standing.link, standing.proof
    if link is None:
        if proof is None:
            return (
                "Run /link to connect yours: you will get a private link to sign in with, and "
                "clicking it is the whole of it."
            )
        return "Only /link links an account, so run it to link that one."

    login = link.account.login
    if link.account.github_user_id is None:
        return "Run /link to settle it."
    if standing.proved:
        # The one thing a stored answer cannot see, said where it matters: a rename leaves the
        # link on a name GitHub no longer uses for them, and their threads stop finding them.
        return "If you have renamed your GitHub account since, run /link so this server knows."
    if proof is None:
        return f"Run /link and sign in as {login} to prove it."
    if _name_changed_hands(standing):
        return "Run /link to link the account you hold now."
    return (
        f"Run /link and sign in as {login} to prove it, or as {proof.login} to link that account "
        "instead."
    )


def _mirrored(standing: LinkStanding) -> str:
    """The repository the link is used for, and whether its threads can mention them through it.

    Said only as far as it always holds. A link with an account id resolves into mentions whether
    or not anything proves it, under the login it was stored with. A link by name alone never
    does, a link whose name now belongs to another account matches nothing GitHub sends, and with
    no link at all nothing here leads to them.
    """
    repo = standing.repo_name
    if repo is None:
        return "This server mirrors no repository yet."
    link = standing.link
    if link is None:
        return (
            f"This server mirrors {repo}, but nothing here links you to a GitHub account, so its "
            "threads cannot mention you."
        )
    if link.account.github_user_id is None or _name_changed_hands(standing):
        return f"This server mirrors {repo}, but its threads cannot mention you through this link."
    return (
        f"This server mirrors {repo}, and its threads mention you where GitHub names "
        f"{link.account.login}."
    )
