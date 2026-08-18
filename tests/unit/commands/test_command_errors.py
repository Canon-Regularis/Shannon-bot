from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from shannon.commands._replies import UNEXPECTED, reply_for
from shannon.discord_bot.client import ShannonBot
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.formatting import MESSAGE_LIMIT
from shannon.discord_bot.responses import reply
from shannon.domain.errors import NotRegisteredError
from shannon.github.errors import GitHubNotFoundError, GitHubRateLimitError
from tests.fakes.discord_objects import FakeInteraction


def wrapped(error: Exception) -> app_commands.CommandInvokeError:
    """What discord.py hands its error handler: the real error inside a wrapper."""
    return app_commands.CommandInvokeError(MagicMock(name="command"), error)


class TestReadingAnError:
    def test_a_known_error_gets_its_own_message(self) -> None:
        assert reply_for(NotRegisteredError("Run /register first.")) == "Run /register first."

    def test_the_kind_of_item_is_named(self) -> None:
        assert reply_for(GitHubNotFoundError("gone"), noun="issue") == (
            "GitHub has no issue at that link."
        )

    def test_the_most_specific_match_wins(self) -> None:
        """GitHubNotFoundError is a GitHubError, and only one of the two answers is useful."""
        assert "GitHub has no" in reply_for(GitHubNotFoundError("gone"))
        assert "could not be reached" in reply_for(GitHubRateLimitError("slow down"))

    def test_something_unrecognised_gets_the_catch_all(self) -> None:
        assert reply_for(RuntimeError("connection pool exhausted")) == UNEXPECTED

    def test_an_error_discord_wrapped_is_still_recognised(self) -> None:
        """Without unwrapping, everything reaching the tree handler reads as the catch-all."""
        assert reply_for(wrapped(NotRegisteredError("Run /register first."))) == (
            "Run /register first."
        )

    def test_a_wrapped_gateway_failure_is_recognised_too(self) -> None:
        assert "Discord refused the update" in reply_for(wrapped(DiscordGatewayError("no")))

    def test_a_wrapped_unknown_still_gets_the_catch_all(self) -> None:
        assert reply_for(wrapped(RuntimeError("boom"))) == UNEXPECTED


class TestTheBackstop:
    """Each command answers what it expects. This answers everything else."""

    async def test_an_unexpected_error_still_gets_a_reply(self) -> None:
        bot = ShannonBot(explain_error=reply_for)
        interaction = FakeInteraction()

        await bot.tree.on_error(interaction, wrapped(RuntimeError("pool exhausted")))

        assert interaction.reply == UNEXPECTED

    async def test_a_known_error_that_escaped_gets_its_real_message(self) -> None:
        bot = ShannonBot(explain_error=reply_for)
        interaction = FakeInteraction()

        await bot.tree.on_error(interaction, wrapped(NotRegisteredError("Run /register first.")))

        assert interaction.reply == "Run /register first."

    async def test_an_interaction_that_has_gone_away_is_not_worth_raising_over(self) -> None:
        """The handler raising would log a second traceback and still tell nobody."""
        bot = ShannonBot(explain_error=reply_for)
        interaction = FakeInteraction()
        interaction.response.send_message = _refusing

        await bot.tree.on_error(interaction, wrapped(RuntimeError("boom")))

    async def test_it_says_which_command_failed_in_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        bot = ShannonBot(explain_error=reply_for)
        with caplog.at_level("ERROR", logger="shannon.discord_bot.client"):
            await bot.tree.on_error(FakeInteraction(), wrapped(RuntimeError("boom")))

        assert "a slash command failed" in caplog.text
        assert "boom" in caplog.text


async def _refusing(*_args: object, **_kwargs: object) -> None:
    raise discord.NotFound(MagicMock(status=404), "unknown interaction")


class TestARepliesThatWouldNotFit:
    """Several replies quote back what the person typed, and an argument can be far too long."""

    async def test_an_enormous_reply_is_trimmed_to_discord_s_limit(self) -> None:
        interaction = FakeInteraction()

        await reply(interaction, "x" * 5000)

        assert len(interaction.reply) <= MESSAGE_LIMIT
        assert interaction.reply.endswith("…")

    async def test_an_ordinary_reply_is_untouched(self) -> None:
        interaction = FakeInteraction()

        await reply(interaction, "Registered owner/repo.")

        assert interaction.reply == "Registered owner/repo."
