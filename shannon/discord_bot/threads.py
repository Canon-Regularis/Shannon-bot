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

# Who one message is allowed to notify, as Discord account ids.
#
# None means the caller has no opinion, and the client's own `AllowedMentions` applies untouched.
# An empty sequence means nobody may be notified. Those are different answers and both are falsy,
# so everything here asks `is None` and never `if notify`. Getting that one character wrong turns
# "notify nobody" into "notify everybody named", silently, which is this whole feature inverted.
Notify = Sequence[int] | None

# Discord's own ceilings.
THREAD_NAME_LIMIT = 100

# Threads can only be opened in these. Any other channel type has to be refused where somebody
# is still watching, because by the time the sync path reaches it there is nobody to tell.
THREADABLE = (discord.TextChannel, discord.ForumChannel)


def _said(content: str | None, view: ui.LayoutView | None) -> dict[str, object]:
    """The one of the two a message may carry, as the keyword to pass.

    Discord refuses a message holding components beside content, so exactly one of these is ever
    set. Built as a splat for the same reason `_may_notify` is: the alternative is every send site
    branching on panel shape, and there are nine of them.
    """
    return {"content": content} if view is None else {"view": view}


def _may_notify(notify: Notify) -> dict[str, discord.AllowedMentions]:
    """The `allowed_mentions` keyword for one write, or no keyword at all.

    Nothing rather than a permissive object, because discord.py merges a per-call value over the
    client's and there is no value meaning "leave it alone": `users=True` reads as a decision to
    notify everybody named, and would override a client default that had since changed.

    Only `users` is set, so `everyone=False` and `roles=True` keep coming from the client. That is
    load-bearing rather than tidy. Un-merged, this object's `everyone` is discord.py's `default`
    sentinel, which is truthy, so its payload carries `parse: ['everyone', 'roles']`; it is the
    merge against the client's own that strips `everyone` back out. A client built without an
    `allowed_mentions` of its own would have every message written here permit `@everyone`.

    The ids are wrapped rather than passed as numbers. `AllowedMentions.to_dict` reads `.id` off
    each entry, so a bare int raises `AttributeError` from inside discord.py's payload builder
    before any request is made. That is not an `HTTPException`, so it walks past the translation
    below and out of this module raw, and the worker then retries it for two hours: the same shape
    of failure `_require_a_connection` exists to prevent.
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
    return _missing_thread_permission(channel)


def _missing_thread_permission(channel: discord.abc.GuildChannel) -> str | None:
    """Which of the permissions needed to open a thread here this bot has not been given.

    The same reasoning as the checks above, for the thing that actually goes wrong most often. A
    channel this bot cannot write in is accepted, written down, and then refuses every delivery
    for the rest of its life: a missing permission is permanent, so the queue drops each one on
    its first attempt with a line in a log nobody is reading, and the person who ran the command
    was told it worked. Private channels and a role that was never given Create Public Threads
    are the ordinary ways in, and both are invisible from the command's side.

    Asked of the bot rather than of the caller. An administrator picking a channel they can see
    says nothing about whether this bot can, and Discord will happily offer one it cannot.

    Manage Threads is required, and it is the one here that costs a working feature rather than
    the whole mirror. It is what shuts a finished item's thread, and what reopens one afterwards
    to write a late comment into it, so without it every item that closes leaves its thread in
    the channel for ever. Asked for at this door because it is the last moment anybody is
    looking: the runtime path steps over a refusal so the mirror survives one, which means a
    server that skipped it would never find out except by noticing nothing had closed.

    That leaves a gap this cannot close, and the closing header carries the other half. A
    repository registered before this check existed never passed it, and the permission can be
    taken away afterwards, so a refusal still has to say so where somebody is reading.

    Skipped where the guild is not cached, which is a client that is still starting: the answer
    would be a guess, and the checks above are the ones this exists for.
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
        # A text channel needs the thread opened and then written in, which are two separate
        # permissions and are refused separately.
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
    """Adding a message to a thread that already exists."""

    async def post(self, *, thread_id: int, panel: Panel, notify: Notify = None) -> int | None: ...


class ShutsThread(Protocol):
    """Shutting a finished item's thread, or giving it back.

    Shut means locked against replies and archived out of the channel, which is what Discord's
    own client calls closing a thread. One verb rather than two on purpose: the two halves go in
    one edit, so they cannot end up disagreeing. Archived without the lock is the pairing that
    matters, because anybody may reopen an unlocked thread and the first reply would do it,
    silently, under a block saying the item is finished.
    """

    async def set_shut(self, *, thread_id: int, shut: bool) -> None: ...


class FindsThreads(Protocol):
    """Where a thread actually is, as opposed to where the mapping says new ones should go.

    Its own role because it is the only question in this module answered by reading Discord rather
    than writing to it, and because one caller has any business asking it. The row remembers this
    too, and is the cheaper answer where it has one; this is for the rows written before it did.
    """

    async def channel_of(self, *, thread_id: int) -> int | None: ...


class KnowsItsServers(Protocol):
    """Whether this bot is in a particular server at the moment.

    Its own role because it is the only question here that is not about a thread. Both callers
    ask it for the same reason and about the same moment: a refusal has come back that reads as a
    permission, and being out of the server answers exactly the same way.
    """

    def is_in(self, guild_id: int) -> bool: ...


class ThreadGateway(
    OpensThreads, PostsToThread, ShutsThread, FindsThreads, KnowsItsServers, Protocol
):
    """Everything this project does to Discord threads.

    The container passes one object satisfying every role, because one Discord client is all
    there is. Callers name the roles they use instead: the notifier only posts, the note mirror
    posts and asks which servers this bot is in, the sync service shuts and asks the same, and
    only the thread binding opens or removes anything. Depending on the whole of this to call one
    method of it is how a collaborator ends up able to delete a thread it had no reason to
    touch.
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

            # Opening the thread and writing in it are two calls and two permissions. Losing the
            # id here would leave the thread orphaned and have the retry open another, so it is
            # reported with the failure and recorded before anyone tries again.
            try:
                with _translated("post the first message"):
                    # On the send rather than on the create above, which is where a reader will
                    # look for it: `TextChannel.create_thread` takes neither the content nor an
                    # allow-list, because it opens an empty thread and the first message is a
                    # separate call. A forum channel does both at once.
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
            # Both halves in one edit, which is one PATCH: Discord applies them together or
            # refuses them together, so there is no attempt that could leave a thread archived
            # and unlocked. That pairing is the dangerous one, because anybody may reopen an
            # unlocked thread and the first reply would, while the row went on saying shut.
            #
            # Archiving used to be refused here, on the grounds that a closed issue still gets
            # label and comment events and an archived thread rejects every edit. Still true.
            # What answers it is `_wake` below: every write reopens the thread first, and the
            # delivery shuts it again once it has finished writing.
            #
            # The bot needs Manage Threads for this, and for reopening a thread it shut.
            await thread.edit(archived=shut, locked=shut)

    async def channel_of(self, *, thread_id: int) -> int | None:
        """Which channel a thread is actually in, or None if it is not there any more.

        Gone is an ordinary answer rather than a failure, and the caller acts on it: the pointer
        is worthless either way, so it is let go of and the item gets a fresh thread from whatever
        visits it next. Discord reports a thread deleted only while discord.py still has it
        cached, and it drops one the moment it archives, so a quiet thread can have gone with
        nothing having said so. An id that resolves to something which is not a thread answers the
        same way, and correctly.

        A refusal and an outage still raise. Neither says where the thread is, and reading either
        as "gone" would let go of a live pointer and open a second thread beside a working one,
        which is the failure this whole path exists to undo.

        Free for a thread discord.py has cached, one fetch for an archived one. Archived is the
        common case here, since the rows with no channel recorded are the old quiet ones.
        """
        try:
            thread = await self._thread(thread_id)
        except ThreadNotFoundError:
            return None
        return thread.parent_id

    def is_in(self, guild_id: int) -> bool:
        """Whether this bot is in that server at the moment.

        Asked when Discord has refused something as though a permission were missing, to tell
        that apart from being out of the server altogether. discord.py empties the guild from its
        cache the moment the bot is removed, and answers a channel it can no longer see with a
        refusal that looks exactly like one it is not allowed to touch.

        A guild that is merely not cached yet, during a start or a reconnect, answers False as
        well, which is the useful way round: the caller treats that as something to wait out, and
        waiting is right for both.
        """
        self._require_a_connection()
        return self._client.get_guild(guild_id) is not None

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
        self, thread: discord.Thread, message_id: int | None, panel: Panel, notify: Notify = None
    ) -> int:
        content, view = as_message(panel)
        if message_id is not None:
            try:
                message = await thread.fetch_message(message_id)
            except discord.NotFound:
                # Someone deleted the metadata message. Post a fresh one and adopt its ID.
                logger.info("metadata message %s is gone, posting a replacement", message_id)
            else:
                # An edit notifies nobody whatever it says, so the allow-list changes nothing
                # here. Carried anyway so this method has one rule rather than two, and so the
                # day Discord changes its mind about edits there is nothing to go back and add.
                #
                # `embed` and `attachments` are nulled explicitly because that is what discord.py
                # requires to attach a view to a message that did not have one, and a block
                # carrying a bare GitHub URL has picked up an auto-generated link preview. Left
                # in, it would refuse the edit.
                await message.edit(
                    content=content, embed=None, attachments=[], view=view, **_may_notify(notify)
                )
                return message.id

        # This one is a new message and does notify, which is the whole reason `update` takes an
        # allow-list at all. A block nobody can edit any more is reposted, and reposting it is
        # indistinguishable from opening the thread as far as everybody it names is concerned.
        replacement = await thread.send(**_said(content, view), **_may_notify(notify))
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
