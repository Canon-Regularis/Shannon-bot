from __future__ import annotations

from shannon.domain.errors import PermanentError, ShannonError


class DiscordGatewayError(ShannonError):
    """Discord refused an operation the sync path depends on."""


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
