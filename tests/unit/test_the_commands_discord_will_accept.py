"""What Discord checks when the commands are registered, checked here instead of on first run.

`setup_hook` calls `tree.sync()` before the gateway connects, and Discord validates every name and
description at that moment. A command that breaks one of its rules does not fail quietly and does
not fail late: the sync raises, `setup_hook` raises, `start()` raises, and the process ends without
ever connecting. Nothing else in this suite would notice, because every test of a command drives
the callback directly and the fake gateway never syncs anything.

So this is the one test standing between a rename and a bot that will not boot. It builds the real
commands the container installs and holds them to the rules Discord documents.
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import create_async_engine

from shannon.config import Settings
from shannon.container import build_container
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway

# Discord's own rule for a chat input command, narrowed to what this project actually uses. The
# real pattern admits several non-Latin scripts; nothing here needs them, and a name that would
# rely on that is worth failing on so somebody reads this comment.
NAME = re.compile(r"^[-_a-z0-9]{1,32}$")

# Discord's ceilings. The command one is per application and global, and this installs fourteen,
# so it is here to catch a future stage adding a hundred rather than because it is close.
MAX_COMMANDS = 100
MAX_PARAMETERS = 25
MAX_DESCRIPTION = 100


def commands():
    """The commands the container really installs.

    Built against an engine that is never connected to: `create_async_engine` opens nothing, and
    wiring is all this needs, so the check stays in the tier that runs without a database.
    """
    container = build_container(
        threads=FakeThreadGateway(),
        settings=Settings(github_webhook_secret="x"),
        engine=create_async_engine("postgresql+asyncpg://nobody@localhost/nothing"),
        github=FakeGitHubClient(),
    )
    return container.commands


def test_every_command_name_is_one_discord_will_take() -> None:
    for command in commands():
        assert NAME.match(command.name), f"/{command.name} is not a name Discord accepts"


def test_every_description_fits_and_is_not_empty() -> None:
    """An empty description is refused outright, and an over-long one takes the sync down with
    it, so the whole application fails to register over one command's help text."""
    for command in commands():
        length = len(command.description)
        assert 1 <= length <= MAX_DESCRIPTION, f"/{command.name} description is {length} chars"


def test_every_parameter_is_one_discord_will_take() -> None:
    for command in commands():
        assert len(command.parameters) <= MAX_PARAMETERS, f"/{command.name} has too many options"
        for parameter in command.parameters:
            assert NAME.match(parameter.name), f"/{command.name} {parameter.name} is not a name"
            length = len(parameter.description)
            assert 1 <= length <= MAX_DESCRIPTION, (
                f"/{command.name} {parameter.name} description is {length} chars"
            )


def test_no_two_commands_share_a_name() -> None:
    """A duplicate is not caught by the tree, which keeps the last one, and not by any test that
    drives a callback directly. It is caught by Discord, at the moment it is too late."""
    names = [command.name for command in commands()]

    assert len(names) == len(set(names)), f"two commands answer to one name: {sorted(names)}"


def test_there_are_not_more_commands_than_discord_will_register() -> None:
    assert len(commands()) <= MAX_COMMANDS
