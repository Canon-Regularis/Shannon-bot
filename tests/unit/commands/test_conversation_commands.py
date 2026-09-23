"""`/log_conversation` and `/stop_conversation`, against a stub.

Issue #103. Two things here are unlike the other command modules. `/log_conversation` refuses when
the deployment has not turned capture on, because the intent it needs is a Developer Portal toggle
this process cannot check for itself, and `/stop_conversation` deliberately does not, so a thread
that was told logging is on can always be made to stop.
"""

from __future__ import annotations

import pytest

from shannon.commands.conversations import (
    NOT_CAPTURING,
    build_log_conversation_command,
    build_stop_conversation_command,
)
from shannon.domain.errors import RepositoryMismatchError
from shannon.services.transcripts.log import (
    AlreadyLoggingError,
    CannotLogError,
    NotLoggingError,
)
from shannon.services.workflow import NotAnItemThreadError
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

THREAD = 9001


class StubLog:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[tuple[int, int]] = []
        self.stopped: list[tuple[int, int]] = []

    async def start(self, *, thread_id: int, by: int) -> tuple[str, int]:
        self.started.append((thread_id, by))
        if self.error is not None:
            raise self.error
        return "acme/widget", 7

    async def stop(self, *, thread_id: int, by: int) -> tuple[str, int]:
        self.stopped.append((thread_id, by))
        if self.error is not None:
            raise self.error
        return "acme/widget", 7


def run_it(
    *,
    service: StubLog | None = None,
    who=None,
    channel_id: int | None = THREAD,
    stopping: bool = False,
    capturing: bool = True,
):
    service = service or StubLog()
    command = (
        build_stop_conversation_command(service, default_gate())
        if stopping
        else build_log_conversation_command(service, default_gate(), capturing=capturing)
    )
    interaction = FakeInteraction(user=who or developer(), channel_id=channel_id)
    return command, interaction, service


class TestWhoMayRunIt:
    @pytest.mark.parametrize("who", [developer, project_manager, administrator])
    async def test_the_tiers_that_may(self, who) -> None:
        command, interaction, service = run_it(who=who())

        await command.callback(interaction)

        assert service.started == [(THREAD, interaction.user.id)]

    async def test_anybody_else_is_refused_before_anything_is_published(self) -> None:
        from tests.unit.commands.conftest import member_with

        command, interaction, service = run_it(who=member_with("Reviewer"))

        await command.callback(interaction)

        assert "You need one of these roles" in interaction.reply
        assert service.started == []


class TestWhereItHasToBeRun:
    async def test_outside_a_server(self) -> None:
        command, interaction, service = run_it()
        interaction.guild_id = None

        await command.callback(interaction)

        assert interaction.said == "Run this inside a server channel."
        assert service.started == []

    async def test_with_no_channel_at_all(self) -> None:
        command, interaction, service = run_it(channel_id=None)

        await command.callback(interaction)

        assert interaction.said == "Run this inside the item's thread."
        assert service.started == []


class TestWhenTheDeploymentCannotReadMessages:
    async def test_starting_is_refused_and_says_what_to_do(self) -> None:
        """The intent is a Developer Portal toggle, and missing it stops the whole process
        starting. So the setting is the gate, and this is what somebody sees until both are on."""
        command, interaction, service = run_it(capturing=False)

        await command.callback(interaction)

        assert interaction.said == NOT_CAPTURING
        assert service.started == []

    async def test_stopping_still_works(self) -> None:
        """Turning capture off where it used to be on leaves conversations open. Nothing is
        captured into them, but somebody in that thread has been told logging is on."""
        command, interaction, service = run_it(stopping=True)

        await command.callback(interaction)

        assert service.stopped == [(THREAD, interaction.user.id)]


class TestWhatItSays:
    async def test_starting_names_the_item_and_that_the_thread_was_told(self) -> None:
        """The notice is what makes publishing somebody's words defensible, and whoever ran the
        command cannot see their own ephemeral reply and the thread line at the same time."""
        command, interaction, _ = run_it()

        await command.callback(interaction)

        assert interaction.said == (
            "Logging this thread to acme/widget#7. Everyone in the thread has been told."
        )

    async def test_stopping_says_the_tail_is_still_coming(self) -> None:
        command, interaction, _ = run_it(stopping=True)

        await command.callback(interaction)

        assert interaction.said == (
            "Stopped logging this thread to acme/widget#7. Anything still waiting will be "
            "published."
        )


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (AlreadyLoggingError("This thread is already being logged to GitHub."), "already"),
            (NotLoggingError("This thread is not being logged to GitHub."), "not being logged"),
            (CannotLogError("A project board card has no GitHub comments."), "board card"),
            (NotAnItemThreadError("Run this inside the thread of a tracked item."), "tracked item"),
            (RepositoryMismatchError("acme/widget is not that repository any more."), "any more"),
        ],
    )
    async def test_a_refusal_is_reported_in_its_own_words(
        self, error: Exception, expected: str
    ) -> None:
        command, interaction, _ = run_it(service=StubLog(error=error))

        await command.callback(interaction)

        assert expected in interaction.reply

    async def test_anything_that_is_not_ours_is_left_to_the_handler(self) -> None:
        command, interaction, _ = run_it(service=StubLog(error=RuntimeError("a bug")))

        with pytest.raises(RuntimeError, match="a bug"):
            await command.callback(interaction)
