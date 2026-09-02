"""The eight status and priority commands, as Discord drives them.

They act on the thread they are run in, so the interaction's channel is the whole input. What is
worth pinning here is which of them exist, who may run them, and that a refusal from the service
comes back as a sentence rather than as silence after a deferred interaction.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.commands.workflow import PRIORITY_COMMANDS, STATUS_COMMANDS, build_workflow_commands
from shannon.domain.enums import Priority, Status
from shannon.services.workflow import NotAnItemThreadError, WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
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


def commands(service: StubWorkflow) -> dict[str, app_commands.Command]:
    return {command.name: command for command in build_workflow_commands(service, default_gate())}


async def run(name: str, service: StubWorkflow, member: FakeMember) -> FakeInteraction:
    interaction = FakeInteraction(user=member, channel_id=THREAD_ID)
    await commands(service)[name].callback(interaction)
    return interaction


def said(interaction: FakeInteraction) -> str:
    return (interaction.followup.messages + interaction.response.messages)[0]


def test_every_command_the_requirements_name_is_built() -> None:
    """Eight of them, and a missing one does not fail: it stops existing in Discord."""
    assert sorted(commands(StubWorkflow())) == [
        "set_backlog",
        "set_done",
        "set_high_priority",
        "set_in_review",
        "set_low_priority",
        "set_med_priority",
        "set_not_reviewed",
        "set_ready_for_merge",
    ]


@pytest.mark.parametrize(("name", "status"), list(STATUS_COMMANDS.items()))
async def test_each_status_command_sets_its_own_status(name: str, status: Status) -> None:
    service = StubWorkflow()

    await run(name, service, member_with("Project Manager"))

    assert service.calls == [("status", THREAD_ID, status)]


@pytest.mark.parametrize(("name", "priority"), list(PRIORITY_COMMANDS.items()))
async def test_each_priority_command_sets_its_own_priority(name: str, priority: Priority) -> None:
    service = StubWorkflow()

    await run(name, service, member_with("Project Manager"))

    assert service.calls == [("priority", THREAD_ID, priority)]


@pytest.mark.parametrize("who", [project_manager, administrator])
async def test_project_managers_and_administrators_may_run_them(who) -> None:
    service = StubWorkflow()

    await run("set_in_review", service, who())

    assert service.calls, "somebody the requirements allow was refused"


async def test_a_developer_may_not() -> None:
    """The permissions table grants these to reviewers and project managers. A developer moving
    their own work to ready for merge is the review step going missing."""
    service = StubWorkflow()

    interaction = await run("set_ready_for_merge", service, developer())

    assert service.calls == []
    assert "You need one of these roles" in said(interaction)


async def test_the_reply_names_the_item_and_what_it_became() -> None:
    service = StubWorkflow()

    interaction = await run("set_in_review", service, member_with("Project Manager"))

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now IN_REVIEW."


async def test_a_repeat_says_so_rather_than_claiming_a_change() -> None:
    service = StubWorkflow(outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=False))

    interaction = await run("set_backlog", service, member_with("Project Manager"))

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is already BACKLOG."


async def test_finishing_says_the_thread_is_locked() -> None:
    service = StubWorkflow(
        outcome=WorkflowOutcome("Canon-Regularis/Shannon-bot", 7, changed=True, locked=True)
    )

    interaction = await run("set_done", service, member_with("Project Manager"))

    assert said(interaction).endswith("is now DONE, and this thread is locked.")


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

    interaction = await run("set_done", service, member_with("Project Manager"))

    answer = said(interaction)
    assert answer.startswith("Canon-Regularis/Shannon-bot#7 is DONE")
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

    interaction = await run("set_in_review", service, member_with("Project Manager"))

    answer = said(interaction)
    assert "could not be unlocked" in answer
    assert "nobody can reply in it" in answer


async def test_a_refusal_comes_back_as_a_sentence() -> None:
    """The interaction has been deferred by this point, so anything escaping leaves the person
    who ran it watching a spinner until Discord gives up."""
    service = StubWorkflow(error=NotAnItemThreadError("Run this inside the item's thread."))

    interaction = await run("set_in_review", service, member_with("Project Manager"))

    assert said(interaction) == "Run this inside the item's thread."


async def test_a_priority_reply_reads_as_a_priority() -> None:
    service = StubWorkflow()

    interaction = await run("set_high_priority", service, member_with("Project Manager"))

    assert said(interaction) == "Canon-Regularis/Shannon-bot#7 is now HIGH priority."


async def test_it_refuses_outside_a_server() -> None:
    """Guild-only is declared to Discord, and checked again because a declaration is not a gate."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), guild_id=None)

    await commands(service)["set_in_review"].callback(interaction)

    assert service.calls == []
    assert said(interaction) == "Run this inside a server channel."


async def test_it_refuses_with_no_channel_to_act_on() -> None:
    """The thread is the whole input, so an interaction without one has nothing to work from."""
    service = StubWorkflow()
    interaction = FakeInteraction(user=member_with("Project Manager"), channel_id=None)

    await commands(service)["set_in_review"].callback(interaction)

    assert service.calls == []
    assert said(interaction) == "Run this inside the item's thread."
