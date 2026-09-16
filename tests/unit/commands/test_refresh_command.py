"""What `/refresh` says, and who may run it. Issue #74.

The counts are the whole reply, so the wording is asserted rather than sampled: the difference
between "done" and "run it again" is one clause, and somebody acts on it.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.refresh import build_refresh_command
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


async def run(
    service: StubRefresh,
    member: FakeMember | None = None,
    *,
    only: str | None = None,
    guild_id: int | None = 1,
) -> FakeInteraction:
    interaction = FakeInteraction(
        guild_id=guild_id, channel_id=99, user=member if member is not None else developer()
    )
    choice = None if only is None else app_commands.Choice(name=only, value=only)
    await build_refresh_command(service, default_gate()).callback(interaction, choice)
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
        ("only", "scope", "kind"),
        [
            ("issues", RefreshScope.ISSUES, "open issues"),
            ("pull_requests", RefreshScope.PULL_REQUESTS, "open pull requests"),
        ],
    )
    async def test_a_choice_narrows_it_and_is_said_back(
        self, only: str, scope: RefreshScope, kind: str
    ) -> None:
        service = StubRefresh(outcome=an_outcome(mirrored=2, already=1))

        interaction = await run(service, only=only)

        assert service.calls == [{"guild_id": 1, "scope": scope}]
        assert f"Mirrored 2 {kind} from" in interaction.reply


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
