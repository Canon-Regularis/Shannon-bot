"""The type of a slash command, written down once.

The first parameter of `app_commands.Command` is whatever the command is bound to, and discord.py
bounds it to `Group | Cog`. Every command here is a module-level function bound to neither, so
`Group` would claim a binding that does not exist and `object` does not satisfy the bound.

A command with subcommands under it is a `Group`, which is not a `Command` at all: it has no
parameters and no callback of its own, and Discord will not run one bare. So what the tree is
handed is one or the other. Issue #201 made `/board` the first of those.
"""

from __future__ import annotations

from typing import Any

from discord import app_commands

SlashCommand = app_commands.Command[Any, ..., None]

SlashGroup = app_commands.Group

# Whatever the bot installs: a command, or a group of them. Each is one entry against Discord's
# limit of a hundred, however many subcommands a group holds.
Installable = SlashCommand | SlashGroup
