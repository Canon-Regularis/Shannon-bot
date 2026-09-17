"""What `/refresh` says, and who may run it. Issue #74.

The counts are the whole reply, so the wording is asserted rather than sampled: the difference
between "done" and "run it again" is one clause, and somebody acts on it.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.refresh import _KINDS, build_refresh_command
from shannon.domain.errors import NotRegisteredError
from shannon.github.errors import GitHubRateLimitError
from shannon.services.sync.refresh import RefreshOutcome, RefreshScope
from tests.fakes.discord_objects import FakeInteraction, FakeMember
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

        assert interaction.reply == (
            f"{FULL_NAME} has no open items right now, so there was nothing to mirror."
        )

    async def test_everything_already_mirrored_says_nothing_to_do(self) -> None:
        service = StubRefresh(outcome=an_outcome(already=43))

        interaction = await run(service)

        assert interaction.reply == (
            f"Nothing to mirror. All 43 open items on {FULL_NAME} already have a thread."
        )

    async def test_a_clean_run_counts_both_halves_and_says_nobody_was_pinged(self) -> None:
        service = StubRefresh(outcome=an_outcome(mirrored=7, already=31))

        interaction = await run(service)

        assert interaction.reply == (
            f"Mirrored 7 open items from {FULL_NAME}, and left alone the 31 that already had a "
            "thread. Nobody was pinged."
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


class TestWhatItAsksFor:
    async def test_no_argument_covers_both_kinds(self) -> None:
        service = StubRefresh()

        await run(service)

        assert service.calls == [{"guild_id": 1, "scope": RefreshScope.EVERYTHING}]

    @pytest.mark.parametrize(
        ("scope", "kind"),
        [
            (RefreshScope.ISSUES, "open issues"),
            (RefreshScope.PULL_REQUESTS, "open pull requests"),
        ],
    )
    async def test_a_choice_narrows_it_and_is_said_back(
        self, scope: RefreshScope, kind: str
    ) -> None:
        """The two that narrow. `all` is not one of them, so it is tested beside this rather than
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

    def test_it_offers_all_three_in_the_order_it_shows_them(self) -> None:
        """Names as well as values, and order as well as membership. A value the service
        understands, under a name nobody reads as `all`, leaves the issue open with every other
        test in this file still green."""
        offered = the_scope_option(a_command()).choices

        assert [(c.name, c.value) for c in offered] == [
            ("all", "everything"),
            ("pull requests", "pull_requests"),
            ("issues", "issues"),
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
        assert interaction.reply == "Run this inside a server channel."


class TestWhenItCannotFinish:
    async def test_it_defers_before_doing_slow_work(self) -> None:
        """Reading a whole backlog is well past Discord's three seconds."""
        service = StubRefresh()

        interaction = await run(service)

        assert interaction.response.deferred is True

    async def test_a_refusal_is_answered_from_the_shared_table(self) -> None:
        service = StubRefresh(error=NotRegisteredError("This server has no repository yet."))

        interaction = await run(service)

        assert interaction.reply == "This server has no repository yet."

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
