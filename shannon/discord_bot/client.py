from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import discord
from discord import app_commands

from shannon.discord_bot.capture import CapturedMessage, captured, from_a_person, has_words
from shannon.discord_bot.panels import Panel
from shannon.discord_bot.responses import reply
from shannon.discord_bot.slash import SlashCommand

logger = logging.getLogger(__name__)

# Injected because the mapping knows the service errors, and nothing in this package should.
ExplainError = Callable[[BaseException], Panel]

ThreadGone = Callable[[int], Awaitable[None]]
ChannelGone = Callable[[int], Awaitable[None]]

# Deleting a channel deletes every thread in it, and discord.py dispatches an event for each at
# once, each a transaction against the pool the delivery worker and every slash command share:
# nine hundred threads made an ordinary query wait sixteen seconds.
LETTING_GO_AT_ONCE = 2
# Separate from the housekeeping limit above so capture cannot queue behind a channel deletion,
# and bounded because it is still a write per message in an armed thread.
TRANSCRIBING_AT_ONCE = 8


class CapturesMessages(Protocol):
    """What capturing a thread needs of the store that holds the transcripts.

    Named here rather than imported: which table holds a thread id is not this package's business.
    `is_logging` is synchronous because `on_message` asks it of every message in the server, and a
    database answer would scan a column that carries no index on purpose.
    """

    def is_logging(self, thread_id: int) -> bool: ...

    def nothing_to_capture(self, thread_id: int) -> None: ...

    async def capture(self, message: CapturedMessage) -> None: ...

    async def forget(self, message_ids: Sequence[int]) -> None: ...


def build_intents(*, capture_messages: bool = False) -> discord.Intents:
    """The gateway intents this bot asks Discord for.

    `message_content` is privileged: a Developer Portal toggle, and Discord's approval past a
    hundred servers, which is why it is behind a setting. Without it every message arrives with
    `content` empty. `members` is not needed, since nothing here looks a member up, and would set
    `chunk_guilds_at_startup`, delaying the READY the delivery worker waits on.
    """
    intents = discord.Intents.default()
    intents.message_content = capture_messages
    return intents


class ShannonBot(discord.Client):
    """Slash commands only, so a bare Client with a command tree is enough.

    Commands are handed in rather than built here: they need services that need this client, so it
    has to exist first.
    """

    def __init__(
        self,
        *,
        explain_error: ExplainError,
        thread_gone: ThreadGone | None = None,
        capture_messages: bool = False,
    ) -> None:
        # GitHub bodies are mirrored verbatim, so `everyone` stays off. Roles stay on for
        # team-review mentions, and `defuse_mentions` neutralises every mention GitHub wrote.
        # `everyone=False` also takes @everyone out of the per-message allow-lists `/mentions`
        # writes: theirs leaves it at a truthy sentinel that discord.py merges over this.
        super().__init__(
            intents=build_intents(capture_messages=capture_messages),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=True, users=True, replied_user=False
            ),
        )
        self.tree = app_commands.CommandTree(self)
        # Assignment is how discord.py documents installing this, and mypy has no way to say so.
        self.tree.on_error = self._command_failed  # type: ignore[method-assign]
        self._explain_error = explain_error
        self._thread_gone = thread_gone
        self._channel_gone: ChannelGone | None = None
        self._letting_go = asyncio.Semaphore(LETTING_GO_AT_ONCE)
        self._capturing: CapturesMessages | None = None
        self._transcribing = asyncio.Semaphore(TRANSCRIBING_AT_ONCE)
        self._pending: list[SlashCommand] = []
        # `is_ready` cannot answer this: it reports whether the cache has ever been filled, is set
        # once when READY arrives and cleared only by `close`, so a connection that came up and
        # later died still reads as ready.
        self._connected = False

    async def _command_failed(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """An unanswered interaction leaves the person with a spinner until Discord gives up."""
        logger.error("a slash command failed", exc_info=error)
        # An error handler that raises is worse than not having one: discord.py logs a second
        # traceback and the person is still left waiting. The interaction may also have expired
        # or already been answered, and neither is worth a stack trace.
        with contextlib.suppress(discord.HTTPException):
            await reply(interaction, self._explain_error(error))

    def install(self, *commands: SlashCommand) -> None:
        self._pending.extend(commands)

    def tell_when_a_channel_goes(self, gone: ChannelGone) -> None:
        self._channel_gone = gone

    def tell_when_a_message_arrives(self, capturing: CapturesMessages) -> None:
        self._capturing = capturing

    def tell_when_a_thread_goes(self, gone: ThreadGone) -> None:
        self._thread_gone = gone

    async def setup_hook(self) -> None:
        """Register the installed commands with Discord, once, at startup.

        A global sync rather than a per-guild one: this bot is invited to a server rather than built
        into one. A per-guild sync appears at once and is what to reach for while developing.
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
        self._connected = True
        logger.info("connected to Discord as %s", self.user)

    async def on_resumed(self) -> None:
        """A reconnection that resumes an existing session sends this and no READY.

        Watching only for READY would report a gateway that is up as down.
        """
        self._connected = True
        logger.info("the connection to Discord came back")

    async def on_disconnect(self) -> None:
        """discord.py reconnects for ever by design, so this is not an error.

        It is recorded because a gateway that has stopped answering leaves the task running and the
        cache filled, so the health check has no other way to know.
        """
        self._connected = False
        logger.info("lost the connection to Discord, waiting for it to come back")

    def gateway_is_up(self) -> bool:
        """Whether Discord can be reached right now, for the health check to answer with.

        A reconnection in progress reads as down while it lasts, which the health check's own start
        period and retries cover.
        """
        return self._connected and self.is_ready()

    async def on_message(self, message: discord.Message) -> None:
        """One of these for every message in every channel of every server this bot is in.

        The order of the checks below is the design: the set lookup rules out almost everything. A
        dropped transcript line is gone and nothing rebuilds it, so a failure here is logged rather
        than suppressed as it is in the handlers below.
        """
        if self._capturing is None:
            return
        if not self._capturing.is_logging(message.channel.id):
            return
        if not from_a_person(message):
            return
        if not has_words(message):
            self._capturing.nothing_to_capture(message.channel.id)
            return

        async with self._transcribing:
            try:
                await self._capturing.capture(captured(message))
            except Exception:
                logger.exception(
                    "could not keep a message from thread %s, so it will not be published",
                    message.channel.id,
                )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Drop a message somebody deleted before it was published.

        Edits are deliberately not honoured: whether one landed would depend on whether the quiet
        gap elapsed first. The raw event rather than `on_message_delete`, which discord.py
        dispatches only for a message it still has cached; nothing here is cached.
        """
        await self._forget(payload.channel_id, [payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """A bulk delete sends this and no per-message deletion.

        Without it a moderator clearing a stretch of a thread would leave every message in it still
        queued to publish.
        """
        await self._forget(payload.channel_id, sorted(payload.message_ids))

    async def _forget(self, channel_id: int, message_ids: Sequence[int]) -> None:
        if self._capturing is None:
            return
        if not self._capturing.is_logging(channel_id):
            return
        async with self._transcribing:
            try:
                await self._capturing.forget(message_ids)
            except Exception:
                logger.exception(
                    "could not drop deleted messages from thread %s, so they may still publish",
                    channel_id,
                )

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """A channel has gone, and with it every thread that was in it.

        The per-thread events Discord sends alongside this cover only the threads discord.py still
        had cached, and it drops one the moment the thread archives, which Discord does by itself
        after a few days of quiet. So a quiet thread is announced by nothing else.
        """
        if self._channel_gone is None:
            return
        async with self._letting_go:
            with contextlib.suppress(Exception):
                await self._channel_gone(channel.id)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        """Let go of a thread somebody removed, before anything writes to it again.

        A pull request or an issue is told again by its next webhook, and the write path turns the
        refusal into a replacement thread. A draft card on a project board has no webhook: the
        poller sees a row still holding a thread id and passes over the card without a Discord call,
        so a card parked in Done is mirrored nowhere, permanently, and nothing says so.
        """
        if self._thread_gone is None:
            return
        async with self._letting_go:
            with contextlib.suppress(Exception):
                await self._thread_gone(payload.thread_id)
