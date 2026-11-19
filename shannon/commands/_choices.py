"""What Discord accepts in an autocomplete picker, written down once. Issue #267.

`/label`, `/unlabel`, `/board link` and the workflow commands each offer a picker. Each used to
carry its own copy of these limits and of the type, with its own comment on the same Discord
rule. What a module does with a limit stays beside its own use.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import discord
from discord import app_commands

# Discord's caps on one picker. A longer list, or a choice whose name or value is longer, is sent
# back as an error rather than cut short, and the whole picker then shows nothing.
MOST_CHOICES = 25
MOST_CHARACTERS = 100

# What discord.py wants of an autocomplete: a coroutine taking the interaction and what has been
# typed so far. The two `Any`s are the coroutine's send and throw types, which nothing supplies.
Suggesting = Callable[
    [discord.Interaction, str], Coroutine[Any, Any, list[app_commands.Choice[str]]]
]
