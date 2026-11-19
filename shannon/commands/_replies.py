"""What the member who ran a command is told when it fails, by error type, and with which mark.

The mark says whether the refusal comes right on its own (amber) or somebody has to put it right
(red). The failure itself is logged in `discord_bot/client.py`, not here. Issue #267.
"""

from __future__ import annotations

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
from shannon.services.boards import (
    BoardMovedError,
    BoardNotLinkedError,
    BoardTakenError,
    BoardUnreadableError,
)
from shannon.services.linking import InvalidGitHubTeamError, NoTeamsHereError
from shannon.services.reminders.book import TooManyRemindersError
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.one_at_a_time import ItemBusyError
from shannon.services.transcripts.log import (
    AlreadyLoggingError,
    CannotLogError,
    NotLoggingError,
)
from shannon.services.workflow import ItemMovedError, NotAnItemThreadError, WorkflowRefusedError

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
    # Both name the board and the command that puts it right, so a template could add nothing.
    # `BoardUnreadableError` had no row at all until `/refresh` learned to read a board: it is
    # raised inside `/board link`, which used to answer every refusal itself, so nothing had ever
    # asked this table about one and it came out as "Something went wrong here." Since #267 only
    # this one and an authorisation nobody granted are answered there; the rest come through here.
    (BoardNotLinkedError, "{message}"),
    (BoardUnreadableError, "{message}"),
    # Both name the board and what holds it, so a template could add nothing here either. Rows
    # of their own so that `/board link` can hand them to `reply_for` with every other error and
    # keep its words exactly, rather than answering them itself. Issue #267.
    (BoardTakenError, "{message}"),
    (BoardMovedError, "{message}"),
    (InvalidGitHubTeamError, "{message}"),
    # It had no row, so `/link_team` on a personal repository told the person who ran it
    # "Something went wrong here" instead of the sentence it raised to say why a team can never
    # be asked for a review there. Red: an account's kind does not come right by itself.
    (NoTeamsHereError, "{message}"),
    (NotAnItemThreadError, "{message}"),
    (WorkflowRefusedError, "{message}"),
    (ItemMovedError, "{message}"),
    # All three name the thread and the state it is in, so a template here could add nothing.
    (AlreadyLoggingError, "{message}"),
    (NotLoggingError, "{message}"),
    (CannotLogError, "{message}"),
    # Says how many may wait and what makes room for another. Issue #229.
    (TooManyRemindersError, "{message}"),
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
    # A reminder going off makes room for the next one.
    TooManyRemindersError,
)


def comes_right(error: BaseException) -> bool:
    """Whether a refusal comes right on its own, and so is amber rather than red.

    The one place the rule is kept. The tier guard asks it too, so a GitHub error added to
    `_COMES_RIGHT` turns amber there as well as here. Issue #267.

    Unwrapped first, as `words_for` is. The backstop in `discord_bot/client.py` only ever sees a
    CommandInvokeError, so a spent quota that escaped a command used to get the right words
    behind the red mark. Issue #267.
    """
    return isinstance(_unwrapped(error), _COMES_RIGHT)


def reply_for(error: BaseException, *, noun: str = "item") -> Panel:
    """The refusal for an error, or the catch-all if it is not one we know about.

    Which of the two marks it carries is the split `comes_right` makes: an error that comes right
    on its own is something still owed rather than something refused, and telling those apart
    before either sentence is read is the whole point of the pair.
    """
    said = words_for(error, noun=noun)
    return owed(said) if comes_right(error) else refused(said)


def words_for(error: BaseException, *, noun: str = "item") -> str:
    """The message for an error, or the catch-all if it is not one we know about."""
    error = _unwrapped(error)

    for kind, template in _REPLIES:
        if isinstance(error, kind):
            return template.format(
                message=getattr(error, "message", str(error)),
                noun=noun,
                wait=_wait_for(getattr(error, "retry_after", None)),
            )
    return UNEXPECTED


def _unwrapped(error: BaseException) -> BaseException:
    """The error a command raised, out of the wrapper discord.py may have put it in.

    discord.py hands its error handler whatever a command raised wrapped in a CommandInvokeError.
    Left wrapped, everything arriving that way reads as the catch-all.
    """
    original: BaseException = getattr(error, "original", error)
    return original
