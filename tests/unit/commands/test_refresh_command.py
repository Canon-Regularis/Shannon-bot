"""What `/refresh` says, and who may run it. Issue #74.

The counts are the whole reply, so the wording is asserted rather than sampled: the difference
between "done" and "run it again" is one clause, and somebody acts on it.

Issue #185 added the redraws to the reply, which is `TestWhatItSaysAboutRedrawing`, and a minute
between runs in a server, which is `TestHowSoonItMayRunAgain`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import discord
import pytest
from discord import app_commands

from shannon.commands._cooldowns import Cooldown
from shannon.commands.refresh import (
    _KINDS,
    _MISSED,
    REFRESH_SPACING_SECONDS,
    build_refresh_command,
)
from shannon.discord_bot.responses import OWED, REFUSED
from shannon.domain.errors import NotRegisteredError
from shannon.github.errors import GitHubRateLimitError
from shannon.services.sync.refresh import MissedTickets, RefreshOutcome, RefreshScope
from tests.fakes.discord_objects import FakeInteraction, FakeMember, FakeResponse
from tests.unit.commands.conftest import administrator, default_gate, developer, member_with

pytestmark = pytest.mark.unit

FULL_NAME = "Canon-Regularis/Shannon-bot"


def an_outcome(**overrides) -> RefreshOutcome:
    fields = {"full_name": FULL_NAME, "mirrored": 0, "already": 0, "failed": 0, "left": 0}
    return RefreshOutcome(**{**fields, **overrides})


class StubRefresh:
    def __init__(self, *, outcome: RefreshOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome if outcome is not None else an_outcome(mirrored=1)
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
        self.calls.append({"guild_id": guild_id, "scope": scope})
        if self.error is not None:
            raise self.error
        return self.outcome


def a_command(service: StubRefresh | None = None) -> app_commands.Command:
    return build_refresh_command(service if service is not None else StubRefresh(), default_gate())


def the_scope_option(command: app_commands.Command) -> app_commands.Parameter:
    """The option a person picks a scope from, found by the name Discord shows.

    By name rather than by position, so renaming it back to something the picker cannot honestly
    say raises here instead of quietly testing a command nobody is offered.
    """
    return next(p for p in command.parameters if p.display_name == "scope")


async def run(
    service: StubRefresh,
    member: FakeMember | None = None,
    *,
    scope: RefreshScope | None = None,
    guild_id: int | None = 1,
) -> FakeInteraction:
    """Drive the callback, taking the choice out of the command rather than inventing one.

    This used to build `Choice(name=only, value=only)` by hand, which is a shape Discord could
    never send: the real dispatch matches an incoming value against the declared list and refuses
    anything absent from it, so a fabricated choice asserts a property of `RefreshScope(str)`
    rather than a property of this command. Nothing here had ever touched the list the picker is
    actually built from, which is the one thing issue #92 is about.

    Read off the built command rather than off `_CHOICES`, because deleting the decorator that
    binds them raises nothing at all: the constant stays correct and the option silently becomes
    a free-text box.
    """
    interaction = FakeInteraction(
        guild_id=guild_id, channel_id=99, user=member if member is not None else developer()
    )
    command = a_command(service)
    choice = None
    if scope is not None:
        choice = next(c for c in the_scope_option(command).choices if c.value == scope.value)
    await command.callback(interaction, choice)
    return interaction


class TestWhatItSays:
    async def test_a_repository_with_nothing_open_says_so(self) -> None:
        service = StubRefresh(outcome=an_outcome())

        interaction = await run(service)

        assert interaction.said == (
            f"{FULL_NAME} has no open items right now, so there was nothing to mirror."
        )

    async def test_no_board_says_which_command_links_one(self) -> None:
        """The counts AND the note, because a run that mirrored real work must not report itself as
        a failure just because one of its three kinds was out of reach."""
        service = StubRefresh(outcome=an_outcome(mirrored=3, tickets_missed=MissedTickets.NO_BOARD))

        interaction = await run(service)

        assert f"Mirrored 3 open items from {FULL_NAME}" in interaction.said
        assert "No board is linked to this server, so tickets were not covered." in interaction.said
        assert "/board link" in interaction.said

    async def test_no_ticket_channel_says_which_command_maps_one(self) -> None:
        """A different command to run, which is why this is not one flag with one sentence: a board
        is linked, so `/board link` would be the wrong advice."""
        service = StubRefresh(
            outcome=an_outcome(mirrored=1, tickets_missed=MissedTickets.NO_CHANNEL)
        )

        interaction = await run(service)

        assert "/set_channel" in interaction.said
        assert "/board" not in interaction.said

    async def test_a_board_that_would_not_read_says_where_to_find_out_why(self) -> None:
        """`/board show` for whose authorisation it is read with, which is the commonest cause,
        and the log for the rest: this reply cannot tell a 404 from a 403 from a spent quota.
        No command to run, because nothing here can be run to fix it from the reply."""
        service = StubRefresh(outcome=an_outcome(tickets_missed=MissedTickets.UNREADABLE))

        interaction = await run(service)

        assert "The board could not be read, so tickets were not covered." in interaction.said
        assert "/board show" in interaction.said
        assert "Run /" not in interaction.said

    async def test_the_note_is_said_even_when_there_was_nothing_to_mirror(self) -> None:
        """The case that made this a wrapper rather than one more clause.

        `_counts` has four exits and three of them are the nothing-to-do sentences, which return
        early. `/refresh all` on a quiet repository with no board takes one of those - and that is
        the run where the reason matters most, because no number moved and the only useful thing to
        say is why.
        """
        service = StubRefresh(outcome=an_outcome(tickets_missed=MissedTickets.NO_BOARD))

        interaction = await run(service)

        assert "has no open items right now" in interaction.said
        assert "/board link" in interaction.said

    async def test_nothing_is_added_when_tickets_were_covered(self) -> None:
        """The other arm. A run with nothing to report about tickets must say nothing about them,
        including on `/refresh pull requests`, which never asks about them at all."""
        service = StubRefresh(outcome=an_outcome(mirrored=2))

        interaction = await run(service)

        assert "ticket" not in interaction.said
        assert "/set_" not in interaction.said

    async def test_everything_already_mirrored_says_nothing_to_do(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=43))

        interaction = await run(service)

        assert interaction.said == (
            f"Nothing to mirror. All 43 open items on {FULL_NAME} already have a thread."
        )

    async def test_one_already_mirrored_reads_as_one(self) -> None:
        """ "All 1 open items ... already have a thread" was reachable, and is three disagreements
        in one sentence (#147)."""
        service = StubRefresh(outcome=an_outcome(already=1))

        interaction = await run(service)

        assert interaction.said == (
            f"Nothing to mirror. The one open item on {FULL_NAME} already has a thread."
        )

    async def test_one_mirrored_reads_as_one(self) -> None:
        service = StubRefresh(outcome=an_outcome(mirrored=1, already=0))

        interaction = await run(service)

        assert interaction.said.startswith(f"Mirrored 1 open item from {FULL_NAME}.")

    async def test_one_that_failed_agrees_with_its_verb(self) -> None:
        """`/set_channel` computed this and `/refresh` hardcoded "are", so a single failure read
        "1 could not be mirrored ... and are among those"."""
        service = StubRefresh(outcome=an_outcome(mirrored=2, already=0, failed=1, left=1))

        interaction = await run(service)

        assert "1 could not be mirrored just now and is among those" in interaction.said

    async def test_a_clean_run_counts_both_halves_and_says_nobody_was_pinged(self) -> None:
        service = StubRefresh(outcome=an_outcome(mirrored=7, already=31, redrawn=31))

        interaction = await run(service)

        assert interaction.said == (
            f"Mirrored 7 open items from {FULL_NAME}. Redrew 31 open items from GitHub. Nobody "
            "was pinged."
        )

    async def test_a_run_that_left_some_says_to_run_it_again(self) -> None:
        """The cap. Without this sentence somebody reads a partial run as a finished one."""
        service = StubRefresh(outcome=an_outcome(mirrored=25, already=31, left=18))

        interaction = await run(service)

        assert "18 are still untracked, so run /refresh again to carry on." in interaction.reply

    async def test_one_left_over_is_not_told_it_are_still_untracked(self) -> None:
        service = StubRefresh(outcome=an_outcome(mirrored=2, already=0, left=1))

        interaction = await run(service)

        assert "1 is still untracked" in interaction.reply

    async def test_failures_are_named_as_part_of_what_is_left(self) -> None:
        """Inside `left` rather than beside it, or the two read as adding up to more work than
        there is."""
        service = StubRefresh(outcome=an_outcome(mirrored=4, already=2, failed=3, left=3))

        interaction = await run(service)

        assert "3 are still untracked" in interaction.reply
        assert "3 could not be mirrored just now and are among those still untracked" in (
            interaction.reply
        )

    async def test_nobody_was_pinged_is_not_said_when_nothing_happened(self) -> None:
        """It answers a question only somebody who just mirrored a backlog is asking."""
        service = StubRefresh(outcome=an_outcome(already=5))

        interaction = await run(service)

        assert "Nobody was pinged" not in interaction.reply


class TestWhatItSaysAboutRedrawing:
    """Issue #185. Every open pull request and issue with a thread is redrawn now, and the reply
    says what became of them. Each part appears only when its count is above nought."""

    async def test_a_repository_with_nothing_to_open_still_has_its_threads_redrawn(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=12, redrawn=12))

        interaction = await run(service)

        assert interaction.said == (
            f"Nothing to mirror. All 12 open items on {FULL_NAME} already have a thread. Redrew "
            "12 open items from GitHub. Nobody was pinged."
        )

    async def test_one_redrawn_reads_as_one(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=1, redrawn=1))

        interaction = await run(service)

        assert interaction.said == (
            f"Nothing to mirror. The one open item on {FULL_NAME} already has a thread. Redrew 1 "
            "open item from GitHub. Nobody was pinged."
        )

    async def test_a_run_short_of_time_says_what_is_left_of_both_halves(self) -> None:
        service = StubRefresh(
            outcome=an_outcome(mirrored=25, left=18, already=27, redrawn=20, unreached=7)
        )

        interaction = await run(service)

        assert interaction.said == (
            f"Mirrored 25 open items from {FULL_NAME}. 18 are still untracked, so run /refresh "
            "again to carry on. Redrew 20 open items from GitHub. 7 open items are still to be "
            "redrawn; the next /refresh with the same scope starts with them. Nobody was pinged."
        )

    async def test_one_not_reached_reads_as_one(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=3, redrawn=2, unreached=1))

        interaction = await run(service)

        assert (
            " 1 open item is still to be redrawn; the next /refresh with the same scope starts "
            "with it." in interaction.said
        )

    async def test_a_thread_that_had_gone_is_said_to_have_a_new_one(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=2, redrawn=1, rebuilt=1))

        interaction = await run(service)

        assert " 1 open item had lost its thread, so it has a new one." in interaction.said

    async def test_several_that_had_gone_agree_with_their_verb(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=3, rebuilt=3))

        interaction = await run(service)

        assert " 3 open items had lost their threads, so they have new ones." in interaction.said

    async def test_a_run_that_only_rebuilt_still_says_nobody_was_pinged(self) -> None:
        """A new thread is a posted block, which is exactly where pinging is the risk."""
        service = StubRefresh(outcome=an_outcome(already=1, rebuilt=1))

        interaction = await run(service)

        assert interaction.said.endswith("Nobody was pinged.")

    async def test_redraws_that_failed_are_counted(self) -> None:
        one = await run(StubRefresh(outcome=an_outcome(already=2, redrawn=1, redraws_failed=1)))
        two = await run(StubRefresh(outcome=an_outcome(already=3, redrawn=1, redraws_failed=2)))

        assert " 1 open item could not be redrawn just now; the log says why." in one.said
        assert " 2 open items could not be redrawn just now; the log says why." in two.said

    async def test_a_run_where_every_redraw_failed_says_nothing_about_pinging(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=2, redraws_failed=2))

        interaction = await run(service)

        assert "Nobody was pinged" not in interaction.said

    async def test_the_kind_asked_for_names_what_was_redrawn(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=2, redrawn=2))

        interaction = await run(service, scope=RefreshScope.PULL_REQUESTS)

        assert " Redrew 2 open pull requests from GitHub." in interaction.said

    async def test_tickets_that_already_have_a_thread_are_said_to_be_the_boards(self) -> None:
        """No number, so the same words read right under `all` and under `tickets`."""
        service = StubRefresh(outcome=an_outcome(already=3, left_to_the_board=3))

        interaction = await run(service, scope=RefreshScope.TICKETS)

        assert interaction.said == (
            f"Nothing to mirror. All 3 tickets on {FULL_NAME} already have a thread. Tickets that "
            "already have a thread are left alone, because the board keeps those up to date "
            "itself."
        )

    async def test_the_note_about_tickets_still_comes_last(self) -> None:
        service = StubRefresh(
            outcome=an_outcome(already=2, redrawn=2, tickets_missed=MissedTickets.NO_BOARD)
        )

        interaction = await run(service)

        assert interaction.said.endswith(
            f"Redrew 2 open items from GitHub. Nobody was pinged. {_MISSED[MissedTickets.NO_BOARD]}"
        )


class Clock:
    """A monotonic clock a test moves."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Busy(StubRefresh):
    """Takes the clock forward while it runs, the way a long run does."""

    def __init__(self, clock: Clock, *, takes: float) -> None:
        super().__init__()
        self._clock = clock
        self._takes = takes

    async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
        self._clock.now += self._takes
        return await super().refresh(guild_id=guild_id, scope=scope)


class _TooLate(FakeResponse):
    """An interaction Discord has already dropped, as a defer that came too late finds it."""

    async def defer(self, *, ephemeral: bool = False, **_: object) -> None:
        raise discord.NotFound(MagicMock(status=404), "Unknown interaction")


class TestHowSoonItMayRunAgain:
    """Issue #185. One run per server at a time, and the next a minute after the last finished."""

    def taking_turns(self, service: StubRefresh, clock: Clock) -> app_commands.Command:
        return build_refresh_command(
            service, default_gate(), cooldown=Cooldown(REFRESH_SPACING_SECONDS, clock=clock)
        )

    async def fire(
        self,
        command: app_commands.Command,
        member: FakeMember | None = None,
        *,
        guild_id: int = 1,
    ) -> FakeInteraction:
        interaction = FakeInteraction(
            guild_id=guild_id, channel_id=99, user=member if member is not None else developer()
        )
        await command.callback(interaction, None)
        return interaction

    async def test_a_second_run_inside_the_minute_is_told_how_long_is_left(self) -> None:
        clock = Clock()
        service = StubRefresh()
        command = self.taking_turns(service, clock)
        await self.fire(command)
        clock.now = 15.0

        interaction = await self.fire(command)

        assert interaction.said == (
            "/refresh ran in this server less than a minute ago. Try again in 45 seconds."
        )
        assert interaction.mark == OWED
        assert interaction.response.deferred is False
        assert len(service.calls) == 1

    async def test_half_a_second_left_is_one_second_rather_than_none(self) -> None:
        clock = Clock()
        command = self.taking_turns(StubRefresh(), clock)
        await self.fire(command)
        clock.now = 59.5

        interaction = await self.fire(command)

        assert interaction.said.endswith("Try again in 1 second.")

    async def test_a_minute_after_it_runs_again(self) -> None:
        clock = Clock()
        service = StubRefresh()
        command = self.taking_turns(service, clock)
        await self.fire(command)
        clock.now = 60.0

        await self.fire(command)

        assert len(service.calls) == 2

    async def test_the_minute_is_counted_from_when_the_run_finished(self) -> None:
        """A run of five minutes, then ten seconds: fifty left, not none."""
        clock = Clock()
        command = self.taking_turns(_Busy(clock, takes=300.0), clock)
        await self.fire(command)
        clock.now += 10.0

        interaction = await self.fire(command)

        assert interaction.said.endswith("Try again in 50 seconds.")

    async def test_a_run_under_way_holds_the_next_ones_without_being_ended_by_them(self) -> None:
        """Two asked for during the run, and both are held: the first refusal did not let go of
        the run it was refused for, or the second would have been told to wait instead."""
        clock = Clock()
        heard: list[FakeInteraction] = []

        class _Reentered(StubRefresh):
            async def refresh(self, *, guild_id: int, scope: RefreshScope) -> RefreshOutcome:
                heard.append(await TestHowSoonItMayRunAgain().fire(command))
                heard.append(await TestHowSoonItMayRunAgain().fire(command))
                return await super().refresh(guild_id=guild_id, scope=scope)

        command = self.taking_turns(_Reentered(), clock)

        await self.fire(command)

        running = (
            "/refresh is already running in this server. Try again a minute after it finishes."
        )
        assert [interaction.said for interaction in heard] == [running, running]
        assert {interaction.mark for interaction in heard} == {OWED}

    async def test_another_server_is_not_held_up(self) -> None:
        clock = Clock()
        service = StubRefresh()
        command = self.taking_turns(service, clock)
        await self.fire(command, guild_id=1)

        await self.fire(command, guild_id=2)

        assert [call["guild_id"] for call in service.calls] == [1, 2]

    async def test_an_administrator_waits_too(self) -> None:
        clock = Clock()
        command = self.taking_turns(StubRefresh(), clock)
        await self.fire(command, administrator())

        interaction = await self.fire(command, administrator())

        assert interaction.mark == OWED

    async def test_somebody_refused_the_command_starts_no_wait(self) -> None:
        clock = Clock()
        service = StubRefresh()
        command = self.taking_turns(service, clock)
        refused = await self.fire(command, member_with("Reviewer"))

        await self.fire(command)

        assert refused.mark == REFUSED
        assert len(service.calls) == 1

    async def test_a_run_that_failed_starts_the_wait_all_the_same(self) -> None:
        """What it spent is spent, and a refusal somebody fixes in seconds costs them a minute."""
        clock = Clock()
        command = self.taking_turns(
            StubRefresh(error=NotRegisteredError("This server has no repository yet.")), clock
        )
        await self.fire(command)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/refresh ran in this server less than a minute ago.")

    async def test_a_run_that_went_wrong_unexpectedly_still_lets_go_of_the_server(self) -> None:
        """Otherwise one bug would hold the server until a restart."""
        clock = Clock()
        command = self.taking_turns(StubRefresh(error=RuntimeError("nobody predicted it")), clock)
        with pytest.raises(RuntimeError):
            await self.fire(command)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/refresh ran in this server less than a minute ago.")

    async def test_a_defer_that_came_too_late_still_lets_go_of_the_server(self) -> None:
        """Discord drops an interaction nobody answered within three seconds, so a bot that busy
        can have its defer refused before any work starts. The run is let go of all the same, or
        the server would be told one is running until a restart."""
        clock = Clock()
        service = StubRefresh()
        command = self.taking_turns(service, clock)
        dropped = FakeInteraction(guild_id=1, channel_id=99, user=developer())
        dropped.response = _TooLate()
        with pytest.raises(discord.NotFound):
            await command.callback(dropped, None)

        interaction = await self.fire(command)

        assert interaction.said.startswith("/refresh ran in this server less than a minute ago.")
        assert service.calls == [], "the work ran after a defer that failed"

    async def test_built_without_one_it_keeps_its_own(self) -> None:
        command = a_command(StubRefresh())
        await self.fire(command)

        interaction = await self.fire(command)

        assert interaction.mark == OWED

    def test_the_wait_is_the_minute_the_words_promise(self) -> None:
        assert REFRESH_SPACING_SECONDS == 60


class TestWhatItAsksFor:
    async def test_no_argument_covers_every_kind(self) -> None:
        service = StubRefresh()

        await run(service)

        assert service.calls == [{"guild_id": 1, "scope": RefreshScope.EVERYTHING}]

    @pytest.mark.parametrize(
        ("scope", "kind"),
        [
            (RefreshScope.ISSUES, "open issues"),
            (RefreshScope.PULL_REQUESTS, "open pull requests"),
            # No "open" on this one: a draft card has no state to be open in, and `TicketPolicy`
            # answers "open" for every card for ever.
            (RefreshScope.TICKETS, "tickets"),
        ],
    )
    async def test_a_choice_narrows_it_and_is_said_back(
        self, scope: RefreshScope, kind: str
    ) -> None:
        """The three that narrow. `all` is not one of them, so it is tested beside this rather than
        folded in: what it has to prove is that it changes nothing."""
        service = StubRefresh(outcome=an_outcome(mirrored=2, already=1))

        interaction = await run(service, scope=scope)

        assert service.calls == [{"guild_id": 1, "scope": scope}]
        assert f"Mirrored 2 {kind} from" in interaction.reply

    async def test_choosing_all_is_the_same_as_leaving_it_out(self) -> None:
        """The whole of issue #92 in one test. `all` is not new behaviour; it is the behaviour a
        bare `/refresh` always had, put somewhere a person can see it. If the two ever answer
        differently then one of them is lying to whoever picked it."""
        service = StubRefresh(outcome=an_outcome(mirrored=2, already=1))

        bare = await run(service)
        chosen = await run(service, scope=RefreshScope.EVERYTHING)

        assert service.calls == [{"guild_id": 1, "scope": RefreshScope.EVERYTHING}] * 2
        assert bare.reply == chosen.reply
        assert "Mirrored 2 open items from" in chosen.reply


class TestWhatTheDropdownOffers:
    """The picker is the feature. #92 is not that this command could not cover everything; it
    always could, by leaving the argument out. It is that nobody looking at Discord could tell.
    """

    def test_it_offers_all_four_in_the_order_it_shows_them(self) -> None:
        """Names as well as values, and order as well as membership. A value the service
        understands, under a name nobody reads as `all`, leaves the issue open with every other
        test in this file still green.

        Written out rather than derived, deliberately, because the ORDER is the claim: `all` first
        so it reads as the default, and tickets last because that is the order a capped run spends
        itself in.
        """
        offered = the_scope_option(a_command()).choices

        assert [(c.name, c.value) for c in offered] == [
            ("all", "everything"),
            ("pull requests", "pull_requests"),
            ("issues", "issues"),
            ("tickets", "tickets"),
        ]

    def test_no_scope_this_command_understands_is_left_out(self) -> None:
        """The other direction, so a fourth scope cannot be added and quietly left unreachable,
        and so nothing that is not a scope can be offered: the callback turns whatever arrives
        straight into a member and has nothing to fall back on."""
        offered = the_scope_option(a_command()).choices

        assert {c.value for c in offered} == {scope.value for scope in RefreshScope}

    def test_the_option_is_optional_and_that_is_what_makes_the_rename_safe(self) -> None:
        """A registration Discord has not caught up with sends the old name, which leaves this one
        absent. discord.py takes the default for an optional parameter and raises
        `CommandSignatureMismatch` for a required one, so the difference is a quiet widening
        against a dead interaction.

        What makes it optional is the `| None` in the annotation, not the `= None` beside it:
        `annotation_to_parameter` supplies the default from an Optional and then sets `required`
        from whether a default exists. So dropping the `= None` changes nothing here, and taking
        the `| None` out is what would break the rename.
        """
        assert the_scope_option(a_command()).required is False

    def test_every_scope_has_something_to_call_the_counts(self) -> None:
        """`_KINDS[scope]` is a lookup with no default, on the reply path, after the work is
        already done. A missing key there loses a finished run to a KeyError."""
        assert set(_KINDS) == set(RefreshScope)

    def test_every_reason_tickets_were_missed_has_something_to_say(self) -> None:
        """`_MISSED[reason]` is the same shape of lookup as `_KINDS` and on the same path: no
        default, on the reply, after the work is done. A missing key loses a finished run."""
        assert set(_MISSED) == set(MissedTickets)


class TestWhoMayRunIt:
    @pytest.mark.parametrize(
        "member", [developer(), member_with("Project Manager"), administrator()]
    )
    async def test_the_roles_that_may_sync_may_also_refresh(self, member: FakeMember) -> None:
        service = StubRefresh()

        await run(service, member)

        assert len(service.calls) == 1

    async def test_a_member_with_no_roles_is_refused(self) -> None:
        service = StubRefresh()

        interaction = await run(service, FakeMember())

        assert service.calls == []
        assert "You need one of these roles to use /refresh" in interaction.reply

    async def test_outside_a_server_it_does_nothing(self) -> None:
        service = StubRefresh()

        interaction = await run(service, guild_id=None)

        assert service.calls == []
        assert interaction.said == "Run this inside a server channel."


class TestWhenItCannotFinish:
    async def test_it_defers_before_doing_slow_work(self) -> None:
        """Reading a whole backlog is well past Discord's three seconds."""
        service = StubRefresh()

        interaction = await run(service)

        assert interaction.response.deferred is True

    async def test_a_refusal_is_answered_from_the_shared_table(self) -> None:
        service = StubRefresh(error=NotRegisteredError("This server has no repository yet."))

        interaction = await run(service)

        assert interaction.said == "This server has no repository yet."

    async def test_a_spent_rate_limit_says_how_long_to_wait(self) -> None:
        service = StubRefresh(error=GitHubRateLimitError("spent", retry_after=600))

        interaction = await run(service)

        assert "GitHub's rate limit is spent." in interaction.reply
        assert "10 minutes" in interaction.reply

    async def test_an_unexpected_failure_is_not_swallowed(self) -> None:
        """Not a ShannonError, so the command lets it out for the tree's handler to answer. The
        per-item tolerance lives in the service; nothing here is worth hiding."""
        service = StubRefresh(error=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await run(service)
