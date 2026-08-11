from __future__ import annotations

from shannon.domain.errors import ShannonError


class DiscordGatewayError(ShannonError):
    """Discord refused an operation the sync path depends on."""


class ChannelNotFoundError(DiscordGatewayError):
    """The configured channel no longer exists, or the bot cannot see it."""


class ThreadNotFoundError(DiscordGatewayError):
    """The stored thread is gone, so there is nothing left to update."""
