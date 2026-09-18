"""`/assign` and `/unassign`, against a stub.

Issue #106. The commands are three guards and a sentence each, so what is worth pinning is the
wording: one command does two different things depending on the thread it is in, and a reply that
said "assigned" for a review request would teach people the wrong thing about their own repository.
"""

from __future__ import annotations

import pytest

from shannon.commands.assign import build_assign_command, build_unassign_command
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.errors import RepositoryMismatchError
from shannon.github.errors import GitHubNotFoundError, GitHubRefusedError
from shannon.services.assignment import AssignmentOutcome
from shannon.services.workflow import NotAnItemThreadError, WorkflowRefusedError
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

THREAD = 9001
WHO = 4242


def outcome(*, reviewing: bool = True, added: bool = True) -> AssignmentOutcome:
    return AssignmentOutcome(
        login="alice",
        full_name="acme/widget",
        number=7,
        reviewing=reviewing,
        added=added,
    )


class StubAssignment:
    def __init__(
        self, *, result: AssignmentOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or outcome()
        self.error = error
        self.calls: list[tuple[str, int, int]] = []

    async def assign(self, *, thread_id: int, discord_user_id: int) -> AssignmentOutcome:
        return self._record("assign", thread_id, discord_user_id)

    async def unassign(self, *, thread_id: int, discord_user_id: int) -> AssignmentOutcome:
        return self._record("unassign", thread_id, discord_user_id)

    def _record(self, what: str, thread_id: int, discord_user_id: int) -> AssignmentOutcome:
        self.calls.append((what, thread_id, discord_user_id))
        if self.error is not None:
            raise self.error
        return self.result


def run_it(
    *,
    service: StubAssignment | None = None,
    who=None,
    channel_id: int | None = THREAD,
    removing: bool = False,
):
    service = service or StubAssignment()
    build = build_unassign_command if removing else build_assign_command
    command = build(service, default_gate())
    interaction = FakeInteraction(user=who or developer(), channel_id=channel_id)
    return command, interaction, service, FakeMember(id=WHO)


class TestWhoMayRunIt:
    @pytest.mark.parametrize("who", [developer, project_manager, administrator])
    async def test_the_tiers_that_may(self, who) -> None:
        command, interaction, service, member = run_it(who=who())

        await command.callback(interaction, member)

        assert service.calls == [("assign", THREAD, WHO)]

    async def test_anybody_else_is_refused_before_anything_is_written(self) -> None:
        """It writes to GitHub, so a refusal has to come before the call and not after it."""
        from tests.unit.commands.conftest import member_with

        command, interaction, service, member = run_it(who=member_with("Reviewer"))

        await command.callback(interaction, member)

        assert "You need one of these roles" in interaction.reply
        assert service.calls == []

    async def test_the_removal_is_gated_the_same_way(self) -> None:
        from tests.unit.commands.conftest import member_with

        command, interaction, service, member = run_it(who=member_with("Reviewer"), removing=True)

        await command.callback(interaction, member)

        assert service.calls == []


class TestWhereItHasToBeRun:
    async def test_outside_a_server(self) -> None:
        command, interaction, service, member = run_it()
        interaction.guild_id = None

        await command.callback(interaction, member)

        assert interaction.reply == "Run this inside a server channel."
        assert service.calls == []

    async def test_with_no_channel_at_all(self) -> None:
        """Checked after the role, so somebody who could not run it is not told how it works."""
        command, interaction, service, member = run_it(channel_id=None)

        await command.callback(interaction, member)

        assert interaction.reply == "Run this inside the item's thread."
        assert service.calls == []

    async def test_it_acts_on_the_thread_it_was_run_in(self) -> None:
        command, interaction, service, member = run_it(channel_id=55)

        await command.callback(interaction, member)

        assert service.calls == [("assign", 55, WHO)]

    async def test_it_acts_on_the_member_it_was_given(self) -> None:
        command, interaction, service, _ = run_it()

        await command.callback(interaction, FakeMember(id=777))

        assert service.calls == [("assign", THREAD, 777)]


class TestWhatItSays:
    async def test_a_review_asked_for(self) -> None:
        command, interaction, _, member = run_it()

        await command.callback(interaction, member)

        assert interaction.reply == f"Asked <@{WHO}> for a review on acme/widget#7."

    async def test_an_issue_assigned(self) -> None:
        """The same command, said differently, because GitHub keeps the two apart and calling an
        assignment a review would teach somebody the wrong thing about their own repository."""
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(reviewing=False))
        )

        await command.callback(interaction, member)

        assert interaction.reply == f"Assigned <@{WHO}> to acme/widget#7."

    async def test_a_review_withdrawn(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(added=False)), removing=True
        )

        await command.callback(interaction, member)

        assert interaction.reply == f"Withdrew the review request from <@{WHO}> on acme/widget#7."

    async def test_an_assignee_taken_off(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(reviewing=False, added=False)), removing=True
        )

        await command.callback(interaction, member)

        assert interaction.reply == f"Took <@{WHO}> off acme/widget#7."

    async def test_the_person_is_named_as_the_mention_that_was_picked(self) -> None:
        """Not the GitHub login it resolved to. Both are true, and the one somebody chose out of a
        list is the one they will recognise in the answer."""
        command, interaction, _, member = run_it()

        await command.callback(interaction, member)

        assert "alice" not in interaction.reply


class TestWhatItRefuses:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (NotAnItemThreadError("Run this inside the thread of a tracked item."), "tracked item"),
            (WorkflowRefusedError("alice has already been asked to review this."), "already been"),
            (RepositoryMismatchError("acme/widget is not that repository any more."), "any more"),
            (
                GitHubRefusedError("Reviews may only be requested from collaborators."),
                "collaborators",
            ),
        ],
    )
    async def test_a_refusal_is_reported_in_its_own_words(
        self, error: Exception, expected: str
    ) -> None:
        command, interaction, _, member = run_it(service=StubAssignment(error=error))

        await command.callback(interaction, member)

        assert expected in interaction.reply

    async def test_github_refusing_is_not_reported_as_being_unreachable(self) -> None:
        """The whole reason `GitHubRefusedError` exists. Under the catch-all this read as a fault
        to wait out, when it is something the person can put right in ten seconds."""
        command, interaction, _, member = run_it(
            service=StubAssignment(error=GitHubRefusedError("Not a collaborator."))
        )

        await command.callback(interaction, member)

        assert "could not be reached" not in interaction.reply
        assert "GitHub would not do that" in interaction.reply

    async def test_an_item_deleted_on_github(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(error=GitHubNotFoundError("x"))
        )

        await command.callback(interaction, member)

        assert interaction.reply == "GitHub could not find that item."

    async def test_discord_refusing_is_reported_rather_than_raised(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(error=DiscordGatewayError("Discord said no"))
        )

        await command.callback(interaction, member)

        assert interaction.reply != ""

    async def test_anything_that_is_not_ours_is_left_to_the_handler(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(error=RuntimeError("a bug"))
        )

        with pytest.raises(RuntimeError, match="a bug"):
            await command.callback(interaction, member)
