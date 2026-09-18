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

# Discord's ceilings. The command one is per application and global, and this installs eighteen,
# so it is here to catch a future stage adding a hundred rather than because it is close.
MAX_COMMANDS = 100
MAX_PARAMETERS = 25
MAX_DESCRIPTION = 100

# The choice ceilings, none of which discord.py checks on the way past. `Choice.__init__` stores
# whatever it is handed: no length on the name, none on the value, no count on the list, and no
# check that two entries do not carry the same value.
#
# Worth having where the rules beside it are, because these can actually fail. A parameter
# description is run through `_shorten` before Discord ever sees it, so the assertion on that one
# above is about intent rather than about the sync; a choice is passed through whole, so one that
# is too long reaches the registration intact and the process ends without connecting.
MAX_CHOICES = 25
MAX_CHOICE_NAME = 100
MAX_CHOICE_VALUE = 100


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
    """The description half of this cannot fail, and that is worth writing down rather than
    leaving to be rediscovered. discord.py runs every parameter description through `_shorten`
    while the decorator is applied, so it is already within the limit before this reads it, and an
    undescribed parameter arrives here as a single ellipsis. The assertion states the intent; the
    name and count assertions beside it are the ones that can go red. Choices are checked below,
    where nothing shortens anything first.
    """
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


def test_every_choice_is_one_discord_will_take() -> None:
    for command in commands():
        for parameter in command.parameters:
            offered = parameter.choices
            assert len(offered) <= MAX_CHOICES, (
                f"/{command.name} {parameter.display_name} offers {len(offered)} choices"
            )
            for choice in offered:
                name = len(choice.name)
                assert 1 <= name <= MAX_CHOICE_NAME, (
                    f"/{command.name} {parameter.display_name} choice name is {name} chars"
                )
                # Every choice here is a string one, where the limit is Discord's hundred
                # characters. An integer choice would be bounded by its range instead.
                value = len(str(choice.value))
                assert 1 <= value <= MAX_CHOICE_VALUE, (
                    f"/{command.name} {parameter.display_name} choice value is {value} chars"
                )


def test_no_two_choices_on_one_option_share_a_value() -> None:
    """Discord refuses a duplicate at sync time, and it is the mistake a copied line makes: the
    name gets changed and the value does not. The picker then offers two entries that do the same
    thing, and the application never registers at all, so nothing in Discord works rather than one
    command being odd."""
    for command in commands():
        for parameter in command.parameters:
            values = [choice.value for choice in parameter.choices]
            assert len(values) == len(set(values)), (
                f"/{command.name} {parameter.display_name} offers one value twice: {values}"
            )
