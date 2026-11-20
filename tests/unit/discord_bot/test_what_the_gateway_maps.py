"""How `DiscordThreadGateway` names Discord's refusals, pinned before issue #269 folds them up.

`test_threads.py` already pins which class each refusal becomes. What it leaves open is the
message each one carries, the cause it chains, and which lookups pay a fetch - the parts #269
moves when the channel and thread lookups become one helper. Those are pinned here, as the current
code writes them.

Two things are left out on purpose, because #269 changes them: a transport failure (an `OSError`
or an aiohttp error) escaping the mapping, and a refused first message in a new text-channel
thread coming out as a retryable error.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from unittest.mock import MagicMock, NonCallableMagicMock

import discord
import pytest

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
)
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.threads import DiscordThreadGateway
from tests.unit.discord_bot.test_threads import a_channel_taking, client_with, thread

Call = Callable[[DiscordThreadGateway], Awaitable[object]]


def _post(gateway: DiscordThreadGateway) -> Awaitable[object]:
    return gateway.post(thread_id=500, panel=Panel.of_text("x"))


def _update(gateway: DiscordThreadGateway) -> Awaitable[object]:
    return gateway.update(
        thread_id=500, message_id=None, name="#7 Renamed", panel=Panel.of_text("x")
    )


def _post_in_channel(gateway: DiscordThreadGateway) -> Awaitable[object]:
    return gateway.post_in_channel(channel_id=10, panel=Panel.of_text("x"))


def _create(gateway: DiscordThreadGateway) -> Awaitable[object]:
    return gateway.create(channel_id=10, name="#7 Title", panel=Panel.of_text("x"))


def _forbidden() -> discord.Forbidden:
    return discord.Forbidden(MagicMock(status=403), "Missing Access")


def _not_found() -> discord.NotFound:
    return discord.NotFound(MagicMock(status=404), "Unknown Channel")


def _outage() -> discord.HTTPException:
    return discord.HTTPException(MagicMock(status=503), "unavailable")


def _missing_from_the_cache(error: Exception) -> NonCallableMagicMock:
    """A client whose cache misses, so the lookup goes to a fetch that raises `error`."""
    client = client_with(None)
    client.fetch_channel.side_effect = error
    return client


class TestARefusedWrite:
    """A post or an update Discord refuses, named by what the bot was trying to do."""

    @pytest.mark.parametrize(
        ("call", "what"),
        [(_post, "post to the thread"), (_update, "update the thread")],
        ids=["post", "update"],
    )
    async def test_forbidden_is_a_permission_error_naming_the_write(
        self, call: Call, what: str
    ) -> None:
        existing = thread()
        error = _forbidden()
        existing.send.side_effect = error
        existing.edit.side_effect = error

        with pytest.raises(DiscordPermissionError) as caught:
            await call(DiscordThreadGateway(client_with(existing)))

        assert str(caught.value) == f"Discord will not let the bot {what}: {error}"
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize(
        ("call", "what"),
        [(_post, "post to the thread"), (_update, "update the thread")],
        ids=["post", "update"],
    )
    async def test_any_other_refusal_is_a_gateway_error_naming_the_write(
        self, call: Call, what: str
    ) -> None:
        existing = thread()
        error = _outage()
        existing.send.side_effect = error
        existing.edit.side_effect = error

        with pytest.raises(DiscordGatewayError) as caught:
            await call(DiscordThreadGateway(client_with(existing)))

        assert type(caught.value) is DiscordGatewayError
        assert str(caught.value) == f"Discord refused to {what}: {error}"
        assert caught.value.__cause__ is error

    async def test_a_message_box_that_answers_not_found_is_a_refusal_not_a_missing_thread(
        self,
    ) -> None:
        """Only the LOOKUP reads a 404 as gone. Once the thread is in hand, a 404 on the write is
        one more refusal, and the caller does not rebuild on it."""
        existing = thread()
        error = _not_found()
        existing.send.side_effect = error

        with pytest.raises(DiscordGatewayError) as caught:
            await _post(DiscordThreadGateway(client_with(existing)))

        assert type(caught.value) is DiscordGatewayError
        assert str(caught.value) == f"Discord refused to post to the thread: {error}"

    async def test_a_refusal_in_a_channel_names_the_channel_write(self) -> None:
        channel = a_channel_taking(discord.TextChannel)
        error = _forbidden()
        channel.send.side_effect = error

        with pytest.raises(DiscordPermissionError) as caught:
            await _post_in_channel(DiscordThreadGateway(client_with(channel)))

        assert str(caught.value) == f"Discord will not let the bot post in the channel: {error}"


class TestLookingUpAThread:
    """`_thread`, reached through `post` and `update`: cache first, then a fetch."""

    @pytest.mark.parametrize("call", [_post, _update], ids=["post", "update"])
    async def test_a_cached_thread_costs_no_fetch(self, call: Call) -> None:
        client = client_with(thread())

        await call(DiscordThreadGateway(client))

        client.get_channel.assert_called_once_with(500)
        client.fetch_channel.assert_not_awaited()

    @pytest.mark.parametrize("call", [_post, _update], ids=["post", "update"])
    async def test_a_thread_missing_from_the_cache_is_fetched_by_its_id(self, call: Call) -> None:
        existing = thread()
        client = client_with(existing)
        client.get_channel.return_value = None

        await call(DiscordThreadGateway(client))

        client.fetch_channel.assert_awaited_once_with(500)
        existing.send.assert_awaited()

    @pytest.mark.parametrize("call", [_post, _update], ids=["post", "update"])
    async def test_not_found_says_the_thread_is_not_there(self, call: Call) -> None:
        error = _not_found()

        with pytest.raises(ThreadNotFoundError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert str(caught.value) == "Thread 500 is not there"
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize("call", [_post, _update], ids=["post", "update"])
    async def test_forbidden_says_the_bot_may_not_see_it(self, call: Call) -> None:
        """Without Discord's own words, unlike a refused write: they travel as the cause."""
        error = _forbidden()

        with pytest.raises(DiscordPermissionError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert str(caught.value) == "Discord will not let the bot see thread 500"
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize("call", [_post, _update], ids=["post", "update"])
    async def test_an_outage_says_the_lookup_was_refused(self, call: Call) -> None:
        error = _outage()

        with pytest.raises(DiscordGatewayError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert type(caught.value) is DiscordGatewayError
        assert str(caught.value) == f"Discord refused to look up thread 500: {error}"
        assert caught.value.__cause__ is error


class TestLookingUpAChannel:
    """`_channel`, reached through `create` and `post_in_channel`: cache first, then a fetch."""

    @pytest.mark.parametrize("call", [_create, _post_in_channel], ids=["create", "in-channel"])
    async def test_a_cached_channel_costs_no_fetch(self, call: Call) -> None:
        channel = a_channel_taking(discord.TextChannel)
        channel.create_thread.return_value = thread()
        client = client_with(channel)

        await call(DiscordThreadGateway(client))

        client.get_channel.assert_called_once_with(10)
        client.fetch_channel.assert_not_awaited()

    async def test_a_channel_missing_from_the_cache_is_fetched_for_a_create(self) -> None:
        channel = a_channel_taking(discord.TextChannel)
        channel.create_thread.return_value = thread()
        client = client_with(channel)
        client.get_channel.return_value = None

        await _create(DiscordThreadGateway(client))

        client.fetch_channel.assert_awaited_once_with(10)
        channel.create_thread.assert_awaited_once()

    @pytest.mark.parametrize("call", [_create, _post_in_channel], ids=["create", "in-channel"])
    async def test_not_found_says_the_channel_is_not_there(self, call: Call) -> None:
        error = _not_found()

        with pytest.raises(ChannelNotFoundError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert str(caught.value) == "Channel 10 is not there"
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize("call", [_create, _post_in_channel], ids=["create", "in-channel"])
    async def test_forbidden_says_the_bot_may_not_see_it(self, call: Call) -> None:
        error = _forbidden()

        with pytest.raises(DiscordPermissionError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert str(caught.value) == "Discord will not let the bot see channel 10"
        assert caught.value.__cause__ is error

    @pytest.mark.parametrize("call", [_create, _post_in_channel], ids=["create", "in-channel"])
    async def test_an_outage_says_the_lookup_was_refused(self, call: Call) -> None:
        error = _outage()

        with pytest.raises(DiscordGatewayError) as caught:
            await call(DiscordThreadGateway(_missing_from_the_cache(error)))

        assert type(caught.value) is DiscordGatewayError
        assert str(caught.value) == f"Discord refused to look up channel 10: {error}"
        assert caught.value.__cause__ is error


class TestDeletingAStrandedThread:
    """`delete` logs this project's gateway errors and lets nothing else through quietly."""

    async def test_a_refused_delete_is_logged_with_what_discord_said(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        existing = thread()
        error = _forbidden()
        existing.delete.side_effect = error

        with caplog.at_level("WARNING", logger="shannon.discord_bot.threads"):
            await DiscordThreadGateway(client_with(existing)).delete(thread_id=500)

        assert caplog.messages == [
            "could not remove the stranded thread 500: "
            f"Discord will not let the bot delete the thread: {error}"
        ]

    async def test_a_thread_already_gone_is_logged_rather_than_raised(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="shannon.discord_bot.threads"):
            await DiscordThreadGateway(_missing_from_the_cache(_not_found())).delete(thread_id=500)

        assert caplog.messages == [
            "could not remove the stranded thread 500: Thread 500 is not there"
        ]

    async def test_something_that_is_not_a_gateway_error_still_raises(self) -> None:
        """A bug is not Discord having a bad minute. Transport errors are left out: #269 maps
        them to the gateway error, after which `delete` logs them too."""
        existing = thread()
        existing.delete.side_effect = RuntimeError("a bug")

        with pytest.raises(RuntimeError, match="a bug"):
            await DiscordThreadGateway(client_with(existing)).delete(thread_id=500)
