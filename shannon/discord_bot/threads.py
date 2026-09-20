from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Protocol

import discord
from discord import ui

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
    ThreadStartedEmptyError,
)
from shannon.discord_bot.layout import as_message
from shannon.discord_bot.panels import Panel

logger = logging.getLogger(__name__)

# Who one message is allowed to notify, as Discord account ids. None means the caller has no
# opinion and the client's own `AllowedMentions` applies untouched; an empty sequence means
# nobody may be notified. Both are falsy, so every check here is `is None` and never `if notify`.
Notify = Sequence[int] | None

# Discord's own ceiling.
THREAD_NAME_LIMIT = 100

# The only channel types Discord will open a thread in.
THREADABLE = (discord.TextChannel, discord.ForumChannel)


def _said(content: str | None, view: ui.LayoutView | None) -> dict[str, object]:
    """Discord refuses a message with components beside content, so exactly one of these is set."""
    return {"content": content} if view is None else {"view": view}


def _may_notify(notify: Notify) -> dict[str, discord.AllowedMentions]:
    """No keyword rather than a permissive object.

    discord.py merges a per-call value over the client's and has no value meaning "leave it alone",
    so `users=True` would override a client default that had since changed. Only `users` is set,
    because un-merged this object's `everyone` is discord.py's truthy `default` sentinel and the
    payload would carry `parse: ['everyone', 'roles']`; the merge against the client's own strips it
    back out. The ids are wrapped because `AllowedMentions.to_dict` reads `.id` off each entry, so a
    bare int raises `AttributeError` inside discord.py before any request is made, which is not an
    `HTTPException` and walks past the translation below.
    """
    if notify is None:
        return {}
    return {
        "allowed_mentions": discord.AllowedMentions(
            users=[discord.Object(id=user_id) for user_id in notify]
        )
    }


def why_threads_will_not_open(channel: object) -> str | None:
    """What is wrong with a channel as a home for this bot's threads, or None if nothing is.

    Asked by `/register` and `/set_channel` before either writes anything down: found out at sync
    time instead, a refused create is a 400 the queue retries for two hours and then drops.
    """
    if not isinstance(channel, THREADABLE):
        return "Use a text or forum channel."
    if isinstance(channel, discord.ForumChannel) and channel.flags.require_tag:
        return (
            "That forum requires a tag on every post, and this bot does not set one. "
            "Turn off Require Tags in the channel's settings, or pick another channel."
        )
    return _missing_thread_permission(channel)


def _missing_thread_permission(channel: discord.abc.GuildChannel) -> str | None:
    """The thread permissions this bot has not been given in a channel, or None if it has them all.

    A missing permission is permanent, so a channel accepted without one refuses every delivery
    afterwards: the queue drops each on its first attempt with a log line nobody reads, and the
    person who ran the command was told it worked. Asked of the bot rather than of the caller,
    because Discord will offer an administrator a channel this bot cannot see. Manage Threads shuts
    a finished item's thread and reopens one for a late comment, and the runtime path steps over a
    refusal so the mirror survives it, so a server without it would notice only that nothing had
    closed; the closing header says so too, for a permission taken away later. Nothing is checked
    where the guild is not cached, which is a client still starting.
    """
    me = getattr(getattr(channel, "guild", None), "me", None)
    if me is None:
        return None

    allowed = channel.permissions_for(me)
    if isinstance(channel, discord.ForumChannel):
        # A forum post is a thread, and creating one is Send Messages in the forum itself.
        wanted = (
            ("View Channel", allowed.view_channel),
            ("Send Messages", allowed.send_messages),
            ("Send Messages in Threads", allowed.send_messages_in_threads),
            ("Manage Threads", allowed.manage_threads),
        )
    else:
        # Opening the thread and writing in it are separate permissions in a text channel.
        wanted = (
            ("View Channel", allowed.view_channel),
            ("Create Public Threads", allowed.create_public_threads),
            ("Send Messages in Threads", allowed.send_messages_in_threads),
            ("Manage Threads", allowed.manage_threads),
        )

    missing = [name for name, held in wanted if not held]
    if not missing:
        return None
    return f"This bot has not been given {', '.join(missing)} there."


# The longest window Discord offers before it archives a quiet thread by itself. Its default of
# one day would archive most threads while their item was still open.
ARCHIVE_AFTER_MINUTES = 10080


@dataclass(frozen=True, slots=True)
class ThreadHandle:
    thread_id: int
    message_id: int | None = None


class OpensThreads(Protocol):
    async def create(
        self, *, channel_id: int, name: str, panel: Panel, notify: Notify = None
    ) -> ThreadHandle: ...

    async def update(
        self,
        *,
        thread_id: int,
        message_id: int | None,
        name: str,
        panel: Panel,
        notify: Notify = None,
    ) -> ThreadHandle: ...

    async def delete(self, *, thread_id: int) -> None: ...


class PostsToThread(Protocol):
    async def post(self, *, thread_id: int, panel: Panel, notify: Notify = None) -> int | None: ...


class ShutsThread(Protocol):
    """Shut means locked against replies and archived out of the channel, in one edit."""

    async def set_shut(self, *, thread_id: int, shut: bool) -> None: ...


class FindsThreads(Protocol):
    """Which channel a thread is in.

    The row records it too and is the cheaper answer, so this is for rows written before it did.
    """

    async def channel_of(self, *, thread_id: int) -> int | None: ...


class KnowsItsServers(Protocol):
    def is_in(self, guild_id: int) -> bool: ...


class ThreadGateway(
    OpensThreads, PostsToThread, ShutsThread, FindsThreads, KnowsItsServers, Protocol
):
    """Everything this project does to Discord threads, in one object.

    One Discord client is all there is; callers name the roles they actually use.
    """


def truncate_thread_name(name: str) -> str:
    name = name.strip() or "Untitled"
    if len(name) <= THREAD_NAME_LIMIT:
        return name
    return name[: THREAD_NAME_LIMIT - 1] + "…"


@contextlib.contextmanager
def _translated(what: str) -> Iterator[None]:
    """Discord's refusals as this project's errors, named by what the bot was trying to do.

    Forbidden is a subclass of HTTPException, so catching the general one first would file a missing
    permission as a temporary refusal and retry it for two hours.
    """
    try:
        yield
    except discord.Forbidden as exc:
        raise DiscordPermissionError(f"Discord will not let the bot {what}: {exc}") from exc
    except discord.HTTPException as exc:
        raise DiscordGatewayError(f"Discord refused to {what}: {exc}") from exc


class DiscordThreadGateway:
    """ThreadGateway on top of a live discord.py client."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def create(
        self, *, channel_id: int, name: str, panel: Panel, notify: Notify = None
    ) -> ThreadHandle:
        channel = await self._channel(channel_id)
        name = truncate_thread_name(name)
        content, view = as_message(panel)

        if isinstance(channel, discord.ForumChannel):
            with _translated("create a thread"):
                created = await channel.create_thread(
                    name=name,
                    auto_archive_duration=ARCHIVE_AFTER_MINUTES,
                    **_said(content, view),
                    **_may_notify(notify),
                )
            return ThreadHandle(thread_id=created.thread.id, message_id=created.message.id)

        if isinstance(channel, discord.TextChannel):
            with _translated("create a thread"):
                thread = await channel.create_thread(
                    name=name,
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=ARCHIVE_AFTER_MINUTES,
                )

            # Opening the thread and writing in it are two calls. Losing the id here would
            # strand the thread and have the retry open another, so it travels with the failure.
            try:
                with _translated("post the first message"):
                    # `TextChannel.create_thread` takes neither the content nor an allow-list:
                    # it opens an empty thread and the first message is a separate call.
                    message = await thread.send(**_said(content, view), **_may_notify(notify))
            except DiscordGatewayError as error:
                raise ThreadStartedEmptyError(str(error), thread_id=thread.id) from error
            return ThreadHandle(thread_id=thread.id, message_id=message.id)

        raise ChannelNotFoundError(
            f"Channel {channel_id} is a {type(channel).__name__}, which cannot hold threads"
        )

    async def update(
        self,
        *,
        thread_id: int,
        message_id: int | None,
        name: str,
        panel: Panel,
        notify: Notify = None,
    ) -> ThreadHandle:
        thread = await self._thread(thread_id)
        name = truncate_thread_name(name)

        with _translated("update the thread"):
            await self._wake(thread)
            # Renames are rate limited hard, so only spend one when the title actually moved.
            if thread.name != name:
                await thread.edit(name=name)
            resolved_message_id = await self._edit_or_post(thread, message_id, panel, notify)

        return ThreadHandle(thread_id=thread.id, message_id=resolved_message_id)

    async def post(self, *, thread_id: int, panel: Panel, notify: Notify = None) -> int | None:
        content, view = as_message(panel)
        thread = await self._thread(thread_id)
        with _translated("post to the thread"):
            await self._wake(thread)
            message = await thread.send(**_said(content, view), **_may_notify(notify))
        return message.id

    async def set_shut(self, *, thread_id: int, shut: bool) -> None:
        thread = await self._thread(thread_id)
        if thread.locked == shut and thread.archived == shut:
            return

        with _translated("close the thread" if shut else "reopen the thread"):
            # Both halves in one PATCH, so no attempt can leave a thread archived and
            # unlocked: anybody may reopen an unlocked thread and the first reply would, while
            # the row went on saying shut. Manage Threads is needed for this and for the reopen.
            await thread.edit(archived=shut, locked=shut)

    async def channel_of(self, *, thread_id: int) -> int | None:
        """Which channel a thread is actually in, or None if it is not there any more.

        Gone is an ordinary answer and the caller drops the pointer; a refusal or an outage still
        raises, because read as gone either would open a second thread beside a working one.
        """
        try:
            thread = await self._thread(thread_id)
        except ThreadNotFoundError:
            return None
        return thread.parent_id

    def is_in(self, guild_id: int) -> bool:
        """Asked when Discord has refused something as though a permission were missing.

        discord.py empties the guild from its cache the moment the bot is removed, and a channel it
        can no longer see refuses exactly like one it is not allowed to touch. A guild not yet
        cached, during a start or a reconnect, answers False too, and waiting suits both.
        """
        self._require_a_connection()
        return self._client.get_guild(guild_id) is not None

    async def delete(self, *, thread_id: int) -> None:
        """Remove a thread the sync path opened and then could not use.

        A failure here is not worth raising: nobody is going to write to that thread, and the
        sync that won has already done the useful work.
        """
        try:
            thread = await self._thread(thread_id)
            with _translated("delete the thread"):
                await thread.delete()
        except DiscordGatewayError as error:
            logger.warning("could not remove the stranded thread %s: %s", thread_id, error)

    async def _wake(self, thread: discord.Thread) -> None:
        """Discord archives a thread on its own once it goes quiet and then refuses every edit.

        Without this the first event after a quiet spell fails and so does every event after it.
        """
        if thread.archived:
            logger.info("thread %s was archived, reopening it to write", thread.id)
            await thread.edit(archived=False)

    async def _edit_or_post(
        self, thread: discord.Thread, message_id: int | None, panel: Panel, notify: Notify = None
    ) -> int:
        content, view = as_message(panel)
        if message_id is not None:
            try:
                message = await thread.fetch_message(message_id)
            except discord.NotFound:
                logger.info("metadata message %s is gone, posting a replacement", message_id)
            else:
                # An edit notifies nobody whatever it says, so the allow-list changes nothing.
                # Nulling `embed` and `attachments` is what discord.py requires to attach a view
                # to a message without one, and a link preview left in would refuse the edit.
                await message.edit(
                    content=content, embed=None, attachments=[], view=view, **_may_notify(notify)
                )
                return message.id

        # A new message does notify, unlike the edit above, which is why `update` takes an
        # allow-list at all.
        replacement = await thread.send(**_said(content, view), **_may_notify(notify))
        return replacement.id

    def _require_a_connection(self) -> None:
        """Nothing is looked up on a client that is not connected.

        On a client that has never connected, or one that has dropped, resolving a channel or a
        thread raises `AttributeError: '_MissingSentinel' object has no attribute 'is_set'` out of
        discord.py's internals, which is not a `discord.HTTPException` and so goes past the
        translation below to be retried for two hours. `is_ready` is safe to ask at any point in a
        client's life: it checks the sentinel before the event. A gateway error rather than a
        permanent one, because a bot that has dropped usually comes back.
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
        """The thread behind an id, keeping a refusal, an outage and a missing thread apart.

        Callers rebuild on a missing thread and give up on a refusal, so reporting a refusal as
        missing would have a temporary loss of access delete the item's record of its thread and
        orphan everything already mirrored into it. discord.py drops a thread from the guild cache
        the moment it archives, so the fetch is the only route to the archived thread `_wake` exists
        to reopen, and a 503 lands in it: raw, it passes through `delete`, which suppresses this
        project's gateway error, and reaches the command replies, which match on this project's
        errors.
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
