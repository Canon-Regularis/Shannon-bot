"""`/remind`, as the person running it meets it. Issue #229.

What is worth pinning is who may remind whom, and everything refused before a reminder is written
down: a reminder goes off later, where it was asked for, with nobody there to tell if it cannot.
Writing one down and sending it are tested against the database; this stands a fake in for the
writing and watches what the command decides.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import MagicMock

import discord
import pytest

from shannon.commands._permissions import REGISTER_ROLES
from shannon.commands.remind import A_BOT, NO_TIME, YOU_CANNOT_POST, build_remind_command
from shannon.db.models import REMINDER_MESSAGE_WIDTH
from shannon.discord_bot.responses import OWED, REFUSED, SUCCEEDED
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.errors import ShannonError
from shannon.services.reminders.book import TooManyRemindersError
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

ALICE = 555
BOB = 777
DUE = datetime(2026, 10, 8, 15, 30, tzinfo=UTC)
WHEN = f"<t:{int(DUE.timestamp())}:R>, on <t:{int(DUE.timestamp())}:f>"


class FakeReminders:
    """Writing a reminder down, recorded rather than done."""

    def __init__(self, *, refusing: ShannonError | None = None) -> None:
        self.refusing = refusing
        self.added: list[dict[str, object]] = []
        # The interaction a test is watching, and whether it had been answered by the time each
        # reminder was written: Discord gives a command three seconds, and a write is a round trip.
        self.watching: FakeInteraction | None = None
        self.answered_before_writing: list[bool] = []

    async def add(
        self,
        *,
        guild_id: int,
        channel_id: int,
        member_id: int,
        set_by: int,
        after: timedelta,
        message: str | None,
    ) -> datetime:
        if self.watching is not None:
            self.answered_before_writing.append(self.watching.response.is_done())
        if self.refusing is not None:
            raise self.refusing
        self.added.append(
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
                "member_id": member_id,
                "set_by": set_by,
                "after": after,
                "message": message,
            }
        )
        return DUE


async def fire(
    command: SlashCommand,
    interaction: FakeInteraction,
    member: FakeMember,
    *,
    days: int = 0,
    hours: int = 0,
    minutes: int = 0,
    message: str | None = None,
) -> None:
    """Run the command, with discord.py's typing answered once rather than at every call.

    The interaction and the member are stand-ins, so they are cast to the real ones, and
    `app_commands.Command` declares its parameters as `...`, which pyright reads as a definite
    arity. One suppression with its reason beside it leaves this file gated.
    """
    theirs = cast(discord.Interaction, interaction)
    them = cast(discord.Member, member)
    await command.callback(theirs, them, days, hours, minutes, message)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]


def person(
    member_id: int, *, bot: bool = False, sees: discord.Permissions | None = None
) -> FakeMember:
    """Somebody named in the command, with what Discord resolved for them in this channel."""
    return FakeMember(id=member_id, bot=bot, resolved_permissions=sees)


def ran_by(caller: FakeMember | None = None) -> FakeInteraction:
    """The command run by ALICE, a developer unless said otherwise."""
    who = caller or developer()
    who.id = ALICE
    return FakeInteraction(user=who)


def command(reminders: FakeReminders | None = None) -> tuple[SlashCommand, FakeReminders]:
    reminders = reminders or FakeReminders()
    return build_remind_command(reminders, default_gate()), reminders


def a_thread(*, locked: bool = False) -> MagicMock:
    """A thread the command was run in. `locked` said outright, because a bare mock's is truthy."""
    thread = MagicMock(spec=discord.Thread)
    thread.locked = locked
    return thread


class TestRemindingYourself:
    async def test_anybody_may_with_no_role_at_all(self) -> None:
        remind, reminders = command()
        interaction = ran_by(FakeMember())

        await fire(remind, interaction, person(ALICE), hours=3, message="look at the river")

        assert interaction.mark == SUCCEEDED
        assert interaction.said == f"This bot will remind you here {WHEN}."
        assert reminders.added == [
            {
                "guild_id": 1,
                "channel_id": 10,
                "member_id": ALICE,
                "set_by": ALICE,
                "after": timedelta(hours=3),
                "message": "look at the river",
            }
        ]

    async def test_with_nothing_to_say_it_says_nothing(self) -> None:
        remind, reminders = command()

        await fire(remind, ran_by(), person(ALICE), minutes=1)

        assert reminders.added[0]["message"] is None

    async def test_the_time_given_is_added_up_exactly(self) -> None:
        remind, reminders = command()

        await fire(remind, ran_by(), person(ALICE), days=365, hours=23, minutes=59)

        assert reminders.added[0]["after"] == timedelta(days=365, hours=23, minutes=59)

    @pytest.mark.parametrize(
        ("days", "hours", "minutes", "after"),
        [
            (1, 0, 0, timedelta(days=1)),
            (0, 1, 0, timedelta(hours=1)),
            (0, 0, 1, timedelta(minutes=1)),
        ],
        ids=["days", "hours", "minutes"],
    )
    async def test_any_one_of_the_three_is_enough(
        self, days: int, hours: int, minutes: int, after: timedelta
    ) -> None:
        remind, reminders = command()

        await fire(remind, ran_by(), person(ALICE), days=days, hours=hours, minutes=minutes)

        assert reminders.added[0]["after"] == after


class TestRemindingSomebodyElse:
    async def test_a_developer_is_refused_and_nothing_is_written(self) -> None:
        """A bot that pings anybody on anybody's say-so is a spam tool."""
        remind, reminders = command()
        interaction = ran_by(developer())

        await fire(remind, interaction, person(BOB), hours=1)

        assert interaction.mark == REFUSED
        assert interaction.said == (
            f"{default_gate().denial('remind', REGISTER_ROLES)} Reminding yourself needs no role."
        )
        assert reminders.added == []

    @pytest.mark.parametrize("caller", [project_manager, administrator], ids=["pm", "admin"])
    async def test_the_tier_that_speaks_for_the_server_may(
        self, caller: Callable[[], FakeMember]
    ) -> None:
        remind, reminders = command()
        interaction = ran_by(caller())

        await fire(remind, interaction, person(BOB), hours=1)

        assert interaction.mark == SUCCEEDED
        assert interaction.said == f"This bot will remind <@{BOB}> here {WHEN}."
        assert (reminders.added[0]["member_id"], reminders.added[0]["set_by"]) == (BOB, ALICE)


class TestWhatCouldNeverArrive:
    async def test_a_bot_cannot_be_reminded(self) -> None:
        remind, reminders = command()
        interaction = ran_by(project_manager())

        await fire(remind, interaction, person(BOB, bot=True), hours=1)

        assert (interaction.mark, interaction.said) == (REFUSED, A_BOT)
        assert reminders.added == []

    async def test_the_role_is_asked_about_before_whether_it_is_a_bot(self) -> None:
        """Somebody refused the half they may not use is told about the role, not the target."""
        remind, _ = command()
        interaction = ran_by(developer())

        await fire(remind, interaction, person(BOB, bot=True), hours=1)

        assert interaction.said.endswith("Reminding yourself needs no role.")

    async def test_somebody_who_cannot_see_the_channel_is_refused(self) -> None:
        remind, reminders = command()
        interaction = ran_by(project_manager())
        blind = person(BOB, sees=discord.Permissions(send_messages=True))

        await fire(remind, interaction, blind, hours=1)

        assert interaction.said == (
            f"A reminder here would never reach <@{BOB}>, who cannot see this channel."
        )
        assert reminders.added == []

    async def test_somebody_who_can_see_it_is_not(self) -> None:
        remind, reminders = command()
        seeing = person(BOB, sees=discord.Permissions(view_channel=True))

        await fire(remind, ran_by(project_manager()), seeing, hours=1)

        assert len(reminders.added) == 1

    async def test_nothing_resolved_for_them_is_not_guessed_at(self) -> None:
        """Discord sends what a member may do with the command. Where it sent nothing, no answer is
        made up in either direction."""
        remind, reminders = command()

        await fire(remind, ran_by(project_manager()), person(BOB), hours=1)

        assert len(reminders.added) == 1

    async def test_no_time_at_all_is_refused(self) -> None:
        remind, reminders = command()
        interaction = ran_by()

        await fire(remind, interaction, person(ALICE))

        assert (interaction.mark, interaction.said) == (REFUSED, NO_TIME)
        assert reminders.added == []


class TestWhereItWouldGoOff:
    async def test_a_caller_who_cannot_post_here_is_refused(self) -> None:
        """Or anybody could have this bot post their words into a read-only channel."""
        remind, reminders = command()
        interaction = ran_by()
        interaction.permissions = discord.Permissions(view_channel=True)

        await fire(remind, interaction, person(ALICE), hours=1)

        assert (interaction.mark, interaction.said) == (REFUSED, YOU_CANNOT_POST)
        assert reminders.added == []

    async def test_a_bot_that_cannot_post_here_says_what_it_lacks(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.app_permissions = discord.Permissions(view_channel=True)

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.said == (
            "This bot cannot post in this channel, so the reminder would never go off. It has "
            "not been given Send Messages here."
        )
        assert reminders.added == []

    async def test_everything_the_bot_lacks_is_named(self) -> None:
        remind, _ = command()
        interaction = ran_by()
        interaction.app_permissions = discord.Permissions.none()

        await fire(remind, interaction, person(ALICE), hours=1)

        assert "given View Channel or Send Messages here." in interaction.said

    async def test_in_a_thread_it_is_sending_in_threads_that_counts(self) -> None:
        """Discord keeps the two apart, and a channel can allow either without the other."""
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread()
        interaction.app_permissions = discord.Permissions(view_channel=True, send_messages=True)

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.said.endswith("It has not been given Send Messages in Threads here.")
        assert reminders.added == []

    async def test_in_a_thread_sending_in_threads_is_enough(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread()
        only_threads = discord.Permissions(view_channel=True, send_messages_in_threads=True)
        interaction.permissions = only_threads
        interaction.app_permissions = only_threads

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.mark == SUCCEEDED
        assert len(reminders.added) == 1

    async def test_a_caller_who_cannot_post_in_a_thread_is_refused_there(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread()
        interaction.permissions = discord.Permissions(view_channel=True, send_messages=True)

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.said == YOU_CANNOT_POST
        assert reminders.added == []


class TestALockedThread:
    """Found reviewing #229. Nobody can post in a locked thread to keep it from archiving, so by
    the time a reminder goes off there it has to be reopened, and reopening a locked thread takes
    Manage Threads. Accepted without it, the reminder would be refused then, with nobody to tell.
    """

    THREADS = discord.Permissions(view_channel=True, send_messages_in_threads=True)
    MODERATING = discord.Permissions(
        view_channel=True, send_messages_in_threads=True, manage_threads=True
    )

    async def test_a_bot_that_cannot_reopen_it_is_refused_by_name(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread(locked=True)
        interaction.permissions = self.MODERATING
        interaction.app_permissions = self.THREADS

        await fire(remind, interaction, person(ALICE), days=2)

        assert interaction.said == (
            "This bot cannot post in this channel, so the reminder would never go off. It has "
            "not been given Manage Threads here."
        )
        assert reminders.added == []

    async def test_a_bot_that_can_is_fine(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread(locked=True)
        interaction.permissions = self.MODERATING
        interaction.app_permissions = self.MODERATING

        await fire(remind, interaction, person(ALICE), days=2)

        assert interaction.mark == SUCCEEDED
        assert len(reminders.added) == 1

    async def test_a_caller_who_could_not_post_in_it_is_refused(self) -> None:
        """Only somebody who may manage threads can post in a locked one."""
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel = a_thread(locked=True)
        interaction.permissions = self.THREADS
        interaction.app_permissions = self.MODERATING

        await fire(remind, interaction, person(ALICE), days=2)

        assert (interaction.mark, interaction.said) == (REFUSED, YOU_CANNOT_POST)
        assert reminders.added == []

    async def test_three_things_missing_read_as_a_sentence(self) -> None:
        remind, _ = command()
        interaction = ran_by()
        interaction.channel = a_thread(locked=True)
        interaction.permissions = self.MODERATING
        interaction.app_permissions = discord.Permissions.none()

        await fire(remind, interaction, person(ALICE), days=2)

        assert interaction.said.endswith(
            "It has not been given View Channel, Send Messages in Threads or Manage Threads here."
        )


class TestHowItAnswers:
    async def test_outside_a_server_it_says_so(self) -> None:
        """`guild_only` keeps it out of a direct message; this is what happens if that ever goes."""
        remind, reminders = command()
        interaction = ran_by()
        interaction.guild_id = None

        await fire(remind, interaction, person(ALICE), hours=1)

        assert (interaction.mark, interaction.said) == (
            REFUSED,
            "Run this inside a server channel.",
        )
        assert reminders.added == []

    async def test_with_no_channel_to_go_off_in_it_says_so(self) -> None:
        remind, reminders = command()
        interaction = ran_by()
        interaction.channel_id = None

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.mark == REFUSED
        assert reminders.added == []

    async def test_a_full_book_is_amber_because_it_comes_right(self) -> None:
        """A reminder going off makes room for the next one."""
        full = TooManyRemindersError("You already have 25 reminders waiting in this server.")
        remind, _ = command(FakeReminders(refusing=full))
        interaction = ran_by()

        await fire(remind, interaction, person(ALICE), hours=1)

        assert interaction.mark == OWED
        assert interaction.said == "You already have 25 reminders waiting in this server."

    async def test_it_defers_before_anything_is_written(self) -> None:
        """Discord drops a command left unanswered for three seconds, and the write is a round trip
        to the database. The order is the point, so it is what is checked."""
        remind, reminders = command()
        interaction = ran_by()
        reminders.watching = interaction

        await fire(remind, interaction, person(ALICE), hours=1)

        assert reminders.answered_before_writing == [True]
        assert interaction.response.deferred_ephemerally is True

    @pytest.mark.parametrize("minutes", [0, 1], ids=["refused", "set"])
    async def test_every_answer_is_private(self, minutes: int) -> None:
        """The reminder itself is public when it goes off. Setting one tells nobody else."""
        remind, _ = command()
        interaction = ran_by()

        await fire(remind, interaction, person(ALICE), minutes=minutes)

        assert interaction.ephemerally == [True]


class TestWhatDiscordIsTold:
    """The limits live in the declaration, where Discord holds somebody to them as they type."""

    def test_only_who_is_required(self) -> None:
        remind, _ = command()

        assert [(each.name, each.required) for each in remind.parameters] == [
            ("member", True),
            ("days", False),
            ("hours", False),
            ("minutes", False),
            ("message", False),
        ]

    @pytest.mark.parametrize(("name", "most"), [("days", 365), ("hours", 23), ("minutes", 59)])
    def test_each_count_of_time_has_its_range(self, name: str, most: int) -> None:
        remind, _ = command()
        told = {each.name: each for each in remind.parameters}[name]

        assert told.type is discord.AppCommandOptionType.integer
        assert (told.min_value, told.max_value) == (0, most)

    def test_the_message_is_cut_where_the_column_is(self) -> None:
        """Sent to Discord as a string's longest length, so nobody can type past it."""
        remind, _ = command()
        told = {each.name: each for each in remind.parameters}["message"]

        assert told.type is discord.AppCommandOptionType.string
        assert told.max_value == REMINDER_MESSAGE_WIDTH == 500

    def test_who_is_a_member_of_the_server(self) -> None:
        remind, _ = command()
        told = {each.name: each for each in remind.parameters}["member"]

        assert told.type is discord.AppCommandOptionType.user
