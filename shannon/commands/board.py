"""`/board`: which GitHub project board this server mirrors, and whose authorisation reaches it.

Issue #201. This was two commands doing one job. A board is read under the authorisation of whoever
linked it (issue #170), so linking one for the first time took both, in order, with a trip to GitHub
in the middle: `/set_board` refused, `/authorise_board` handed out a link, the browser came back,
and `/set_board` was run again. Undoing it was split the same way, and the two places that said how
disagreed with each other.

Now it is one command, and linking a board is one click. `/board link` hands out one link - to this
bot, then Discord, then GitHub - that REMEMBERS the board that was chosen, and following it both
authorises and links: the shape `/link` already has, where clicking the link is the whole of it.

**Who may run which half.** Linking and unlinking decide something for the server, so they take the
tier that speaks for it, `REGISTER_ROLES`. Authorising and asking take `BOARD_ROLES`, the tiers
whose authorisation is ever actually used. Withdrawing takes no tier at all, deliberately: it
deletes a credential that belongs to whoever runs it, and that must not depend on a role they may
since have lost. A factory gated on some of its halves cannot be described by
`_permissions.UNGATED`, so - as with `/link` - it keeps its `gate`, and this module's own tests
hold the tiers.

**No member argument anywhere.** Every link this hands out is issued for whoever ran it and never
for somebody named. A link used to be a bearer credential, recorded as whoever it was issued for by
whoever opened it, so a parameter naming somebody else would have been a way to collect their
credential. Since #201's review Discord has to name that member before GitHub is asked anything,
and the rule stands regardless: a credential is granted by the person it belongs to, from their
own command.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Coroutine, Sequence
from typing import Any, Protocol

import discord
from discord import app_commands

from shannon.commands._guards import NOT_IN_A_SERVER, in_a_server
from shannon.commands._permissions import BOARD_ROLES, REGISTER_ROLES
from shannon.commands._replies import words_for
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import defer, done, owed, refused, reply
from shannon.discord_bot.roles import CommandRole
from shannon.discord_bot.slash import SlashGroup
from shannon.domain.board import ChosenBoard
from shannon.domain.enums import VerificationPurpose
from shannon.domain.errors import BoardNotAuthorisedError, NotRegisteredError, ShannonError
from shannon.domain.text import code_span
from shannon.github.projects import ProjectListing
from shannon.services.boards import (
    BoardLink,
    BoardStanding,
    BoardUnlinked,
    BoardUnreadableError,
    said,
)

logger = logging.getLogger(__name__)

# Discord's caps: twenty-five suggestions, and a hundred characters for each one's name. It sends a
# longer list, or a longer name, back as an error rather than cutting it - and an autocomplete that
# errors shows nothing at all, so one long board title emptied the whole picker.
MOST_CHOICES = 25
MOST_LABEL = 100

# The largest board number there is room for. GitHub's numbers are small and the columns that hold
# one are 32-bit integers, so a number past this used to reach Postgres - which refused it with an
# error the person saw as "Something went wrong here".
LARGEST_BOARD = 2**31 - 1

# A GitHub login: letters, digits and hyphens, at most thirty-nine, never starting with a hyphen -
# and an underscore, which an Enterprise Managed User's login carries before its short code.
# Checked rather than trusted because it is stored and it becomes part of a request path; whether
# the account exists is GitHub's to say. ASCII only, because `[A-Za-z0-9]` is what GitHub allows and
# Python's own idea of a letter is far wider.
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", re.ASCII)

# A board's own page, both kinds of owner, with whatever GitHub has appended to it - a view, a
# query, a trailing slash. Anchored at both ends so a sentence that merely CONTAINS one is still
# refused rather than half-read. Digits are `[0-9]` rather than `\d`, which in Python matches
# every script's digits - and `int()` then refuses some of them, so "²" got past the parser and
# crashed it.
#
# The view is a path segment, not a query: a board opens on `/projects/6/views/1`, so that is what
# the address bar holds when somebody copies it - and it was refused as "not a board" all along,
# under a comment saying views were accepted.
_BOARD_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/(?:users|orgs)/"
    r"(?P<owner>[^/?#]+)/projects/(?P<number>[0-9]+)(?:/views/[0-9]+)?/?(?:[?#].*)?$",
    re.IGNORECASE,
)

# The picker's own entries, arriving as text. `\b` rather than anything looser, so "#6abc" is still
# refused: the boundary is what keeps this to the shape `_label_for` writes instead of reading a
# number out of any prose that happens to open with a hash.
_BOARD_LABEL = re.compile(r"^#(?P<number>[0-9]+)\b", re.ASCII)

NOT_CONFIGURED = (
    "This bot is not set up to authorise project boards yet, so there is nothing to sign in to. "
    "An admin needs to register the board OAuth App and set SHANNON_GITHUB_BOARD_CLIENT_ID, its "
    "client secret, SHANNON_BOARD_CREDENTIAL_KEY, SHANNON_DISCORD_CLIENT_ID, "
    "SHANNON_DISCORD_CLIENT_SECRET and SHANNON_PUBLIC_BASE_URL."
)

# Said after the link rather than instead of it, because the thing worth knowing is what the grant
# is FOR. People are reasonably wary of an OAuth screen asking for project access, and "full control
# of projects" is what GitHub will show them. Two versions, because only the person who LINKS a
# board has it read with their authorisation; somebody authorising on their own is only having
# their own card moves made as them.
#
# The organisation line is there because an organisation's board is the one place a correct
# sign-in still fails: organisations restrict OAuth apps by default. GitHub offers an owner the
# Grant button only on a first sign-in, so the line also says where anybody can ask afterwards.
ORGANISATIONS = (
    "If the board belongs to an organisation, the organisation has to approve this app: GitHub "
    "shows its owners a Grant button the first time they sign in, and anybody can ask for it "
    "under Applications in their own GitHub settings. /board withdraw takes it back."
)

LINKING_IS_FOR = (
    "-# GitHub will ask for access to your projects, which reading a board and moving its cards "
    "need: the board is read with it, and a card you move from Discord moves as you rather than "
    "as somebody else. " + ORGANISATIONS
)

AUTHORISING_IS_FOR = (
    "-# GitHub will ask for access to your projects, which moving a card needs: a card you move "
    "from Discord then moves as you rather than as somebody else. " + ORGANISATIONS
)

# In a browser, because the link goes to Discord before GitHub: Discord says which member is
# holding the browser, and only the one who ran the command gets any further. Found reviewing #201.
SIGN_IN_TO_LINK = (
    "Open this link in your browser and sign in to GitHub, and {board} is linked when you do. "
    "Discord checks that it is you first. There is nothing else to run.\n{url}\n\n" + LINKING_IS_FOR
)

SIGN_IN = (
    "Open this link in your browser and sign in to GitHub. Discord checks that it is you first."
    "\n{url}\n\n" + AUTHORISING_IS_FOR
)

# Where the board would not open with the authorisation they already have. The reason comes first,
# because a wrong number is the likelier cause and signing in again will not fix that one. Nor
# will it fix an organisation that has not approved this app: GitHub completes a second sign-in
# without showing anything, so that one is said separately, with where approval is asked for.
WILL_NOT_OPEN = (
    "{reason}\n\nIf the number and owner are right and you revoked this app on GitHub, open this "
    "link in your browser and sign in again - Discord checks that it is you first - and it is "
    "linked when you do.\n{url}\n\n-# If the board belongs to an organisation, "
    "the organisation has to approve this app first - an owner can, under its Settings, "
    "Third-party access - and then /board link works as it is."
)

NOT_A_BOARD = (
    "{board} is not a board. Pick one from the list, paste the board's URL, or type its number - "
    "the one after /projects/ in that URL."
)

NOT_AN_ACCOUNT = (
    "{owner} is not a GitHub account name. Leave the owner out where the board belongs to this "
    "repository's own owner, or paste the board's URL, which carries it."
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

Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]


class LinksBoards(Protocol):
    """Pointing this server's repository at a board, letting it go, and saying which it is."""

    async def assign(
        self, *, guild_id: int, project_number: int, typed_owner: str, acting: int
    ) -> BoardLink: ...

    async def unassign(self, *, guild_id: int) -> BoardUnlinked: ...

    async def standing(self, *, guild_id: int, asking: int) -> BoardStanding: ...

    async def choices_for(
        self, guild_id: int, typed_owner: str, *, acting: int
    ) -> Sequence[ProjectListing]: ...


class AuthorisesBoards(Protocol):
    """Handing out the one-time link, and whether this deployment can.

    Two members, the same shape `ProvesIdentity` has for `/link`. `can_authorise_a_board` is its
    own property rather than an argument to `configured`, because the two applications are
    registered separately and either can be missing on its own - a deployment with the App and no
    OAuth App must still be able to run `/link`.

    `tier` is required, and is the set this command gated the half on. Found reviewing #201: the
    link is followed up to ten minutes later, and following it asks Discord again for that tier,
    so a half that forgot to say which would be one whose links nobody but an administrator
    could finish.
    """

    @property
    def can_authorise_a_board(self) -> bool: ...

    async def link_for(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        purpose: VerificationPurpose,
        board: ChosenBoard | None = None,
        tier: Collection[CommandRole],
    ) -> str: ...


class ForgetsAuthorisations(Protocol):
    """Letting go of one person's authorisation in one server."""

    async def forget(self, *, guild_id: int, discord_user_id: int) -> bool: ...


def build_board_command(
    boards: LinksBoards,
    verification: AuthorisesBoards,
    authorisations: ForgetsAuthorisations,
    gate: PermissionGate,
) -> SlashGroup:
    """`/board` and its five halves.

    Guild-only through the decorator on the GROUP, because Discord reads it from the top-level
    command alone and ignores it on a subcommand - and through the decorator rather than the
    constructor argument, which sends a context list of null where every other command here sends
    the guild.
    """
    group = app_commands.guild_only(
        app_commands.Group(name="board", description="The GitHub project board this server mirrors")
    )

    @group.command(name="link", description="Mirror a GitHub project board in this server")
    @app_commands.describe(
        board="The board to mirror: pick one, paste its URL, or type its number",
        owner="Only if the board is not owned by this repository's own owner",
    )
    async def link(
        interaction: discord.Interaction,
        board: str,
        owner: app_commands.Range[str, None, 39] = "",
    ) -> None:
        guild_id = await in_a_server(interaction, "board link", gate, REGISTER_ROLES)
        if guild_id is None:
            return

        chosen = _wanted(board)
        if chosen is None:
            # Cut before it is quoted, so the closing fence and the sentence after it survive:
            # Discord allows six thousand characters here and a reply two thousand, so an uncut
            # paste was quoted back whole and the part that said what to do instead never arrived.
            shown = code_span(_clipped(board.strip()))
            await reply(interaction, refused(NOT_A_BOARD.format(board=shown)))
            return
        typed = owner.strip()
        if typed and _LOGIN.fullmatch(typed) is None:
            named = code_span(_clipped(typed))
            await reply(interaction, refused(NOT_AN_ACCOUNT.format(owner=named)))
            return
        # What was typed beats what was pasted. Somebody who filled in both meant the one they
        # typed, and silently preferring the URL would be this command overruling them about the
        # half of the address people get wrong.
        wanted = ChosenBoard(number=chosen.number, owner=typed or chosen.owner)

        await defer(interaction)
        try:
            linked = await boards.assign(
                guild_id=guild_id,
                project_number=wanted.number,
                typed_owner=wanted.owner,
                # Whoever ran it, and not a parameter anybody can name. Their GitHub authorisation
                # is what this board is read under from here on, so linking one on somebody else's
                # behalf would put a server back on one person's credential.
                acting=interaction.user.id,
            )
        except BoardNotAuthorisedError:
            await _sign_in_to_link(interaction, verification, guild_id, wanted, reason="")
            return
        except BoardUnreadableError as unopenable:
            await _sign_in_to_link(
                interaction, verification, guild_id, wanted, reason=unopenable.message
            )
            return
        except NotRegisteredError as error:
            await reply(interaction, refused(words_for(error, noun="repository")))
            return
        except ShannonError as error:
            await reply(interaction, refused(error.message))
            return

        await reply(interaction, done(said(linked)))

    link.autocomplete("board")(_suggesting(boards))

    @group.command(name="unlink", description="Stop mirroring this server's board")
    async def unlink(interaction: discord.Interaction) -> None:
        guild_id = await in_a_server(interaction, "board unlink", gate, REGISTER_ROLES)
        if guild_id is None:
            return

        # Deferred although nothing here talks to GitHub: unlinking forgets every card pairing
        # in the repository in one bulk update, then deletes a credential in a second
        # transaction, and a large repository or a lock wait can outrun Discord's three seconds.
        await defer(interaction)
        try:
            unlinked = await boards.unassign(guild_id=guild_id)
        except NotRegisteredError as error:
            await reply(interaction, refused(words_for(error, noun="repository")))
            return

        await reply(interaction, done(_unlinked(unlinked)))

    @group.command(
        name="authorise", description="Let the cards you move from Discord move as you on GitHub"
    )
    async def authorise(interaction: discord.Interaction) -> None:
        guild_id = await in_a_server(interaction, "board authorise", gate, BOARD_ROLES)
        if guild_id is None:
            return

        if not verification.can_authorise_a_board:
            await reply(interaction, refused(NOT_CONFIGURED))
            return

        await defer(interaction)
        # Issued for whoever ran it, never for an argument; see the module docstring.
        url = await verification.link_for(
            guild_id=guild_id,
            discord_user_id=interaction.user.id,
            purpose=VerificationPurpose.BOARD,
            tier=BOARD_ROLES,
        )
        await reply(interaction, owed(SIGN_IN.format(url=url)))

    @group.command(
        name="withdraw", description="Take back the GitHub authorisation you gave this bot"
    )
    async def withdraw(interaction: discord.Interaction) -> None:
        # The guild check by hand rather than through `in_a_server`, which exists to apply a tier,
        # and this half has none: see the module docstring.
        if interaction.guild_id is None:
            await reply(interaction, refused(NOT_IN_A_SERVER))
            return

        # No deferral: one delete, and nothing that talks to GitHub.
        gone = await authorisations.forget(
            guild_id=interaction.guild_id, discord_user_id=interaction.user.id
        )
        await reply(interaction, done(WITHDRAWN if gone else NOTHING_TO_WITHDRAW))

    @group.command(
        name="show", description="Which board this server mirrors, and whose access reads it"
    )
    async def show(interaction: discord.Interaction) -> None:
        guild_id = await in_a_server(interaction, "board show", gate, BOARD_ROLES)
        if guild_id is None:
            return

        await defer(interaction)
        try:
            standing = await boards.standing(guild_id=guild_id, asking=interaction.user.id)
        except NotRegisteredError as error:
            await reply(interaction, refused(words_for(error, noun="repository")))
            return
        except ShannonError as error:
            await reply(interaction, refused(error.message))
            return

        # Owed where the board is linked and is not being read, because that is something
        # somebody has to do; done otherwise, including a server with no board at all.
        healthy = standing.number is None or standing.title is not None
        await reply(interaction, done(_shown(standing)) if healthy else owed(_shown(standing)))

    return group


async def _sign_in_to_link(
    interaction: discord.Interaction,
    verification: AuthorisesBoards,
    guild_id: int,
    wanted: ChosenBoard,
    *,
    reason: str,
) -> None:
    """Hand out the one link that authorises and links at once.

    Two ways here. With no authorisation at all, and the reason is empty: this is the ordinary
    first time, and the link is the whole answer. With one that would not open the board, and the
    reason is why: a wrong number is the likelier cause, so it is said first, and the link is
    there for the other causes - a grant revoked on GitHub, or an organisation that has not approved
    this app, both of which signing in again is the fix for.

    The board rides on the pending row and never in the URL, so the link carries nothing anybody
    could edit on the way to GitHub and back.
    """
    if not verification.can_authorise_a_board:
        # Nobody can be sent anywhere. Where the board would not open, the reason is still the
        # answer worth giving; where nobody had authorised, there is nothing to authorise with.
        await reply(interaction, refused(reason or NOT_CONFIGURED))
        return

    url = await verification.link_for(
        guild_id=guild_id,
        discord_user_id=interaction.user.id,
        purpose=VerificationPurpose.BOARD,
        board=wanted,
        # The tier `/board link` is gated on, which is the only half that comes through here.
        tier=REGISTER_ROLES,
    )
    message = (
        WILL_NOT_OPEN.format(reason=reason, url=url)
        if reason
        else SIGN_IN_TO_LINK.format(board=_named(wanted), url=url)
    )
    await reply(interaction, owed(message))


def _named(wanted: ChosenBoard) -> str:
    """A board as a sentence names it, with its owner where one was given."""
    return f"{wanted.owner}'s board #{wanted.number}" if wanted.owner else f"board #{wanted.number}"


def _wanted(board: str) -> ChosenBoard | None:
    """What somebody chose: a number, and any owner they pasted, or None for "that is not a board".

    Parsed rather than trusted, because discord.py documents a choice as a suggestion - what
    arrives here may have been typed, and typed prose must be turned away with a sentence.

    A whole URL is accepted because it is the obvious thing to paste: it is what GitHub puts in the
    address bar. And a URL carries the OWNER, which is the other half of addressing a board and the
    half people get wrong. `/users/` and `/orgs/` are the same two prefixes the board reader splits
    on, for the same reason: a login names one account of one kind.

    The picker's OWN entries are accepted too. Discord sends a choice's value when the entry is
    committed and the raw text when it is typed or a highlighted suggestion is let fall through, and
    those are different strings: the value is the bare number, the label is "#6 Shannon Bot".

    Two outcomes rather than three, since issue #201. The picker used to offer "None - stop
    mirroring a board" and this read it as zero, which made a typo one character from unlinking
    somebody's board. Unlinking is its own command now, so nothing here means "none".
    """
    text = board.strip()
    label = _BOARD_LABEL.match(text)
    pasted = _BOARD_URL.match(text)
    if text.isascii() and text.isdigit():
        return _board(text, owner="")
    if label is not None:
        return _board(label.group("number"), owner="")
    if pasted is not None:
        return _board(pasted.group("number"), owner=pasted.group("owner"))
    return None


def _board(number: str, *, owner: str) -> ChosenBoard | None:
    """A board out of the text for its number and owner, or None where either is not one.

    The number's length is checked before it is converted: Python refuses to turn a string of more
    than a few thousand digits into an integer at all, and a board number is at most ten.
    """
    if len(number) > len(str(LARGEST_BOARD)) or not 0 < int(number) <= LARGEST_BOARD:
        return None
    if owner and _LOGIN.fullmatch(owner) is None:
        return None
    return ChosenBoard(number=int(number), owner=owner)


def _suggesting(boards: LinksBoards) -> Suggesting:
    """The picker, which must answer quickly and must never raise.

    Discord allows an autocomplete about three seconds and shows nothing at all when the callback
    fails, so a GitHub outage would be indistinguishable from an owner with no boards. Not gated: an
    autocomplete has nowhere to put a refusal, and what it lists is listed under the asker's OWN
    authorisation, so it can show them nothing their account cannot already see.
    """

    async def suggest(
        interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []

        # Whatever has been typed into the other field so far. `namespace` carries only the
        # options Discord has actually sent, so this is absent as readily as it is empty.
        typed_owner = getattr(interaction.namespace, "owner", "") or ""
        try:
            listed = await boards.choices_for(
                interaction.guild_id, typed_owner, acting=interaction.user.id
            )
        except Exception:
            logger.warning("could not suggest boards for guild %s", interaction.guild_id)
            return []

        wanted = current.strip().casefold()
        return [
            app_commands.Choice(name=_label_for(one), value=str(one.number))
            for one in listed
            if wanted in one.title.casefold() or wanted in str(one.number)
        ][:MOST_CHOICES]

    return suggest


def _label_for(one: ProjectListing) -> str:
    """How a board is named in the picker, cut to what Discord will take.

    One function rather than a formatted string at the call site, because `_wanted` reads this
    shape back: a client that submits the label as text instead of committing the choice hands it
    over verbatim. Cut at the end, never the start, so the "#6" that `_wanted` reads survives.
    """
    return _clipped(f"#{one.number} {one.title}")


def _clipped(text: str) -> str:
    """Text cut to what one Discord choice name will take, with the cut marked."""
    return text if len(text) <= MOST_LABEL else f"{text[: MOST_LABEL - 1]}…"


def _unlinked(unlinked: BoardUnlinked) -> str:
    """What unlinking changed, naming whose authorisation went with it.

    Named because they are the only person who can also revoke it on GitHub, and the person who
    ran this may well not be them. By their Discord account, which the server already knows - never
    by the GitHub login it was granted as, which is theirs.
    """
    if unlinked.replaced is None:
        return f"{unlinked.repo_name} was not mirroring a board, and still is not."
    forgot = (
        f"\n\n-# <@{unlinked.forgot}>'s GitHub authorisation was forgotten with it. Forgetting it "
        "here is not revoking it: only they can do that, under Settings, Applications, Authorized "
        "OAuth Apps on GitHub."
        if unlinked.forgot is not None
        else ""
    )
    return (
        f"{unlinked.repo_name} has stopped mirroring board #{unlinked.replaced}. Its threads are "
        f"left where they are.{forgot}"
    )


def _shown(standing: BoardStanding) -> str:
    """What `/board show` says: the board, whose authorisation reads it, and the asker's own.

    The linker by their Discord account, never their GitHub login, for the reason `_unlinked`
    gives. The asker's own login is shown to the asker, whose it is - and the reply is private.
    """
    yours = (
        f"You have authorised as {standing.yours}, so a card you move from Discord moves as you."
        if standing.yours is not None
        else "You have not authorised, so a card you move from Discord will not move on GitHub: "
        "/board authorise does that."
    )
    if standing.number is None:
        return f"{standing.repo_name} mirrors no board. /board link chooses one.\n\n-# {yours}"

    board = f"{standing.owner}'s board #{standing.number}"
    if standing.shared:
        state = (
            f"{standing.repo_name} mirrors {board}, and so does another server, so neither is "
            "being read: a board belongs to one server here. Whichever should not have it can "
            "run /board unlink there."
        )
    elif standing.linked_by is None:
        state = (
            f"{standing.repo_name} mirrors {board}, but nobody's authorisation stands behind it, "
            "so it is not being read. Linking it again with /board link fixes that."
        )
    elif not standing.held:
        state = (
            f"{standing.repo_name} mirrors {board}, linked by <@{standing.linked_by}>, whose "
            "authorisation is gone, so it is not being read. They can sign in again with "
            "/board authorise, or anybody who links boards can link it again with /board link."
        )
    elif standing.title is None:
        state = (
            f"{standing.repo_name} mirrors {board}, linked by <@{standing.linked_by}>, but it does "
            "not open with their authorisation: it was revoked on GitHub, or no longer reaches the "
            "board. Linking it again with /board link fixes that."
        )
    else:
        state = (
            f"{standing.repo_name} mirrors {board}, {standing.title}, read with "
            f"<@{standing.linked_by}>'s GitHub authorisation."
        )
    return f"{state}\n\n-# {yours}"
