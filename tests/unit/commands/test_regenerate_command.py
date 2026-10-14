"""`/regenerate`, against a stub.

Issue #65. The command itself is four guards and a sentence, so what is worth pinning is the
wording: this is the one command whose whole purpose is to correct something, and a reply that
glosses over a thread having moved or a lock not going back leaves somebody believing a thread is
in a state it is not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest

from shannon.commands._cooldowns import Cooldown
from shannon.commands.regenerate import REGENERATE_SPACING_SECONDS, build_regenerate_command
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.discord_bot.responses import OWED, REFUSED
from shannon.domain.errors import RepositoryMismatchError
from shannon.github.errors import GitHubNotFoundError
from shannon.services.sync.manual import SyncFailedError
from shannon.services.sync.regenerate import RegenerateOutcome
from shannon.services.workflow import NotAnItemThreadError, WorkflowRefusedError
from tests.fakes.discord_objects import FakeInteraction, FakeResponse
from tests.unit.commands.conftest import (
    administrator,
    default_gate,
    developer,
    member_with,
    project_manager,
)

pytestmark = pytest.mark.unit

THREAD = 9001
REDREW = RegenerateOutcome(
    full_name="acme/widget", number=7, thread_id=THREAD, created=False, shut_refused=False
)


class StubRegeneration:
    def __init__(
        self, *, outcome: RegenerateOutcome | None = REDREW, error: Exception | None = None
    ) -> None:
        self.outcome = outcome
        self.error = error
        self.calls: list[int] = []

    async def regenerate(self, *, thread_id: int) -> RegenerateOutcome:
        self.calls.append(thread_id)
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome


def run_it(*, service: StubRegeneration | None = None, who=None, channel_id: int | None = THREAD):
    service = service or StubRegeneration()
    command = build_regenerate_command(service, default_gate())
    interaction = FakeInteraction(user=who or developer(), channel_id=channel_id)
    return command, interaction, service


class TestWhoMayRunIt:
    async def test_a_developer_may(self) -> None:
        command, interaction, service = run_it(who=developer())

        await command.callback(interaction)

        assert service.calls == [THREAD]

    async def test_a_project_manager_may(self) -> None:
        command, interaction, service = run_it(who=project_manager())

        await command.callback(interaction)

        assert service.calls == [THREAD]

    async def test_an_administrator_outranks_the_configuration(self) -> None:
        command, interaction, service = run_it(who=administrator())

        await command.callback(interaction)

        assert service.calls == [THREAD]

    async def test_anybody_else_is_refused_before_anything_is_read(self) -> None:
        """The same tier as `/pr` and `/refresh`: it reads from GitHub and redraws a display, and
        changes nothing on GitHub and no server setting."""
        from tests.unit.commands.conftest import member_with

        command, interaction, service = run_it(who=member_with("Reviewer"))

        await command.callback(interaction)

        assert "You need one of these roles" in interaction.reply
        assert service.calls == []


class TestWhereItHasToBeRun:
    async def test_outside_a_server(self) -> None:
        command, interaction, service = run_it()
        interaction.guild_id = None

        await command.callback(interaction)

        assert interaction.said == "Run this inside a server channel."
        assert service.calls == []

    async def test_with_no_channel_at_all(self) -> None:
        """Checked after the role, so somebody who could not run it anyway is not told how it
        works."""
        command, interaction, service = run_it(channel_id=None)

        await command.callback(interaction)

        assert interaction.said == "Run this inside the item's thread."
        assert service.calls == []

    async def test_the_thread_it_acts_on_is_the_one_it_was_run_in(self) -> None:
        """Inside a thread Discord's channel id IS the thread id, which is what lets the command
        take no argument at all."""
        command, interaction, service = run_it(channel_id=4242)

        await command.callback(interaction)

        assert service.calls == [4242]


class TestWhatItSays:
    async def test_the_ordinary_redraw(self) -> None:
        command, interaction, _ = run_it()

        await command.callback(interaction)

        assert interaction.said == (
            f"Redrew acme/widget#7 from GitHub: <#{THREAD}>. Nobody was pinged."
        )

    async def test_a_thread_that_had_to_be_replaced_says_so(self) -> None:
        """The link is not the thread they ran it in, so leaving them to notice is how somebody
        ends up looking at an empty thread wondering what happened."""
        command, interaction, _ = run_it(
            service=StubRegeneration(
                outcome=RegenerateOutcome(
                    full_name="acme/widget",
                    number=7,
                    thread_id=5555,
                    created=True,
                    shut_refused=False,
                )
            )
        )

        await command.callback(interaction)

        assert "had gone, so it has a new one: <#5555>" in interaction.reply
        assert "Nobody was pinged." in interaction.reply

    async def test_a_lock_that_did_not_go_back_says_so(self) -> None:
        """Worse than before the command was run: writing to an archived thread wakes it, so a
        refused shut leaves a finished item's thread open in the channel."""
        command, interaction, _ = run_it(
            service=StubRegeneration(
                outcome=RegenerateOutcome(
                    full_name="acme/widget",
                    number=7,
                    thread_id=THREAD,
                    created=False,
                    shut_refused=True,
                )
            )
        )

        await command.callback(interaction)

        assert "could not be closed again" in interaction.reply
        assert "Manage Threads" in interaction.reply

    async def test_it_always_says_nobody_was_pinged(self) -> None:
        """It is the one surprising thing about the command. A redraw names everybody on the item
        as a live mention, and somebody watching that happen has every reason to expect the
        notifications that usually go with them."""
        command, interaction, _ = run_it()

        await command.callback(interaction)

        assert interaction.said.endswith("Nobody was pinged.")


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (NotAnItemThreadError("Run this inside the thread of a tracked item."), "tracked item"),
            (WorkflowRefusedError("acme/widget is a project board card."), "board card"),
            (RepositoryMismatchError("acme/widget is not that repository any more."), "any more"),
            (SyncFailedError("acme/widget#7 could not be redrawn just now."), "could not be"),
        ],
    )
    async def test_a_refusal_is_reported_in_its_own_words(
        self, error: Exception, expected: str
    ) -> None:
        command, interaction, _ = run_it(service=StubRegeneration(error=error))

        await command.callback(interaction)

        assert expected in interaction.reply

    async def test_an_item_deleted_on_github_is_called_an_item(self) -> None:
        """No noun is passed, so the 404 row falls back to its default. The command takes no link
        and does not know which kind of item it is in until it has looked."""
        command, interaction, _ = run_it(service=StubRegeneration(error=GitHubNotFoundError("x")))

        await command.callback(interaction)

        assert interaction.said == "GitHub could not find that item."

    async def test_discord_refusing_is_reported_rather_than_raised(self) -> None:
        command, interaction, _ = run_it(
            service=StubRegeneration(error=DiscordGatewayError("Discord said no"))
        )

        await command.callback(interaction)

        assert interaction.reply != ""

    async def test_anything_that_is_not_ours_is_left_to_the_handler(self) -> None:
        """The command's own table is for errors this project raises deliberately. A bug is not
        one, and swallowing it here would hide it behind a tidy sentence."""
        command, interaction, _ = run_it(service=StubRegeneration(error=RuntimeError("a bug")))

        with pytest.raises(RuntimeError, match="a bug"):
            await command.callback(interaction)


class Clock:
    """A monotonic clock a test moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TooLate(FakeResponse):
    """An interaction Discord has already dropped, as a defer that came too late finds it."""

    async def defer(self, *, ephemeral: bool = False, **_: object) -> None:
        raise discord.NotFound(MagicMock(status=404), "Unknown interaction")


class TestHowSoonItMayRunAgainInAThread:
    """Issue #185. One redraw per thread at a time, and the next five seconds after the last."""

    def taking_turns(self, service: StubRegeneration, clock: Clock):
        return build_regenerate_command(
            service, default_gate(), cooldown=Cooldown(REGENERATE_SPACING_SECONDS, clock=clock)
        )

    async def fire(self, command, *, channel_id: int | None = THREAD, who=None) -> FakeInteraction:
        interaction = FakeInteraction(user=who or developer(), channel_id=channel_id)
        await command.callback(interaction)
        return interaction

    async def test_the_same_thread_within_five_seconds_is_told_how_long_is_left(self) -> None:
        clock = Clock()
        service = StubRegeneration()
        command = self.taking_turns(service, clock)
        await self.fire(command)
        clock.now = 2.0

        interaction = await self.fire(command)

        assert interaction.said == (
            "/regenerate ran in this thread a moment ago. Try again in 3 seconds."
        )
        assert interaction.mark == OWED
        assert interaction.response.deferred is False
        assert service.calls == [THREAD]

    async def test_five_seconds_after_it_runs_again(self) -> None:
        clock = Clock()
        service = StubRegeneration()
        command = self.taking_turns(service, clock)
        await self.fire(command)
        clock.now = REGENERATE_SPACING_SECONDS

        await self.fire(command)

        assert service.calls == [THREAD, THREAD]

    async def test_the_wait_is_counted_from_when_the_redraw_finished(self) -> None:
        clock = Clock()

        class _Slow(StubRegeneration):
            async def regenerate(self, *, thread_id: int) -> RegenerateOutcome:
                clock.now += 30.0
                return await super().regenerate(thread_id=thread_id)

        command = self.taking_turns(_Slow(), clock)
        await self.fire(command)
        clock.now += 1.0

        interaction = await self.fire(command)

        assert interaction.said.endswith("Try again in 4 seconds.")

    async def test_another_thread_is_not_held_up(self) -> None:
        clock = Clock()
        service = StubRegeneration()
        command = self.taking_turns(service, clock)
        await self.fire(command, channel_id=THREAD)

        await self.fire(command, channel_id=THREAD + 1)

        assert service.calls == [THREAD, THREAD + 1]

    async def test_a_redraw_under_way_holds_the_next_ones_without_being_ended_by_them(
        self,
    ) -> None:
        clock = Clock()
        heard: list[FakeInteraction] = []
        outer = self

        class _Reentered(StubRegeneration):
            async def regenerate(self, *, thread_id: int) -> RegenerateOutcome:
                heard.append(await outer.fire(command))
                heard.append(await outer.fire(command))
                return await super().regenerate(thread_id=thread_id)

        command = self.taking_turns(_Reentered(), clock)

        await self.fire(command)

        running = (
            "/regenerate is already redrawing this thread. Try again a few seconds after it "
            "finishes."
        )
        assert [interaction.said for interaction in heard] == [running, running]

    async def test_somebody_refused_the_command_starts_no_wait(self) -> None:
        clock = Clock()
        service = StubRegeneration()
        command = self.taking_turns(service, clock)
        refused = await self.fire(command, who=member_with("Reviewer"))

        await self.fire(command)

        assert refused.mark == REFUSED
        assert service.calls == [THREAD]

    async def test_a_redraw_that_failed_starts_the_wait_all_the_same(self) -> None:
        clock = Clock()
        failing = StubRegeneration(error=SyncFailedError("could not be redrawn just now"))
        command = self.taking_turns(failing, clock)
        await self.fire(command)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/regenerate ran in this thread a moment ago.")

    async def test_a_redraw_that_went_wrong_unexpectedly_still_lets_go_of_the_thread(
        self,
    ) -> None:
        clock = Clock()
        command = self.taking_turns(StubRegeneration(error=RuntimeError("unpredicted")), clock)
        with pytest.raises(RuntimeError):
            await self.fire(command)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/regenerate ran in this thread a moment ago.")

    async def test_a_defer_that_came_too_late_still_lets_go_of_the_thread(self) -> None:
        """As `/refresh` does: a defer Discord refused still lets go of the thread."""
        clock = Clock()
        service = StubRegeneration()
        command = self.taking_turns(service, clock)
        dropped = FakeInteraction(user=developer(), channel_id=THREAD)
        dropped.response = _TooLate()
        with pytest.raises(discord.NotFound):
            await command.callback(dropped)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/regenerate ran in this thread a moment ago.")
        assert service.calls == [], "the redraw ran after a defer that failed"

    async def test_built_without_one_it_keeps_its_own(self) -> None:
        command, first, _ = run_it()
        await command.callback(first)
        second = FakeInteraction(user=developer(), channel_id=THREAD)

        await command.callback(second)

        assert second.mark == OWED

    def test_the_wait_is_five_seconds(self) -> None:
        assert REGENERATE_SPACING_SECONDS == 5
