"""The type of a slash command, written down once.

`app_commands.Command` takes three parameters, and the first is whatever the command is bound to:
discord.py bounds it to `Group | Cog`. Every command in this project is a plain module-level
function bound to neither, so there is nothing truthful to put in that slot. `Group` would claim a
binding that does not exist, and `object` does not satisfy the bound.

So this is the one place in the package where `Any` is the honest answer rather than a gap, and it
is said once here instead of seventeen times across the command modules.

The second parameter is the callback's own signature, which pyright already collapses to `...`, and
the third is what the callback returns, which for every command here is nothing.
"""

from __future__ import annotations

from typing import Any

from discord import app_commands

SlashCommand = app_commands.Command[Any, ..., None]
