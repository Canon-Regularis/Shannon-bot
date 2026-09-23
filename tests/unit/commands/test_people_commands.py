"""The four commands that put somebody on an item, against a stub.

Issues #106 and #105. Each is three guards and a sentence, so what is worth pinning is the wording.
There are two lists on a pull request and a person can be on both, so a reply that said "assigned"
for a review request would teach somebody the wrong thing about their own repository.
"""

from __future__ import annotations

import pytest

from shannon.commands.people import (
    build_assign_command,
    build_request_review_command,
    build_unassign_command,
    build_unrequest_review_command,
)
from shannon.discord_bot.errors import DiscordGatewayError
from shannon.domain.enums import ActorRole
from shannon.domain.errors import RepositoryMismatchError
from shannon.github.errors import GitHubNotFoundError, GitHubRefusedError
from shannon.services.people import PeopleOutcome
from shannon.services.workflow import NotAnItemThreadError, WorkflowRefusedError
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import administrator, default_gate, developer, project_manager

pytestmark = pytest.mark.unit

THREAD = 9001
WHO = 4242


def outcome(
    *, role: ActorRole = ActorRole.ASSIGNEE, added: bool = True, proved: bool = True
) -> PeopleOutcome:
    return PeopleOutcome(
        login="alice", full_name="acme/widget", number=7, role=role, added=added, proved=proved
    )


class StubAssignment:
    def __init__(
        self, *, result: PeopleOutcome | None = None, error: Exception | None = None
    ) -> None:
        self.result = result or outcome()
        self.error = error
        self.calls: list[tuple[str, int, int]] = []

    async def assign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return self._record("assign", thread_id, discord_user_id)

    async def unassign(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return self._record("unassign", thread_id, discord_user_id)

    async def request_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return self._record("request_review", thread_id, discord_user_id)

    async def unrequest_review(self, *, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        return self._record("unrequest_review", thread_id, discord_user_id)

    def _record(self, what: str, thread_id: int, discord_user_id: int) -> PeopleOutcome:
        self.calls.append((what, thread_id, discord_user_id))
        if self.error is not None:
            raise self.error
        return self.result


BUILDERS = {
    "assign": build_assign_command,
    "unassign": build_unassign_command,
    "request_review": build_request_review_command,
    "unrequest_review": build_unrequest_review_command,
}


def run_it(
    *,
    service: StubAssignment | None = None,
    who=None,
    channel_id: int | None = THREAD,
    command_name: str = "assign",
):
    service = service or StubAssignment()
    command = BUILDERS[command_name](service, default_gate())
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

        command, interaction, service, member = run_it(
            who=member_with("Reviewer"), command_name="unassign"
        )

        await command.callback(interaction, member)

        assert service.calls == []


class TestWhereItHasToBeRun:
    async def test_outside_a_server(self) -> None:
        command, interaction, service, member = run_it()
        interaction.guild_id = None

        await command.callback(interaction, member)

        assert interaction.said == "Run this inside a server channel."
        assert service.calls == []

    async def test_with_no_channel_at_all(self) -> None:
        """Checked after the role, so somebody who could not run it is not told how it works."""
        command, interaction, service, member = run_it(channel_id=None)

        await command.callback(interaction, member)

        assert interaction.said == "Run this inside the item's thread."
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
    """Four sentences, chosen by the role the service reports rather than by which command ran.

    That separation is what lets the service refuse a review on an issue and still answer in the
    caller's terms, and it is why the reply never has to know which of the four was typed.
    """

    async def test_somebody_assigned(self) -> None:
        command, interaction, _, member = run_it()

        await command.callback(interaction, member)

        assert interaction.said == f"Put <@{WHO}> on acme/widget#7."

    async def test_somebody_taken_off_the_assignees(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(added=False)), command_name="unassign"
        )

        await command.callback(interaction, member)

        assert interaction.said == f"Took <@{WHO}> off acme/widget#7."

    async def test_a_change_made_on_a_link_nobody_proved_says_so(self) -> None:
        """`/link` records a login an admin typed and GitHub was never asked whose it is, so this
        may have acted on a real repository as somebody unrelated to the member named above. The
        person reading the reply is the only one in a position to notice."""
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(proved=False))
        )

        await command.callback(interaction, member)

        assert interaction.said.startswith(f"Put <@{WHO}> on acme/widget#7.")
        assert "run /link" in interaction.reply

    async def test_a_proved_one_says_nothing_extra(self) -> None:
        """A note under every reply is a note nobody reads, and almost every link will be proved
        once the server has been through it."""
        command, interaction, _, member = run_it()

        await command.callback(interaction, member)

        assert interaction.said == f"Put <@{WHO}> on acme/widget#7."

    async def test_a_review_asked_for(self) -> None:
        """Said differently from an assignment, because they are different lists and somebody can
        be on both at once. Calling one the other teaches the wrong thing about the repository."""
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(role=ActorRole.REVIEWER)),
            command_name="request_review",
        )

        await command.callback(interaction, member)

        assert interaction.said == f"Asked <@{WHO}> for a review on acme/widget#7."

    async def test_a_review_withdrawn(self) -> None:
        command, interaction, _, member = run_it(
            service=StubAssignment(result=outcome(role=ActorRole.REVIEWER, added=False)),
            command_name="unrequest_review",
        )

        await command.callback(interaction, member)

        assert interaction.said == f"Withdrew the review request from <@{WHO}> on acme/widget#7."

    async def test_the_person_is_named_as_the_mention_that_was_picked(self) -> None:
        """Not the GitHub login it resolved to. Both are true, and the one somebody chose out of a
        list is the one they will recognise in the answer."""
        command, interaction, _, member = run_it()

        await command.callback(interaction, member)

        assert "alice" not in interaction.reply


class TestEachCommandCallsItsOwnMethod:
    """Four builders over one helper, so the only thing separating them is which method they were
    handed. Crossing two of those wires is invisible to every other test in this file."""

    @pytest.mark.parametrize("name", ["assign", "unassign", "request_review", "unrequest_review"])
    async def test_the_method_matches_the_command(self, name: str) -> None:
        command, interaction, service, member = run_it(command_name=name)

        await command.callback(interaction, member)

        assert service.calls == [(name, THREAD, WHO)]


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

        assert interaction.said == "GitHub could not find that item."

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
