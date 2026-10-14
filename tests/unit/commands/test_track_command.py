"""`/track`, as somebody in an item's thread meets it. Issue #163.

The exact wording is pinned, as `/mentions` pins its own: the answer is private, nobody else in the
thread can correct it, and a sentence that is subtly wrong about what the thread will be told is
worse than none. The service stands in for the database here; choosing against the database is
tested beside it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import discord
import pytest
from discord import app_commands

from shannon.commands.track import _COVERS, _STILL, NO_KIND, build_track_command
from shannon.discord_bot.responses import REFUSED, SUCCEEDED
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import ObjectType
from shannon.domain.errors import ShannonError
from shannon.domain.tracking import HAPPENS_TO, TrackedEvent, Tracking
from shannon.services.tracking import NOT_AN_ITEM_THREAD, ThreadLines
from shannon.services.workflow import NotAnItemThreadError
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
    administrator,
    default_gate,
    developer,
    member_with,
    project_manager,
)

pytestmark = pytest.mark.unit

THREAD = 4242
ALICE = 555


class StubTracking:
    """A thread's choices, held here rather than in the database, with what was asked."""

    def __init__(
        self,
        *,
        kind: ObjectType = ObjectType.PR,
        tracking: Tracking | None = None,
        error: ShannonError | None = None,
    ) -> None:
        self.kind = kind
        self.tracking = tracking or Tracking()
        self.error = error
        self.read: list[int] = []
        self.chosen: list[tuple[int, TrackedEvent, bool, int]] = []

    async def told(self, *, thread_id: int) -> ThreadLines:
        if self.error is not None:
            raise self.error
        self.read.append(thread_id)
        return ThreadLines(kind=self.kind, tracking=self.tracking)

    async def choose(
        self, *, thread_id: int, event: TrackedEvent, wanted: bool, by: int
    ) -> ThreadLines:
        if self.error is not None:
            raise self.error
        self.chosen.append((thread_id, event, wanted, by))
        if event not in HAPPENS_TO[self.kind]:
            return ThreadLines(kind=self.kind, tracking=self.tracking)
        was = self.tracking.wants(event)
        on = set(self.tracking.on) - {event}
        off = set(self.tracking.off) - {event}
        (on if wanted else off).add(event)
        self.tracking = Tracking(on=frozenset(on), off=frozenset(off))
        return ThreadLines(kind=self.kind, tracking=self.tracking, was=was)


async def fire(
    service: StubTracking,
    event: str | None = None,
    state: str | None = None,
    *,
    who: FakeMember | None = None,
    channel_id: int | None = THREAD,
    guild_id: int | None = 1,
) -> FakeInteraction:
    """Run the command, with discord.py's typing answered once rather than at every call.

    `app_commands.Command` declares its parameters as `...`, which pyright reads as a definite
    arity, and the interaction is a stand-in for the real one. One suppression with its reason
    beside it leaves this file gated.
    """
    caller = who or developer()
    caller.id = ALICE
    interaction = FakeInteraction(user=caller, channel_id=channel_id, guild_id=guild_id)
    command: SlashCommand = build_track_command(service, default_gate())
    picked = None if event is None else app_commands.Choice(name=event, value=event)
    turned = None if state is None else app_commands.Choice(name=state, value=state)
    theirs = cast(discord.Interaction, interaction)
    await command.callback(theirs, picked, turned)  # type: ignore[arg-type]  # pyright: ignore[reportCallIssue]
    return interaction


class TestWhoMayRunIt:
    @pytest.mark.parametrize(
        "caller", [developer, project_manager, administrator], ids=["dev", "pm", "admin"]
    )
    async def test_the_tier_every_thread_command_takes(
        self, caller: Callable[[], FakeMember]
    ) -> None:
        service = StubTracking()

        interaction = await fire(service, who=caller())

        assert interaction.reply.startswith("What this thread is told:")

    async def test_anybody_else_is_refused_before_anything_is_read(self) -> None:
        """It changes what everybody in the thread is told."""
        service = StubTracking()

        interaction = await fire(service, "commits", "on", who=member_with("Reviewer"))

        assert interaction.mark == REFUSED
        assert "You need one of these roles" in interaction.said
        assert (service.read, service.chosen) == ([], [])


class TestWhereItCanRun:
    async def test_outside_a_server_it_says_so(self) -> None:
        service = StubTracking()

        interaction = await fire(service, guild_id=None)

        assert interaction.said == "Run this inside a server channel."
        assert service.read == []

    async def test_with_no_channel_it_says_so(self) -> None:
        service = StubTracking()

        interaction = await fire(service, channel_id=None)

        assert interaction.said == "Run this inside the item's thread."
        assert service.read == []

    async def test_a_thread_that_is_no_items_is_refused_with_the_services_words(self) -> None:
        service = StubTracking(error=NotAnItemThreadError(NOT_AN_ITEM_THREAD))

        interaction = await fire(service)

        assert (interaction.mark, interaction.said) == (REFUSED, NOT_AN_ITEM_THREAD)

    async def test_anything_else_is_left_to_the_trees_handler(self) -> None:
        class Broken(StubTracking):
            async def told(self, *, thread_id: int) -> ThreadLines:
                raise RuntimeError("the database went away")

        with pytest.raises(RuntimeError):
            await fire(Broken())


class TestTheListing:
    async def test_a_pull_request_untouched_lists_every_kind_at_its_default(self) -> None:
        interaction = await fire(StubTracking())

        assert interaction.mark == ""
        assert interaction.reply == (
            "What this thread is told:\n"
            "- Comments and reviews: on (`comments`)\n"
            "- Labels, statuses and priorities: off (`labels`)\n"
            "- Commits and force pushes: off (`commits`)\n"
            "- CI results: on (`checks`)\n"
            "- Everybody having approved: on (`approvals`)\n"
            "- Leaving draft and going back into it: on (`draft`)\n"
            "- Review requests and assignments: on (`people`)\n"
            "- Closing, merging and reopening: on (`state`)\n"
            "-# Pick a kind, and on or off, to change one."
        )

    async def test_an_issue_lists_only_what_can_happen_to_one(self) -> None:
        interaction = await fire(StubTracking(kind=ObjectType.ISSUE))

        assert interaction.reply == (
            "What this thread is told:\n"
            "- Comments: on (`comments`)\n"
            "- Labels, statuses and priorities: off (`labels`)\n"
            "- Assignments: on (`people`)\n"
            "- Closing and reopening: on (`state`)\n"
            "-# Pick a kind, and on or off, to change one."
        )

    async def test_a_board_card_lists_its_fields_and_its_life(self) -> None:
        interaction = await fire(StubTracking(kind=ObjectType.TICKET))

        assert interaction.reply == (
            "What this thread is told:\n"
            "- The card's fields changing: off (`labels`)\n"
            "- The card being archived, restored, deleted or converted: on (`state`)\n"
            "-# Pick a kind, and on or off, to change one."
        )

    async def test_a_choice_shows_in_the_listing(self) -> None:
        service = StubTracking(tracking=Tracking(on=frozenset({TrackedEvent.COMMITS})))

        interaction = await fire(service)

        assert "- Commits and force pushes: on (`commits`)" in interaction.reply


class TestReadingOneKind:
    async def test_a_kind_without_on_or_off_says_where_it_stands(self) -> None:
        service = StubTracking()

        interaction = await fire(service, "commits")

        assert interaction.reply == "Lines about commits and force pushes are off in this thread."
        assert (service.read, service.chosen) == ([THREAD], [])

    async def test_a_kind_the_item_never_gets_is_refused_with_what_it_does(self) -> None:
        interaction = await fire(StubTracking(kind=ObjectType.ISSUE), "commits")

        assert interaction.mark == REFUSED
        assert interaction.said == (
            "An issue never gets that kind of line. Here you can pick `comments`, `labels`, "
            "`people` or `state`."
        )


class TestChoosing:
    async def test_turning_a_kind_on(self) -> None:
        service = StubTracking()

        interaction = await fire(service, "commits", "on")

        assert interaction.mark == SUCCEEDED
        assert interaction.said == (
            "Lines about commits and force pushes are on in this thread from now on."
        )
        assert service.chosen == [(THREAD, TrackedEvent.COMMITS, True, ALICE)]

    async def test_turning_a_kind_off(self) -> None:
        interaction = await fire(StubTracking(), "checks", "off")

        assert interaction.said == "Lines about CI results are off in this thread from now on."

    @pytest.mark.parametrize(
        ("kind", "covers"),
        [
            (ObjectType.PR, "closing, merging and reopening"),
            (ObjectType.ISSUE, "closing and reopening"),
        ],
    )
    async def test_turning_the_closing_headers_off_says_the_thread_still_shuts(
        self, kind: ObjectType, covers: str
    ) -> None:
        interaction = await fire(StubTracking(kind=kind), "state", "off")

        assert interaction.said == (
            f"Lines about {covers} are off in this thread from now on. The thread still shuts and "
            "opens again as it did, without a line saying so, unless this bot is not allowed to "
            "shut it."
        )

    async def test_a_cards_thread_is_promised_no_line_for_a_shut_it_was_refused(self) -> None:
        """Only an item's closing header shows a refused shut. A card's lines never do."""
        interaction = await fire(StubTracking(kind=ObjectType.TICKET), "state", "off")

        assert interaction.said == (
            "Lines about the card being archived, restored, deleted or converted are off in this "
            "thread from now on. The thread still shuts and opens again as it did, without a line "
            "saying so."
        )

    @pytest.mark.parametrize(
        ("kind", "covers"),
        [(ObjectType.PR, "review requests and assignments"), (ObjectType.ISSUE, "assignments")],
    )
    async def test_turning_people_off_says_nobody_is_rung_for_it_later(
        self, kind: ObjectType, covers: str
    ) -> None:
        interaction = await fire(StubTracking(kind=kind), "people", "off")

        assert interaction.said == (
            f"Lines about {covers} are off in this thread from now on. Nobody put on the item "
            "while this is off is rung for it later."
        )

    async def test_a_kind_already_so_says_so(self) -> None:
        interaction = await fire(StubTracking(), "comments", "on")

        assert (interaction.mark, interaction.said) == (
            SUCCEEDED,
            "Lines about comments and reviews were already on in this thread.",
        )

    async def test_a_kind_the_item_never_gets_is_refused(self) -> None:
        interaction = await fire(StubTracking(kind=ObjectType.TICKET), "commits", "on")

        assert interaction.said == (
            "A board card never gets that kind of line. Here you can pick `labels` or `state`."
        )

    async def test_on_or_off_with_no_kind_is_refused_before_anything_is_asked(self) -> None:
        service = StubTracking()

        interaction = await fire(service, state="on")

        assert (interaction.mark, interaction.said) == (REFUSED, NO_KIND)
        assert (service.read, service.chosen) == ([], [])


class TestHowItAnswers:
    @pytest.mark.parametrize(
        ("event", "state"),
        [(None, None), ("labels", None), ("labels", "on"), (None, "on")],
        ids=["list", "read one", "choose", "refused"],
    )
    async def test_every_answer_is_private(self, event: str | None, state: str | None) -> None:
        """The thread is not told when its lines change; only whoever changed them is."""
        interaction = await fire(StubTracking(), event, state)

        assert interaction.ephemerally == [True]


class TestWhatDiscordIsTold:
    def test_the_kinds_offered_are_the_kinds_there_are(self) -> None:
        command = build_track_command(StubTracking(), default_gate())
        told = {each.name: each for each in command.parameters}

        assert [choice.value for choice in told["event"].choices] == [
            event.value for event in TrackedEvent
        ]
        assert [choice.value for choice in told["state"].choices] == ["on", "off"]
        assert (told["event"].required, told["state"].required) == (False, False)

    @pytest.mark.parametrize("kind", list(ObjectType))
    def test_every_kind_an_item_can_get_has_words(self, kind: ObjectType) -> None:
        assert tuple(_COVERS[kind]) == HAPPENS_TO[kind]

    @pytest.mark.parametrize("kind", list(ObjectType))
    def test_what_still_happens_is_only_said_of_a_kind_the_item_can_get(
        self, kind: ObjectType
    ) -> None:
        assert set(_STILL[kind]) <= set(HAPPENS_TO[kind])
