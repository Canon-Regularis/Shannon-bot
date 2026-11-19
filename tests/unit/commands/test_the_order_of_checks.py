"""The order a GitHub-gated command makes its checks in. Issue #267.

Nine commands ask GitHub whether the caller may write (#158), and `github_allows` documents two
rules about when. GitHub is asked only once the interaction is deferred, because the question is
a network call and Discord drops an interaction left unanswered for three seconds. And the role
is asked first, so somebody without it hears about the role rather than about a GitHub account
they may never have connected.

Nothing could see either rule. Every reply in the command tests is read back through
`FakeInteraction.said`, which merges the first response with the followups, so swapping the
defer and the question, or the role and the question, passed. #267 folds those three steps into
a shared helper, which is exactly where they could be reordered, so they are pinned here first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

import pytest

from shannon.commands.conversations import build_log_conversation_command
from shannon.commands.labels import build_label_command, build_unlabel_command
from shannon.commands.workflow import PRIORITY_CHOICES, build_workflow_commands
from shannon.discord_bot.permissions import PermissionGate
from shannon.discord_bot.responses import REFUSED
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import Priority
from shannon.github import people
from shannon.services.workflow import WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.fakes.grants import FakeGrants
from tests.unit.commands.conftest import (
    FakeAccess,
    administrator,
    default_gate,
    gate_with,
    member_with,
)
from tests.unit.commands.test_conversation_commands import StubLog
from tests.unit.commands.test_label_commands import StubLabels, StubSuggestions
from tests.unit.commands.test_people_commands import BUILDERS as PEOPLE
from tests.unit.commands.test_people_commands import StubAssignment
from tests.unit.commands.test_workflow_commands import FakeColumns, StubWorkflow

pytestmark = pytest.mark.unit

THREAD = 9001
NO = "GitHub does not have monalisa as a collaborator."

Callback = Callable[..., Awaitable[None]]


class Moves(StubWorkflow):
    """`StubWorkflow`, with `set_priority` taking `acting` the way `MovesItems` declares it.

    The shared stub makes it required, which both type checkers refuse against the protocol, and
    this file is held to them where that one is not. Widened here so the shared one stays as is.
    """

    async def set_priority(
        self, *, thread_id: int, priority: Priority, acting: int | None = None
    ) -> WorkflowOutcome:
        self.calls.append(("priority", thread_id, priority))
        self.acted.append(acting)
        if self.error is not None:
            raise self.error
        return self.outcome


class WatchingAccess(FakeAccess):
    """`FakeAccess`, also noting whether the interaction had been deferred when it was asked.

    Kept here rather than added to the shared stand-in: these are the only tests that need it,
    and the shared one is read by every command test as it stands.
    """

    def __init__(self, interaction: FakeInteraction, refusal: str | None = None) -> None:
        super().__init__(refusal)
        self.interaction = interaction
        self.deferred_when_asked: list[bool] = []

    async def refusal_for(
        self, *, guild_id: int, discord_user_id: int, at_least: str
    ) -> str | None:
        self.deferred_when_asked.append(self.interaction.response.deferred)
        return await super().refusal_for(
            guild_id=guild_id, discord_user_id=discord_user_id, at_least=at_least
        )


@dataclass(frozen=True)
class Ready:
    """One gated command, built and waiting to be run, with a way to ask whether it wrote."""

    command: SlashCommand
    options: tuple[object, ...]
    writes: Callable[[], int]


Builds = Callable[[FakeAccess, PermissionGate], Ready]


def label(access: FakeAccess, gate: PermissionGate) -> Ready:
    service = StubLabels()
    command = build_label_command(service, gate, access, StubSuggestions())
    return Ready(command, ("bug",), lambda: len(service.calls))


def unlabel(access: FakeAccess, gate: PermissionGate) -> Ready:
    service = StubLabels()
    command = build_unlabel_command(service, gate, access, StubSuggestions())
    return Ready(command, ("bug",), lambda: len(service.calls))


def person(name: str) -> Builds:
    def build(access: FakeAccess, gate: PermissionGate) -> Ready:
        service = StubAssignment()
        command = PEOPLE[name](service, gate, access)
        return Ready(command, (FakeMember(id=4242),), lambda: len(service.calls))

    return build


def workflow(name: str, picked: object) -> Builds:
    def build(access: FakeAccess, gate: PermissionGate) -> Ready:
        service = Moves()
        built = build_workflow_commands(service, gate, access, FakeColumns())
        command = next(command for command in built if command.name == name)
        return Ready(command, (picked,), lambda: len(service.calls))

    return build


def log_conversation(access: FakeAccess, gate: PermissionGate) -> Ready:
    service = StubLog()
    command = build_log_conversation_command(service, gate, access, capturing=True)
    return Ready(command, (), lambda: len(service.started))


HIGH = next(choice for choice in PRIORITY_CHOICES if choice.value == "HIGH")

GATED = [
    pytest.param(label, id="label"),
    pytest.param(unlabel, id="unlabel"),
    *(pytest.param(person(name), id=name) for name in PEOPLE),
    pytest.param(workflow("status", "In review"), id="status"),
    pytest.param(workflow("priority", HIGH), id="priority"),
    pytest.param(log_conversation, id="log_conversation"),
]

LABELS = [pytest.param(label, id="label"), pytest.param(unlabel, id="unlabel")]


async def fire(ready: Ready, interaction: FakeInteraction) -> None:
    """The callback typed once: the interaction is a stand-in, and `app_commands.Command`
    declares its parameters as `...`."""
    await cast(Callback, ready.command.callback)(interaction, *ready.options)


def in_the_thread(who: FakeMember) -> FakeInteraction:
    return FakeInteraction(user=who, channel_id=THREAD)


class TestGitHubIsAskedOnlyOnceDeferred:
    @pytest.mark.parametrize("builds", GATED)
    async def test_the_interaction_was_deferred_before_github_was_asked(
        self, builds: Builds
    ) -> None:
        interaction = in_the_thread(administrator())
        access = WatchingAccess(interaction)

        await fire(builds(access, default_gate()), interaction)

        assert access.deferred_when_asked == [True]

    @pytest.mark.parametrize("builds", GATED)
    async def test_it_asks_for_write(self, builds: Builds) -> None:
        """Every one of them is a write. Asking for admin would refuse the collaborators these
        commands are for, and the people and label commands had no test saying so."""
        interaction = in_the_thread(administrator())
        access = FakeAccess()

        await fire(builds(access, default_gate()), interaction)

        assert [at_least for _, _, at_least in access.asked] == [people.WRITE]

    @pytest.mark.parametrize("builds", GATED)
    async def test_its_refusal_is_a_private_followup_and_not_the_first_response(
        self, builds: Builds
    ) -> None:
        """What a defer that came second would change: the refusal would be the first response,
        and in production it would be racing Discord's three seconds to get there."""
        interaction = in_the_thread(administrator())

        await fire(builds(FakeAccess(NO), default_gate()), interaction)

        assert interaction.response.messages == []
        assert interaction.followup.messages == [f"{REFUSED} {NO}"]
        assert interaction.ephemerally == [True]


class TestTheRoleIsCheckedBeforeGitHub:
    @pytest.mark.parametrize("builds", LABELS)
    async def test_somebody_without_the_role_is_never_asked_about(self, builds: Builds) -> None:
        interaction = in_the_thread(member_with("Reviewer"))
        access = FakeAccess(NO)

        await fire(builds(access, default_gate()), interaction)

        assert access.asked == [], "it asked GitHub about somebody the role already refused"
        assert "You need one of these roles" in interaction.reply
        assert NO not in interaction.reply

    @pytest.mark.parametrize("builds", LABELS)
    async def test_the_roles_refusal_is_the_first_response(self, builds: Builds) -> None:
        """On a server where GitHub cannot stand in for a role the role is answered from memory,
        so its refusal goes out before anything is deferred."""
        interaction = in_the_thread(member_with("Reviewer"))

        await fire(builds(FakeAccess(NO), default_gate()), interaction)

        assert interaction.response.deferred is False
        assert interaction.followup.messages == []

    @pytest.mark.parametrize("builds", LABELS)
    async def test_where_github_stands_in_for_the_role_both_questions_follow_the_defer(
        self, builds: Builds
    ) -> None:
        """Issue #94. A member with no role whom GitHub lets in as a developer is asked about
        twice, by the tier guard and then by `github_allows`, and neither may come first."""
        interaction = in_the_thread(member_with("Reviewer"))
        grants = FakeGrants(None, watching=interaction)
        access = WatchingAccess(interaction)

        await fire(builds(access, gate_with(grants)), interaction)

        assert grants.deferred_when_asked == [True]
        assert access.deferred_when_asked == [True]

    @pytest.mark.parametrize("builds", LABELS)
    async def test_a_tier_github_turns_away_is_never_asked_about_writing(
        self, builds: Builds
    ) -> None:
        """The tier question is the role's stand-in, so it keeps the role's place: first."""
        interaction = in_the_thread(member_with("Reviewer"))
        access = FakeAccess()

        await fire(builds(access, gate_with(FakeGrants())), interaction)

        assert access.asked == []


class TestARefusalStopsTheCommand:
    @pytest.mark.parametrize("builds", GATED)
    async def test_the_role_refusing_writes_nothing(self, builds: Builds) -> None:
        interaction = in_the_thread(member_with("nothing"))
        ready = builds(FakeAccess(), default_gate())

        await fire(ready, interaction)

        assert ready.writes() == 0

    @pytest.mark.parametrize("builds", GATED)
    async def test_github_refusing_writes_nothing(self, builds: Builds) -> None:
        interaction = in_the_thread(administrator())
        ready = builds(FakeAccess(NO), default_gate())

        await fire(ready, interaction)

        assert ready.writes() == 0

    @pytest.mark.parametrize("builds", GATED)
    async def test_with_neither_refusing_it_does_write(self, builds: Builds) -> None:
        """The control. Without it the two above pass for a stand-in that never writes at all."""
        interaction = in_the_thread(administrator())
        ready = builds(FakeAccess(), default_gate())

        await fire(ready, interaction)

        assert ready.writes() == 1
