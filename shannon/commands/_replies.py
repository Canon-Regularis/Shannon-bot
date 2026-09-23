from __future__ import annotations

import logging
import math

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
)
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.responses import owed, refused
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
from shannon.services.linking import InvalidGitHubTeamError
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.one_at_a_time import ItemBusyError
from shannon.services.transcripts.log import (
    AlreadyLoggingError,
    CannotLogError,
    NotLoggingError,
)
from shannon.services.workflow import ItemMovedError, NotAnItemThreadError, WorkflowRefusedError

logger = logging.getLogger(__name__)

# What the person who ran the command is told, by error type. Ordered most specific first,
# because the first match wins and several of these share a base class.
_REPLIES: tuple[tuple[type[ShannonError], str], ...] = (
    (UnparseableLinkError, "That link did not work. {message}"),
    # Above the 404 row that would otherwise claim it: GitHub answers 404 both for a repository
    # that is not there and for one this bot may not see, so a private repository was reported as
    # missing and the person went and checked a link that was perfectly correct.
    (NotInstalledError, "{message}"),
    # Not "at that link": the workflow commands take no link, and a 404 there means the item has
    # gone from GitHub since it was mirrored.
    (GitHubNotFoundError, "GitHub could not find that {noun}."),
    # Both are GitHubError and must stay above the catch-all row, or a spent quota and a refused
    # token read as GitHub being unreachable when GitHub answered and said when to come back or
    # who to ask.
    (GitHubRateLimitError, "GitHub's rate limit is spent. {wait}"),
    (
        GitHubAuthError,
        "GitHub refused this bot's access, so it could not read that {noun}. "
        "An admin needs to check its GitHub token.",
    ),
    # Also above the catch-all: GitHub said no and said why in a sentence better than any kept
    # here — not a collaborator, the item's own author, already asked.
    (GitHubRefusedError, "GitHub would not do that. {message}"),
    (GitHubError, "GitHub could not be reached. {message}"),
    # The same split as the two GitHub rows: both are DiscordGatewayError and must stay above it,
    # or a permission nobody granted and a channel somebody deleted come back as "Discord refused
    # the update" followed by whatever discord.py said, error code and all.
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
    # Somebody else's write, not a fault: a delivery for the same item is in flight and did
    # not finish inside the wait.
    (ItemBusyError, "Something else is changing that {noun} right now. Try again in a moment."),
    (NotRegisteredError, "{message}"),
    # Its own row because the message names the account GitHub signed the person in as and what
    # that account is missing.
    (NotProvenError, "{message}"),
    (RepositoryMismatchError, "{message}"),
    (DuplicateRegistrationError, "{message}"),
    (SyncFailedError, "{message}"),
    (InvalidGitHubTeamError, "{message}"),
    (NotAnItemThreadError, "{message}"),
    (WorkflowRefusedError, "{message}"),
    (ItemMovedError, "{message}"),
    # All three name the thread and the state it is in, so a template here could add nothing.
    (AlreadyLoggingError, "{message}"),
    (NotLoggingError, "{message}"),
    (CannotLogError, "{message}"),
)

# Said when nothing above matches. Vague on purpose: what went wrong is a bug or an outage.
UNEXPECTED = "Something went wrong here. It has been logged."


def _wait_for(seconds: object) -> str:
    """When to come back, in words a person reads rather than a number of seconds.

    Rounded up: telling somebody to wait less than the truth earns a second refusal.
    """
    if not isinstance(seconds, int) or seconds <= 0:
        return "Try again shortly."
    minutes = math.ceil(seconds / 60)
    if minutes == 1:
        return "Try again in a minute."
    return f"Try again in about {minutes} minutes."


# Which refusals come right on their own: amber for those and red for the rest, so "wait a
# minute" and "somebody has to put this right" are told apart before either sentence is read.
_COMES_RIGHT: tuple[type[ShannonError], ...] = (
    GitHubRateLimitError,
    ItemNotReadyError,
    ItemBusyError,
)


def reply_for(error: BaseException, *, noun: str = "item") -> Panel:
    """The refusal for an error, or the catch-all if it is not one we know about.

    Which of the two marks it carries is the split `_COMES_RIGHT` already makes: an error that
    comes right on its own is something still owed rather than something refused, and telling
    those apart before either sentence is read is the whole point of the pair.
    """
    said = words_for(error, noun=noun)
    return owed(said) if isinstance(error, _COMES_RIGHT) else refused(said)


def words_for(error: BaseException, *, noun: str = "item") -> str:
    """The message for an error, or the catch-all if it is not one we know about."""
    # discord.py hands its error handler whatever a command raised wrapped in a
    # CommandInvokeError; unwrapped, everything arriving that way reads as the catch-all.
    error = getattr(error, "original", error)

    for kind, template in _REPLIES:
        if isinstance(error, kind):
            return template.format(
                message=getattr(error, "message", str(error)),
                noun=noun,
                wait=_wait_for(getattr(error, "retry_after", None)),
            )
    return UNEXPECTED
