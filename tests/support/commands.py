"""The commands a bot installs, as somebody in Discord can actually run them."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from discord import app_commands


def runnable(installed: Iterable[object]) -> Iterator[app_commands.Command[Any, ..., Any]]:
    """Every command somebody can run: each flat one, and each subcommand of a group.

    A group itself is not one of them. Discord will not run `/board` bare, so a sentence naming it
    alone names nothing anybody can type - and a group has no parameters to check, so a test that
    read them off every installed entry would raise on the first group rather than check anything.
    Issue #201 made `/board` the first.
    """
    for one in installed:
        if isinstance(one, app_commands.Group):
            yield from (
                command
                for command in one.walk_commands()
                if isinstance(command, app_commands.Command)
            )
        elif isinstance(one, app_commands.Command):
            yield one
