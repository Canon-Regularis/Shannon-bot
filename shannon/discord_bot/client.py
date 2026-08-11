from __future__ import annotations

import logging

import discord
from discord import app_commands

logger = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    # Members are needed to turn a GitHub login into someone this server can actually ping.
    intents.members = True
    return intents


class ShannonBot(discord.Client):
    """Slash commands only, so a bare Client with a command tree is enough.

    Commands are handed in rather than built here. The thread gateway needs a live client and
    the commands need services that need that gateway, so the client has to exist before the
    things that use it. Composition is the container's job.
    """

    def __init__(self) -> None:
        # GitHub comment bodies are mirrored verbatim, so a comment containing @everyone would
        # otherwise ping the whole server. Only the user mentions this bot builds itself are
        # allowed to resolve.
        super().__init__(
            intents=build_intents(),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True, replied_user=False
            ),
        )
        self.tree = app_commands.CommandTree(self)
        self._pending: list[app_commands.Command] = []

    def install(self, *commands: app_commands.Command) -> None:
        self._pending.extend(commands)

    async def setup_hook(self) -> None:
        for command in self._pending:
            self.tree.add_command(command)
        await self.tree.sync()
        logger.info("synced %s slash commands", len(self._pending))

    async def on_ready(self) -> None:
        logger.info("connected to Discord as %s", self.user)
