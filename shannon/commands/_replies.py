from __future__ import annotations

import logging
import math

from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.errors import (
    DuplicateRegistrationError,
    ItemNotReadyError,
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
)
from shannon.services.linking import InvalidGitHubTeamError, InvalidGitHubUsernameError
from shannon.services.sync.manual import SyncFailedError
from shannon.services.workflow import NotAnItemThreadError, WorkflowRefusedError

logger = logging.getLogger(__name__)

# What the person who ran the command is told, by error type. Ordered most specific first: the
# first match wins and several of these share a base class. One table for every command, so
# nothing escapes after the interaction has been deferred and leaves the caller with silence.
_REPLIES: tuple[tuple[type[ShannonError], str], ...] = (
    (UnparseableLinkError, "That link did not work. {message}"),
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
    (GitHubError, "GitHub could not be reached. {message}"),
    (DiscordGatewayError, "Discord refused the update. {message}"),
    (ItemNotReadyError, "That {noun} is still being set up here. Try again in a moment."),
    (NotRegisteredError, "{message}"),
    (RepositoryMismatchError, "{message}"),
    (DuplicateRegistrationError, "{message}"),
    (SyncFailedError, "{message}"),
    (InvalidGitHubUsernameError, "{message}"),
    (InvalidGitHubTeamError, "{message}"),
    (NotAnItemThreadError, "{message}"),
    (WorkflowRefusedError, "{message}"),
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


def reply_for(error: Exception, *, noun: str = "item") -> str:
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
