from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable

import discord
from discord import app_commands

from shannon.discord_bot.responses import reply

logger = logging.getLogger(__name__)

# Turns whatever a command raised into something worth showing the person who ran it. Injected
# because the mapping knows the service errors, and nothing in this package should.
ExplainError = Callable[[BaseException], str]


def build_intents() -> discord.Intents:
    """What this bot needs off the gateway, which is nothing privileged.

    It used to ask for `members`, on the stated grounds that turning a GitHub login into
    somebody this server can ping needed it. It does not. A mention is a `<@id>` string built
    from a row in `user_links`, and Discord resolves it on receipt; nothing here ever looks a
    member up. The one thing that reads a member is the permission gate, and what it reads is
    `interaction.user`, which discord.py builds from the interaction payload and whose roles
    resolve against the guild role cache that arrives under `guilds`.

    Asking for it anyway was not free. It is privileged, so it is a Developer Portal toggle
    that stops the process starting at all when it is missed, and it needs Discord's approval
    past a hundred servers. discord.py also reads it as a request to chunk: `members` on turns
    `chunk_guilds_at_startup` on, so the whole member list of every server is pulled over the
    gateway before READY fires and then kept in memory, and READY is what the worker waits for
    before it will deliver anything.
    """
    return discord.Intents.default()


class ShannonBot(discord.Client):
    """Slash commands only, so a bare Client with a command tree is enough.

    Commands are handed in rather than built here. The thread gateway needs a live client and
    the commands need services that need that gateway, so the client has to exist before the
    things that use it. Composition is the container's job.
    """

    def __init__(self, *, explain_error: ExplainError) -> None:
        # GitHub comment bodies are mirrored verbatim, so a comment containing @everyone would
        # otherwise ping the whole server.
        #
        # Roles are allowed because a review asked of a GitHub team is announced as a mention of
        # the Discord role somebody linked to it, and a role that cannot resolve is a ping that
        # reaches nobody. That is safe here and not merely tolerable: every scrap of
        # GitHub-authored text goes through `defuse_mentions` on the way in, which puts a
        # zero-width space inside the brackets of `<@&123>` as well as `<@123>`, so the only live
        # mentions in any message this bot sends are the ones it built. `everyone` stays off,
        # because nothing this bot builds is ever addressed to everyone.
        super().__init__(
            intents=build_intents(),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=True, users=True, replied_user=False
            ),
        )
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = self._command_failed
        self._explain_error = explain_error
        self._pending: list[app_commands.Command] = []

    async def _command_failed(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Answer an interaction whose command raised something it did not expect.

        Each command handles the errors it knows about. Anything else, a dropped database
        connection or a plain bug, would otherwise leave the person who ran it looking at a
        spinner until Discord gave up, with the reason only in the log.
        """
        logger.error("a slash command failed", exc_info=error)
        # An error handler that raises is worse than not having one: discord.py logs a second
        # traceback and the person who ran the command is still left waiting. The interaction
        # may also have expired, or already been answered, and neither is worth a stack trace.
        with contextlib.suppress(discord.HTTPException):
            await reply(interaction, self._explain_error(error))

    def install(self, *commands: app_commands.Command) -> None:
        self._pending.extend(commands)

    async def setup_hook(self) -> None:
        """Register the commands with Discord, once, before the gateway connects.

        A global sync rather than a per-guild one, because this bot is invited to a server rather
        than built into one and a global command works wherever it is invited. The cost is worth
        saying out loud in the log: Discord serves global commands from a cache and can take up to
        an hour to show a new one, so the first start of a fresh application looks exactly like a
        broken one. A per-guild sync appears at once and is the thing to reach for while
        developing.
        """
        for command in self._pending:
            self.tree.add_command(command)
        await self.tree.sync()
        logger.info(
            "registered %s slash commands with Discord; a global sync can take up to an hour to "
            "appear in a server, so they may not be typeable yet",
            len(self._pending),
        )

    async def on_ready(self) -> None:
        logger.info("connected to Discord as %s", self.user)
