"""The WARNING line each command writes when a `ShannonError` refuses it. Issue #267.

The member reads the refusal, and every command's own tests pin that. The operator reads this
line, and nothing pinned it: #267 folds the commands' shared halves together, and a fold that
dropped the line, or dropped the typed value `/status` and `/priority` put in it on purpose, would
have passed every test. So each one is written out here exactly as it reads.

`/register` and `/unregister` logged `register failed` and `unregister failed` until #267 worded
them as every other command does, as a change of its own rather than as part of a fold. `/track`
still reads differently and is pinned as it is.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import cast

import pytest

from shannon.commands.conversations import (
    build_log_conversation_command,
    build_stop_conversation_command,
)
from shannon.commands.labels import build_label_command, build_unlabel_command
from shannon.commands.link_team import build_link_team_command
from shannon.commands.refresh import build_refresh_command
from shannon.commands.regenerate import build_regenerate_command
from shannon.commands.register import build_register_command
from shannon.commands.sync_link import build_issue_command, build_pr_command
from shannon.commands.track import build_track_command
from shannon.commands.unregister import build_unregister_command
from shannon.commands.workflow import PRIORITY_CHOICES, build_workflow_commands
from shannon.discord_bot.slash import SlashCommand
from shannon.domain.enums import Priority
from shannon.domain.errors import RepositoryMismatchError, ShannonError
from shannon.services.workflow import WorkflowOutcome
from tests.fakes.discord_objects import FakeInteraction, FakeMember
from tests.unit.commands.conftest import (
    FakeAccess,
    FakeVerification,
    administrator,
    default_gate,
)
from tests.unit.commands.test_board_command import Built, StubBoards
from tests.unit.commands.test_board_command import fire as fire_half
from tests.unit.commands.test_conversation_commands import StubLog
from tests.unit.commands.test_label_commands import StubLabels, StubSuggestions
from tests.unit.commands.test_link_team_command import FakeRole as TeamRole
from tests.unit.commands.test_link_team_command import StubTeamLinking
from tests.unit.commands.test_people_commands import BUILDERS as PEOPLE
from tests.unit.commands.test_people_commands import StubAssignment
from tests.unit.commands.test_refresh_command import StubRefresh
from tests.unit.commands.test_regenerate_command import StubRegeneration
from tests.unit.commands.test_register_command import StubRegistration
from tests.unit.commands.test_sync_link_command import StubManualSync
from tests.unit.commands.test_track_command import StubTracking
from tests.unit.commands.test_unregister_command import FakeUnregistration
from tests.unit.commands.test_workflow_commands import FakeColumns, StubWorkflow

pytestmark = pytest.mark.unit

THREAD = 9001
REFUSAL = "This server is registered to acme/widget, not acme/gadget."

Callback = Callable[..., Awaitable[None]]

# What a case is: given the refusal its service is to raise, run the command once.
Runs = Callable[[ShannonError], Awaitable[None]]


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


async def fire(command: SlashCommand, *options: object, guild_id: int = 1) -> None:
    """Run a command in an item's thread as somebody every tier lets in.

    An administrator, so no case here is stopped by its gate before it reaches the service, and
    the callback typed once: the interaction is a stand-in and `app_commands.Command` declares its
    parameters as `...`.
    """
    interaction = FakeInteraction(user=administrator(), channel_id=THREAD, guild_id=guild_id)
    await cast(Callback, command.callback)(interaction, *options)


def workflow(name: str) -> Callable[[ShannonError], SlashCommand]:
    def build(error: ShannonError) -> SlashCommand:
        built = build_workflow_commands(
            Moves(error=error), default_gate(), FakeAccess(), FakeColumns()
        )
        return next(command for command in built if command.name == name)

    return build


async def status(error: ShannonError) -> None:
    # Typed with the spaces somebody might leave, which the line names it without.
    await fire(workflow("status")(error), "  In review ")


async def priority(error: ShannonError) -> None:
    high = next(choice for choice in PRIORITY_CHOICES if choice.value == "HIGH")
    await fire(workflow("priority")(error), high)


async def refresh(error: ShannonError) -> None:
    await fire(build_refresh_command(StubRefresh(error=error), default_gate()), None)


async def regenerate(error: ShannonError) -> None:
    await fire(build_regenerate_command(StubRegeneration(error=error), default_gate()))


async def track(error: ShannonError) -> None:
    # No kind and no state, which reads what the thread is told: the shortest way to the service.
    await fire(build_track_command(StubTracking(error=error), default_gate()), None, None)


def people(name: str) -> Runs:
    async def run(error: ShannonError) -> None:
        command = PEOPLE[name](StubAssignment(error=error), default_gate(), FakeAccess())
        await fire(command, FakeMember(id=4242))

    return run


def labels(*, removing: bool) -> Runs:
    async def run(error: ShannonError) -> None:
        build = build_unlabel_command if removing else build_label_command
        command = build(StubLabels(error=error), default_gate(), FakeAccess(), StubSuggestions())
        await fire(command, "bug")

    return run


def sync_link(build: Callable[..., SlashCommand]) -> Runs:
    async def run(error: ShannonError) -> None:
        command = build(StubManualSync(error=error), default_gate())
        await fire(command, "https://github.com/acme/widget/pull/7")

    return run


async def log_conversation(error: ShannonError) -> None:
    command = build_log_conversation_command(
        StubLog(error=error), default_gate(), FakeAccess(), capturing=True
    )
    await fire(command)


async def stop_conversation(error: ShannonError) -> None:
    await fire(build_stop_conversation_command(StubLog(error=error), default_gate()))


def board(half: str, *args: str) -> Runs:
    """`/board link` and `/board show`, which logged nothing for a refusal until #267 sent them
    through the reply table and wrote the line every other command writes."""

    async def run(error: ShannonError) -> None:
        built = Built(boards=StubBoards(error=error))
        interaction = FakeInteraction(user=administrator(), channel_id=THREAD, guild_id=1)
        await fire_half(built.sub(half), interaction, *args)

    return run


async def link_team(error: ShannonError) -> None:
    command = build_link_team_command(StubTeamLinking(error=error), default_gate())
    await fire(command, "backend", TeamRole())


async def register(error: ShannonError) -> None:
    command = build_register_command(
        StubRegistration(error=error), FakeVerification(), default_gate()
    )
    await fire(command, "https://github.com/acme/widget")


async def unregister(error: ShannonError) -> None:
    command = build_unregister_command(
        FakeUnregistration(error=error), FakeVerification(), default_gate()
    )
    await fire(command, "acme/widget")


CASES = [
    pytest.param(
        status,
        "shannon.commands.workflow",
        f"/status In review could not finish: {REFUSAL}",
        id="status",
    ),
    pytest.param(
        priority,
        "shannon.commands.workflow",
        f"/priority High priority could not finish: {REFUSAL}",
        id="priority",
    ),
    pytest.param(
        refresh, "shannon.commands.refresh", f"/refresh could not finish: {REFUSAL}", id="refresh"
    ),
    pytest.param(
        regenerate,
        "shannon.commands.regenerate",
        f"/regenerate could not finish: {REFUSAL}",
        id="regenerate",
    ),
    pytest.param(
        track,
        "shannon.commands.track",
        f"choosing which lines a thread is told failed: {REFUSAL}",
        id="track",
    ),
    *(
        pytest.param(
            people(name), "shannon.commands.people", f"/{name} could not finish: {REFUSAL}", id=name
        )
        for name in ("assign", "unassign", "request_review", "unrequest_review")
    ),
    pytest.param(
        labels(removing=False),
        "shannon.commands.labels",
        f"/label could not finish: {REFUSAL}",
        id="label",
    ),
    pytest.param(
        labels(removing=True),
        "shannon.commands.labels",
        f"/unlabel could not finish: {REFUSAL}",
        id="unlabel",
    ),
    pytest.param(
        sync_link(build_pr_command),
        "shannon.commands.sync_link",
        f"/pr could not finish: {REFUSAL}",
        id="pr",
    ),
    pytest.param(
        sync_link(build_issue_command),
        "shannon.commands.sync_link",
        f"/issue could not finish: {REFUSAL}",
        id="issue",
    ),
    pytest.param(
        log_conversation,
        "shannon.commands.conversations",
        f"/log_conversation could not finish: {REFUSAL}",
        id="log_conversation",
    ),
    pytest.param(
        stop_conversation,
        "shannon.commands.conversations",
        f"/stop_conversation could not finish: {REFUSAL}",
        id="stop_conversation",
    ),
    pytest.param(
        link_team,
        "shannon.commands.link_team",
        f"/link_team could not finish: {REFUSAL}",
        id="link_team",
    ),
    pytest.param(
        register,
        "shannon.commands.register",
        f"/register could not finish: {REFUSAL}",
        id="register",
    ),
    pytest.param(
        board("link", "3"),
        "shannon.commands.board",
        f"/board link could not finish: {REFUSAL}",
        id="board link",
    ),
    pytest.param(
        board("show"),
        "shannon.commands.board",
        f"/board show could not finish: {REFUSAL}",
        id="board show",
    ),
    pytest.param(
        unregister,
        "shannon.commands.unregister",
        f"/unregister could not finish: {REFUSAL}",
        id="unregister",
    ),
]


@pytest.mark.parametrize(("runs", "logger", "line"), CASES)
async def test_a_refusal_is_logged_once_as_a_warning(
    runs: Runs, logger: str, line: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Exactly one line, at WARNING, from the command's own module.

    The level matters as much as the words: a refusal is the command working, so ERROR would page
    somebody over a typo and INFO would hide a run of them. The logger matters because it is what
    an operator filters on.
    """
    with caplog.at_level(logging.WARNING, logger=logger):
        await runs(RepositoryMismatchError(REFUSAL))

    written = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert written == [("WARNING", line)]
    assert [record.name for record in caplog.records] == [logger]
