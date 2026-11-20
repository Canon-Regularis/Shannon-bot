from __future__ import annotations

from shannon.domain.errors import PermanentError, ShannonError


class DiscordGatewayError(ShannonError):
    """Discord refused an operation, or could not be reached to be asked.

    Raised in place of discord.py's own errors, so callers see one family. Each subclass names a
    case that needs different handling.
    """


class DiscordUnreachableError(DiscordGatewayError):
    """Discord was not reached at all: a reset or dropped connection, after discord.py's retries.

    Its own class so nothing words it as a refusal, issue #269. Not permanent, so the worker
    retries it.
    """


class ChannelNotFoundError(PermanentError, DiscordGatewayError):
    """The mapped channel is gone or cannot hold threads; only /set_channel mends it."""


class ThreadNotFoundError(DiscordGatewayError):
    """The stored thread is gone.

    Callers rebuild rather than retry: a deleted thread never comes back, and retrying the same
    id would lose every later event for that item.
    """


class ThreadStartedEmptyError(DiscordGatewayError):
    """The thread was created but its first message did not land.

    Carries the id, or the retry opens a second thread beside the empty one.
    """

    def __init__(self, message: str, *, thread_id: int) -> None:
        super().__init__(message)
        self.thread_id = thread_id


class DiscordPermissionError(PermanentError, DiscordGatewayError):
    """The bot is missing a permission; permanent, since waiting never grants one."""


class ThreadStartedWithoutPermissionError(DiscordPermissionError, ThreadStartedEmptyError):
    """The thread was created and Discord would not let the bot write its first message.

    Both at once, issue #269. An empty thread, so the sync still records its id and nothing opens
    a second one; and a missing permission, so the worker gives up at once and a command answers
    with the permission row. As a plain empty thread it was retried for nothing, and the refusal
    was readable only off `__cause__`, which nothing reads.
    """
