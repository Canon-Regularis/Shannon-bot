from __future__ import annotations

import logging
import math

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
)
from shannon.discord_bot.panels import Accent, Block, BlockKind, Panel
from shannon.domain.errors import (
    DuplicateRegistrationError,
    ItemNotReadyError,
    NotInstalledError,
    NotProvenError,
    NotRegisteredError,
    RepositoryMismatchError,
    ShannonError,
    UnparseableLinkError,
)
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
)
from shannon.services.linking import InvalidGitHubTeamError, InvalidGitHubUsernameError
from shannon.services.sync.manual import SyncFailedError
from shannon.services.transcripts.log import (
    AlreadyLoggingError,
    CannotLogError,
    NotLoggingError,
)
from shannon.services.workflow import ItemMovedError, NotAnItemThreadError, WorkflowRefusedError

logger = logging.getLogger(__name__)

# What the person who ran the command is told, by error type. Ordered most specific first: the
# first match wins and several of these share a base class. One table for every command, so
# nothing escapes after the interaction has been deferred and leaves the caller with silence.
_REPLIES: tuple[tuple[type[ShannonError], str], ...] = (
    (UnparseableLinkError, "That link did not work. {message}"),
    # Above the 404 it replaces, because the first match wins and the row below would otherwise
    # claim this one. That ordering is the fix for issue #98: GitHub answers 404 both for a
    # repository that is not there and for one this bot may not see, so a private repository was
    # reported as missing and the person went and checked a link that was perfectly correct. The
    # service composes the sentence because only it knows which repository and which link to
    # offer, so the template is the message and nothing else.
    (NotInstalledError, "{message}"),
    # Not "at that link": the workflow commands take no link, and a 404 there means the
    # item has gone from GitHub since it was mirrored.
    (GitHubNotFoundError, "GitHub could not find that {noun}."),
    # Both of these are GitHubError and both used to fall through to it, so a spent quota and a
    # refused token were each reported as GitHub being unreachable. Neither is: GitHub answered,
    # and it said something the person in front of the bot can act on. One tells them when to come
    # back and the other tells them who to ask, which is the difference between a message worth
    # reading and one worth ignoring.
    (GitHubRateLimitError, "GitHub's rate limit is spent. {wait}"),
    (
        GitHubAuthError,
        "GitHub refused this bot's access, so it could not read that {noun}. "
        "An admin needs to check its GitHub token.",
    ),
    # Also above the catch-all, and for the sharper half of the same reason. GitHub answered and
    # said no, and it said why in a sentence better than any kept here: not a collaborator, the
    # item's own author, already asked. Reported as unreachable, all of those read as a fault to
    # wait out rather than something the person in front of the bot can put right in ten seconds.
    (GitHubRefusedError, "GitHub would not do that. {message}"),
    (GitHubError, "GitHub could not be reached. {message}"),
    # The same split as the two GitHub rows above, for the same reason, on the side of it that
    # is likelier to happen. Both of these are a DiscordGatewayError and both used to fall
    # through to it, so a permission nobody granted and a channel somebody deleted were each
    # reported as "Discord refused the update", followed by whatever discord.py had said, error
    # code and all. Neither is a refusal to wait out, and echoing a raw API message at somebody
    # sitting in Discord tells them nothing they can do.
    (
        DiscordPermissionError,
        "Discord will not let this bot do that here. An admin needs to give it the missing "
        "permission; the log says which one.",
    ),
    (
        ChannelNotFoundError,
        "The channel {noun} threads go in has gone, or is a kind that cannot hold threads. "
        "Run /set_channel to point them somewhere else.",
    ),
    (DiscordGatewayError, "Discord refused the update. {message}"),
    (ItemNotReadyError, "That {noun} is still being set up here. Try again in a moment."),
    (NotRegisteredError, "{message}"),
    # Its own row rather than falling through to the catch-all, because the message names
    # the account GitHub signed the person in as and what that account is missing. "Something
    # went wrong here" would leave somebody who genuinely cannot do this with no idea why.
    (NotProvenError, "{message}"),
    (RepositoryMismatchError, "{message}"),
    (DuplicateRegistrationError, "{message}"),
    (SyncFailedError, "{message}"),
    (InvalidGitHubUsernameError, "{message}"),
    (InvalidGitHubTeamError, "{message}"),
    (NotAnItemThreadError, "{message}"),
    (WorkflowRefusedError, "{message}"),
    (ItemMovedError, "{message}"),
    # All three say which thread and what state it is in, which is the whole of what somebody
    # running one of these needs, so there is nothing a template here could add.
    (AlreadyLoggingError, "{message}"),
    (NotLoggingError, "{message}"),
    (CannotLogError, "{message}"),
)

# Said when nothing above matches. Deliberately vague: whatever went wrong is a bug or an
# outage, and neither is the user's business beyond knowing it did not work.
UNEXPECTED = "Something went wrong here. It has been logged."


def _wait_for(seconds: object) -> str:
    """When to come back, in words a person reads rather than a number of seconds.

    GitHub says when its window reopens and the client already works it out, so the only reason
    not to pass it on is that nobody did. Rounded up, because telling somebody to wait less than
    the truth earns a second refusal.
    """
    if not isinstance(seconds, int) or seconds <= 0:
        return "Try again shortly."
    minutes = math.ceil(seconds / 60)
    if minutes == 1:
        return "Try again in a minute."
    return f"Try again in about {minutes} minutes."


# Which refusals come right on their own. Amber for those and red for the rest, so
# "wait a minute" and "somebody has to put this right" are told apart before either
# sentence has been read.
_COMES_RIGHT: tuple[type[ShannonError], ...] = (GitHubRateLimitError, ItemNotReadyError)


def reply_for(error: Exception, *, noun: str = "item") -> Panel:
    """The refusal for an error, or the catch-all if it is not one we know about.

    A card rather than a sentence since issue #116, and the words are unchanged. The
    bar is the only thing added, which is why every call site kept the line it had.
    """
    said = words_for(error, noun=noun)
    tone = Accent.MEDIUM if isinstance(error, _COMES_RIGHT) else Accent.FAILED
    return Panel(blocks=(Block(BlockKind.HEADING, said),), accent=tone)


def words_for(error: Exception, *, noun: str = "item") -> str:
    """The message for an error, or the catch-all if it is not one we know about."""
    # discord.py hands its error handler whatever a command raised wrapped in a
    # CommandInvokeError. Looking through that is what lets the table match at all when the
    # error arrives that way; without it everything unexpected reads as the catch-all.
    error = getattr(error, "original", error)

    for kind, template in _REPLIES:
        if isinstance(error, kind):
            return template.format(
                message=getattr(error, "message", str(error)),
                noun=noun,
                wait=_wait_for(getattr(error, "retry_after", None)),
            )
    return UNEXPECTED
