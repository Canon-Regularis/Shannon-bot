from __future__ import annotations

from shannon.domain.errors import PermanentError, ShannonError


class DiscordGatewayError(ShannonError):
    """Discord refused an operation the sync path depends on."""


class ChannelNotFoundError(PermanentError, DiscordGatewayError):
    """The mapped channel is gone, or is a kind that cannot hold threads.

    Permanent because both need somebody to run /set_channel. Retrying for half an hour only
    delays the log line that says so.
    """


class ThreadNotFoundError(DiscordGatewayError):
    """The stored thread is gone.

    Callers treat this as a signal to rebuild rather than as a failure, because a thread someone
    deleted is never coming back and retrying the same id forever would lose every later event
    for that item.
    """


class ThreadStartedEmptyError(DiscordGatewayError):
    """The thread was created but its first message did not land.

    Carries the id so the caller can record the thread before failing. Without that the id is
    lost, and the retry opens a second thread beside the empty one.
    """

    def __init__(self, message: str, *, thread_id: int) -> None:
        super().__init__(message)
        self.thread_id = thread_id


class DiscordPermissionError(PermanentError, DiscordGatewayError):
    """The bot is missing a permission.

    Separate from the rest because no amount of waiting grants a permission. Someone has to
    change the channel settings, so this is reported once rather than retried for two hours.
    """
