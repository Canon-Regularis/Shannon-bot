from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

import discord

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
    ThreadStartedEmptyError,
)

logger = logging.getLogger(__name__)

# Discord's own ceilings.
THREAD_NAME_LIMIT = 100

# Threads can only be opened in these. Any other channel type has to be refused where somebody
# is still watching, because by the time the sync path reaches it there is nobody to tell.
THREADABLE = (discord.TextChannel, discord.ForumChannel)


def why_threads_will_not_open(channel: object) -> str | None:
    """What is wrong with a channel as a home for this bot's threads, or None if nothing is.

    Asked by `/register` and `/set_channel` before either writes anything down, because that is
    the last moment somebody is looking at the answer. A channel that refuses is not found out
    until the sync path reaches it, which is hours later and behind the queue: Discord answers a
    refused create with a 400, the queue reads that as worth retrying, and the item burns sixteen
    attempts over two hours before it is dropped with one log line. Nobody is told at any point.

    A forum can be set to demand a tag on every post. Nothing here picks one, because which tag
    a pull request belongs under is the server's business and not something to guess, so a forum
    set that way is refused rather than half-supported. It is one checkbox in the channel's
    settings, and the message says so.
    """
    if not isinstance(channel, THREADABLE):
        return "Use a text or forum channel."
    if isinstance(channel, discord.ForumChannel) and channel.flags.require_tag:
        return (
            "That forum requires a tag on every post, and this bot does not set one. "
            "Turn off Require Tags in the channel's settings, or pick another channel."
        )
    return None


# The longest window Discord offers before it archives a quiet thread by itself. An archived
# thread rejects edits, and a pull request nobody discusses for a day is completely ordinary, so
# the default of one day would archive most threads while their item was still open.
ARCHIVE_AFTER_MINUTES = 10080


@dataclass(frozen=True, slots=True)
class ThreadHandle:
    thread_id: int
    message_id: int | None = None


class OpensThreads(Protocol):
    """Owning a thread's existence: opening one, rewriting it, taking it away.

    Only the code that keeps an item pointed at exactly one thread has any business here.
    """

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle: ...

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle: ...

    async def delete(self, *, thread_id: int) -> None: ...


class PostsToThread(Protocol):
    """Adding a message to a thread that already exists."""

    async def post(self, *, thread_id: int, content: str) -> int | None: ...


class LocksThread(Protocol):
    """Closing a thread to further replies, or opening it again."""

    async def set_locked(self, *, thread_id: int, locked: bool) -> None: ...


class ThreadGateway(OpensThreads, PostsToThread, LocksThread, Protocol):
    """Everything this project does to Discord threads.

    The container passes one object satisfying all three roles, because one Discord client is
    all there is. Callers name the role they use instead: the note mirror and the notifier only
    post, the sync service only locks, and only the thread binding opens or removes anything.
    Depending on the whole of this to call one method of it is how a collaborator ends up able
    to delete a thread it had no reason to touch.
    """


def truncate_thread_name(name: str) -> str:
    name = name.strip() or "Untitled"
    if len(name) <= THREAD_NAME_LIMIT:
        return name
    return name[: THREAD_NAME_LIMIT - 1] + "…"


@contextlib.contextmanager
def _translated(what: str) -> Iterator[None]:
    """Turn discord.py's exceptions into this project's, keeping the two apart.

    Forbidden is a subclass of HTTPException, so catching the general one first would file a
    missing permission as a temporary refusal and retry it for two hours.
    """
    try:
        yield
    except discord.Forbidden as exc:
        raise DiscordPermissionError(f"Discord will not let the bot {what}: {exc}") from exc
    except discord.HTTPException as exc:
        raise DiscordGatewayError(f"Discord refused to {what}: {exc}") from exc


class DiscordThreadGateway:
    """ThreadGateway on top of a live discord.py client.

    Handles both text channels and forum channels, because a server may keep pull requests in
    either and the requirements name both.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def create(self, *, channel_id: int, name: str, content: str) -> ThreadHandle:
        channel = await self._channel(channel_id)
        name = truncate_thread_name(name)

        if isinstance(channel, discord.ForumChannel):
            with _translated("create a thread"):
                created = await channel.create_thread(
                    name=name, content=content, auto_archive_duration=ARCHIVE_AFTER_MINUTES
                )
            return ThreadHandle(thread_id=created.thread.id, message_id=created.message.id)

        if isinstance(channel, discord.TextChannel):
            with _translated("create a thread"):
                thread = await channel.create_thread(
                    name=name,
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=ARCHIVE_AFTER_MINUTES,
                )

            # Opening the thread and writing in it are two calls and two permissions. Losing the
            # id here would leave the thread orphaned and have the retry open another, so it is
            # reported with the failure and recorded before anyone tries again.
            try:
                with _translated("post the first message"):
                    message = await thread.send(content)
            except DiscordGatewayError as error:
                raise ThreadStartedEmptyError(str(error), thread_id=thread.id) from error
            return ThreadHandle(thread_id=thread.id, message_id=message.id)

        raise ChannelNotFoundError(
            f"Channel {channel_id} is a {type(channel).__name__}, which cannot hold threads"
        )

    async def update(
        self, *, thread_id: int, message_id: int | None, name: str, content: str
    ) -> ThreadHandle:
        thread = await self._thread(thread_id)
        name = truncate_thread_name(name)

        with _translated("update the thread"):
            await self._wake(thread)
            # Renames are rate limited hard, so only spend one when the title actually moved.
            if thread.name != name:
                await thread.edit(name=name)
            resolved_message_id = await self._edit_or_post(thread, message_id, content)

        return ThreadHandle(thread_id=thread.id, message_id=resolved_message_id)

    async def post(self, *, thread_id: int, content: str) -> int | None:
        thread = await self._thread(thread_id)
        with _translated("post to the thread"):
            await self._wake(thread)
            message = await thread.send(content)
        return message.id

    async def set_locked(self, *, thread_id: int, locked: bool) -> None:
        thread = await self._thread(thread_id)
        if thread.locked == locked and not thread.archived:
            return

        with _translated("lock the thread" if locked else "unlock the thread"):
            # Locked but deliberately not archived. Archiving hides the thread and makes every
            # later edit fail, and a closed issue still receives label and assignment events
            # that have to reach its metadata. Unarchiving rides along with the lock change
            # rather than costing a second call. The bot needs Manage Threads to write here.
            await thread.edit(archived=False, locked=locked)

    async def delete(self, *, thread_id: int) -> None:
        """Remove a thread the sync path opened and then could not use.

        Missing it is not worth failing over: the thread it would have removed is one nobody
        is going to write to, and the sync that won has already done the useful work.
        """
        try:
            thread = await self._thread(thread_id)
            with _translated("delete the thread"):
                await thread.delete()
        except DiscordGatewayError as error:
            logger.warning("could not remove the stranded thread %s: %s", thread_id, error)

    async def _wake(self, thread: discord.Thread) -> None:
        """Unarchive before writing.

        Discord archives a thread on its own once it goes quiet, and then refuses every edit to
        it. Without this, the first event after a quiet spell fails and so does every event
        after that, which loses the item's thread for good.
        """
        if thread.archived:
            logger.info("thread %s was archived, reopening it to write", thread.id)
            await thread.edit(archived=False)

    async def _edit_or_post(
        self, thread: discord.Thread, message_id: int | None, content: str
    ) -> int:
        if message_id is not None:
            try:
                message = await thread.fetch_message(message_id)
            except discord.NotFound:
                # Someone deleted the metadata message. Post a fresh one and adopt its ID.
                logger.info("metadata message %s is gone, posting a replacement", message_id)
            else:
                await message.edit(content=content)
                return message.id

        replacement = await thread.send(content)
        return replacement.id

    def _require_a_connection(self) -> None:
        """Refuse before touching a client that has nothing behind it.

        Every operation here resolves a channel or a thread first, and both reach into the
        client's cache and then its websocket. On a client that has never connected, or one that
        has dropped, that surfaces as `AttributeError: '_MissingSentinel' object has no attribute
        'is_set'` out of discord.py's internals, which is not a `discord.HTTPException` and so
        goes straight past the translation below. The worker then retries an obscure internal
        error for two hours and writes it into `last_error` for somebody to puzzle over.

        `is_ready` is safe to ask at any point in a client's life: it checks the sentinel before
        the event. A gateway error rather than a permanent one, because a bot that has dropped
        usually comes back, and the delivery should be waiting when it does.
        """
        if not self._client.is_ready():
            raise DiscordGatewayError(
                "the Discord gateway is not connected, so nothing can be read or written yet"
            )

    async def _channel(self, channel_id: int) -> discord.abc.GuildChannel:
        self._require_a_connection()
        channel = self._client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(channel_id)
            except discord.NotFound as exc:
                raise ChannelNotFoundError(f"Channel {channel_id} is not there") from exc
            except discord.Forbidden as exc:
                raise DiscordPermissionError(
                    f"Discord will not let the bot see channel {channel_id}"
                ) from exc
            except discord.HTTPException as exc:
                raise DiscordGatewayError(
                    f"Discord refused to look up channel {channel_id}: {exc}"
                ) from exc
        return channel  # type: ignore[return-value]

    async def _thread(self, thread_id: int) -> discord.Thread:
        """Fetch a thread, keeping "it is gone" and "we are not allowed" apart.

        Callers rebuild on the first and give up on the second. Reporting a permission refusal
        as a missing thread would have a temporary loss of access delete the item's record of
        its thread and open a replacement, orphaning everything already mirrored into it.

        Everything else Discord can answer is a third thing, and it used to leave here as a raw
        discord.py exception. This is not a cold path: discord.py drops a thread from the guild
        cache the moment it archives, so the fetch is the only route to exactly the archived
        thread `_wake` exists to reopen, and a 503 lands in it. Untranslated it walked straight
        through `delete`, which suppresses this project's gateway error and documents itself as
        not worth failing over, and it reached the command replies, which match on this
        project's errors and answered "something went wrong here" for a Discord outage.
        """
        self._require_a_connection()
        channel = self._client.get_channel(thread_id)
        if channel is None:
            try:
                channel = await self._client.fetch_channel(thread_id)
            except discord.NotFound as exc:
                raise ThreadNotFoundError(f"Thread {thread_id} is not there") from exc
            except discord.Forbidden as exc:
                raise DiscordPermissionError(
                    f"Discord will not let the bot see thread {thread_id}"
                ) from exc
            except discord.HTTPException as exc:
                raise DiscordGatewayError(
                    f"Discord refused to look up thread {thread_id}: {exc}"
                ) from exc

        if not isinstance(channel, discord.Thread):
            raise ThreadNotFoundError(f"{thread_id} is not a thread")
        return channel
