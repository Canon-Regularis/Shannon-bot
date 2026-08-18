from __future__ import annotations

import logging

from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.errors import (
    DuplicateRegistrationError,
    ItemNotReadyError,
    NotRegisteredError,
    RepositoryMismatchError,
    ShannonError,
    UnparseableLinkError,
)
from shannon.github.errors import GitHubError, GitHubNotFoundError
from shannon.services.linking import InvalidGitHubUsernameError
from shannon.services.sync.manual import SyncFailedError

logger = logging.getLogger(__name__)

# What the person who ran the command is told, by error type. Ordered most specific first: the
# first match wins and several of these share a base class. One table for every command, so
# nothing escapes after the interaction has been deferred and leaves the caller with silence.
_REPLIES: tuple[tuple[type[ShannonError], str], ...] = (
    (UnparseableLinkError, "That link did not work. {message}"),
    (GitHubNotFoundError, "GitHub has no {noun} at that link."),
    (GitHubError, "GitHub could not be reached. {message}"),
    (DiscordGatewayError, "Discord refused the update. {message}"),
    (ItemNotReadyError, "That {noun} is still being set up here. Try again in a moment."),
    (NotRegisteredError, "{message}"),
    (RepositoryMismatchError, "{message}"),
    (DuplicateRegistrationError, "{message}"),
    (SyncFailedError, "{message}"),
    (InvalidGitHubUsernameError, "{message}"),
)

# Said when nothing above matches. Deliberately vague: whatever went wrong is a bug or an
# outage, and neither is the user's business beyond knowing it did not work.
UNEXPECTED = "Something went wrong here. It has been logged."


def reply_for(error: Exception, *, noun: str = "item") -> str:
    """The message for an error, or the catch-all if it is not one we know about."""
    # discord.py hands its error handler whatever a command raised wrapped in a
    # CommandInvokeError. Looking through that is what lets the table match at all when the
    # error arrives that way; without it everything unexpected reads as the catch-all.
    error = getattr(error, "original", error)

    for kind, template in _REPLIES:
        if isinstance(error, kind):
            return template.format(message=getattr(error, "message", str(error)), noun=noun)
    return UNEXPECTED
