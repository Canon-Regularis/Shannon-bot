"""The type of a slash command, written down once.

The first parameter of `app_commands.Command` is whatever the command is bound to, and discord.py
bounds it to `Group | Cog`. Every command here is a module-level function bound to neither, so
`Group` would claim a binding that does not exist and `object` does not satisfy the bound.
"""

from __future__ import annotations

from typing import Any

from discord import app_commands

SlashCommand = app_commands.Command[Any, ..., None]
