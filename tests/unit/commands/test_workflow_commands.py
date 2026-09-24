"""`/status` and `/priority`, as Discord drives them.

They act on the thread they are run in, so the interaction's channel and the picked value are
the whole input. What is worth pinning is which of them exist, what their pickers offer, who
may run them, and that a refusal from the service comes back as a sentence rather than as
silence after a deferred interaction.

It was eight commands taking no argument. The two tables they were built from are now two
choice lists, and the reason each is written out rather than comprehended over its enum is
what the last two tests here hold.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.workflow import (
    PRIORITY_CHOICES,
    STATUS_CHOICES,
    build_workflow_commands,
)
from shannon.domain.enums import Priority, Status
from shannon.github import people
from shannon.github.labels import PRIORITY_LABELS
from shannon.services.workflow import NotAnItemThreadError, WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
    FakeAccess,
    administrator,
    default_gate,
    developer,
    member_with,
    project_manager,
)

THREAD_ID = 555


class StubWorkflow:
    def __init__(self, *, outcome: WorkflowOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome or WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=True)
        self.error = error
        self.calls: list[tuple[str, int, object]] = []

    async def set_status(self, *, thread_id: int, status: Status) -> WorkflowOutcome:
        self.calls.append(("status", thread_id, status))
        if self.error is not None:
            raise self.error
        return self.outcome

    async def set_priority(self, *, thread_id: int, priority: Priority) -> WorkflowOutcome:
        self.calls.append(("priority", thread_id, priority))
        if self.error is not None:
            raise self.error
        return self.outcome


def commands(
    service: StubWorkflow, access: FakeAccess | None = None
) -> dict[str, app_commands.Command]:
    built = build_workflow_commands(service, default_gate(), access or FakeAccess())
    return {command.name: command for command in built}


async def run(
    name: str,
    service: StubWorkflow,
    member: FakeMember,
    value: str,
    access: FakeAccess | None = None,
) -> FakeInteraction:
    interaction = FakeInteraction(user=member, channel_id=THREAD_ID)
    await commands(service, access)[name].callback(
        interaction, app_commands.Choice(name=value, value=value)
    )
    return interaction


def said(interaction: FakeInteraction) -> str:
    """The sentence, without the outcome mark `FakeInteraction.said` strips."""
    return interaction.said


def test_both_commands_are_built() -> None:
    """Two now, where there were eight. A missing one does not fail a test at boot: it
    stops existing in Discord."""
    assert sorted(commands(StubWorkflow())) == ["priority", "status"]


@pytest.mark.parametrize("status", list(Status))
async def test_each_status_can_be_picked(status: Status) -> None:
    service = StubWorkflow()

    await run("status", service, member_with("Project Manager"), status.value)

    assert service.calls == [("status", THREAD_ID, status)]


@pytest.mark.parametrize("priority", [p for p in Priority if p is not Priority.UNSET])
async def test_each_priority_can_be_picked(priority: Priority) -> None:
    service = StubWorkflow()

    await run("priority", service, member_with("Project Manager"), priority.value)

    assert service.calls == [("priority", THREAD_ID, priority)]


@pytest.mark.parametrize("who", [project_manager, administrator])
async def test_project_managers_and_administrators_may_run_them(who) -> None:
    service = StubWorkflow()

    await run("status", service, who(), Status.IN_REVIEW.value)

    assert service.calls, "somebody the requirements allow was refused"


async def test_a_developer_may_not() -> None:
    """The permissions table grants these to reviewers and project managers. A developer moving
    their own work to ready for merge is the review step going missing."""
    service = StubWorkflow()

    interaction = await run("status", service, developer(), Status.READY_FOR_MERGE.value)

    assert service.calls == []
    assert "You need one of these roles" in said(interaction)


async def test_the_reply_names_the_item_and_what_it_became() -> None:
    service = StubWorkflow()

    interaction = await run(
        "status", service, member_with("Project Manager"), Status.IN_REVIEW.value
    )

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now In review."


async def test_a_repeat_says_so_rather_than_claiming_a_change() -> None:
    service = StubWorkflow(outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=False))

    interaction = await run("status", service, member_with("Project Manager"), Status.BACKLOG.value)

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is already Backlog."


async def test_finishing_says_the_thread_is_locked() -> None:
    service = StubWorkflow(
        outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=True, locked=True)
    )

    interaction = await run("status", service, member_with("Project Manager"), Status.DONE.value)

    assert said(interaction).endswith("is now Done, and this thread is locked.")


async def test_a_lock_discord_refused_says_what_did_happen_as_well() -> None:
    """Everything but the lock landed, and reporting only the refusal reads as the opposite.

    Somebody told the command failed goes and runs it again from the top, or worse, decides the
    item is not done and chases it. What it needs to say is that the item moved, that the one
    step left is a permission, and that running it again is what takes it.
    """
    service = StubWorkflow(
        outcome=WorkflowOutcome(
            "Canon-Regularis/Shannon-bot",
            7,
            changed=True,
            locked=False,
            lock_refused=True,
            wanted_locked=True,
        )
    )

    interaction = await run("status", service, member_with("Project Manager"), Status.DONE.value)

    answer = said(interaction)
    assert answer.startswith("Canon-Regularis/Shannon-bot#7 is Done")
    assert "could not be locked" in answer
    assert "Manage Threads" in answer


async def test_a_refused_unlock_says_nobody_can_reply_rather_than_the_opposite() -> None:
    """The two directions are not the same news.

    A thread that would not lock is untidy. A thread that would not unlock is shut against the
    discussion the person running this has just reopened it for, and they need to know that
    before they walk away from it.
    """
    service = StubWorkflow(
        outcome=WorkflowOutcome(
            "Canon-Regularis/Shannon-bot",
            7,
            changed=True,
            locked=True,
            lock_refused=True,
            wanted_locked=False,
        )
    )

    interaction = await run(
        "status", service, member_with("Project Manager"), Status.IN_REVIEW.value
    )

    answer = said(interaction)
    assert "could not be unlocked" in answer
    assert "nobody can reply in it" in answer


async def test_a_refusal_comes_back_as_a_sentence() -> None:
    """The interaction has been deferred by this point, so anything escaping leaves the person
    who ran it watching a spinner until Discord gives up."""
    service = StubWorkflow(error=NotAnItemThreadError("Run this inside the item's thread."))

    interaction = await run(
        "status", service, member_with("Project Manager"), Status.IN_REVIEW.value
    )

    assert said(interaction) == "Run this inside the item's thread."


async def test_a_priority_reply_reads_as_a_priority() -> None:
    service = StubWorkflow()

    interaction = await run(
        "priority", service, member_with("Project Manager"), Priority.HIGH.value
    )

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now High priority."


async def test_it_refuses_outside_a_server() -> None:
    """Guild-only is declared to Discord, and checked again because a declaration is not a gate."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), guild_id=None)

    await commands(service)["status"].callback(
        interaction, app_commands.Choice(name="In review", value=Status.IN_REVIEW.value)
    )

    assert service.calls == []
    assert said(interaction) == "Run this inside a server channel."


async def test_it_refuses_with_no_channel_to_act_on() -> None:
    """The thread is the whole input, so an interaction without one has nothing to work from."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=None)

    await commands(service)["status"].callback(
        interaction, app_commands.Choice(name="In review", value=Status.IN_REVIEW.value)
    )

    assert service.calls == []
    assert said(interaction) == "Run this inside the item's thread."


def test_the_status_picker_opens_on_the_state_work_starts_in() -> None:
    """Discord shows choices in the order they are written, and the enum is not in that
    order: it declares BACKLOG fourth, which is right for the database and wrong for a
    person. Comprehended over `Status`, the picker would open on Not reviewed with Backlog
    buried under Ready for merge."""
    assert [choice.value for choice in STATUS_CHOICES] == [
        Status.BACKLOG.value,
        Status.NOT_REVIEWED.value,
        Status.IN_REVIEW.value,
        Status.READY_FOR_MERGE.value,
        Status.DONE.value,
    ]


def test_no_priority_can_be_picked_that_has_no_label_to_write() -> None:
    """The one test standing between somebody tidying the priority table into a
    comprehension and a KeyError in front of a user.

    `Priority` has four members and `PRIORITY_LABELS` has three: UNSET is what an item has
    before anybody says, not something to pick. `priority_change` ends in a lookup against
    that table, so a picked None would raise rather than refuse, and reach whoever ran it as
    "Something went wrong here". Eight commands could not express UNSET and so were immune
    to this; two commands with a picker are not."""
    assert {Priority(choice.value) for choice in PRIORITY_CHOICES} == set(PRIORITY_LABELS)


def test_between_them_the_pickers_offer_every_state_but_the_empty_one() -> None:
    """Re-homed from the domain tests, where it held the same invariant against a table
    of command names that no longer exists. Here rather than there because that file is on
    neither type-suppression list, and putting discord.py's choice types into it would be a
    new strict-typing surface in a file that has none."""
    offered: set[Status | Priority] = {Status(c.value) for c in STATUS_CHOICES} | {
        Priority(c.value) for c in PRIORITY_CHOICES
    }

    assert offered | {Priority.UNSET} == {*Status, *Priority}


class TestWhatGitHubSays:
    """The second gate, added by issue #158. The Discord role says who may ask; this says whether
    the account they proved may actually write to the repository."""

    async def test_a_refusal_from_github_stops_the_command(self) -> None:
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub does not have monalisa as a collaborator.")
        interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=THREAD_ID)

        await commands(service, access)["status"].callback(
            interaction, app_commands.Choice(name="In review", value=Status.IN_REVIEW.value)
        )

        assert service.calls == [], "it wrote to GitHub after GitHub said no"
        assert "not have monalisa as a collaborator" in said(interaction)

    async def test_the_role_is_checked_first(self) -> None:
        """Somebody without the Discord role is told that, rather than told about a GitHub
        account they may never have connected."""
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub says no.")
        interaction = FakeInteraction(user=developer(), channel_id=THREAD_ID)

        await commands(service, access)["status"].callback(
            interaction, app_commands.Choice(name="In review", value=Status.IN_REVIEW.value)
        )

        assert "You need one of these roles" in said(interaction)
        assert access.asked == [], "it asked GitHub about somebody the role already refused"

    async def test_it_asks_for_write_rather_than_admin(self) -> None:
        """A collaborator with write is exactly who these commands are for. Asking for admin
        would refuse the reviewers the permission table grants them to."""
        service = StubWorkflow()
        access = FakeAccess()

        await run("status", service, member_with("Project Manager"), Status.IN_REVIEW.value, access)

        assert [asked[2] for asked in access.asked] == [people.WRITE]

    async def test_priority_is_gated_too(self) -> None:
        service = StubWorkflow()
        access = FakeAccess(refusal="GitHub says no.")
        interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=THREAD_ID)

        await commands(service, access)["priority"].callback(
            interaction, app_commands.Choice(name="High", value=Priority.HIGH.value)
        )

        assert service.calls == []
