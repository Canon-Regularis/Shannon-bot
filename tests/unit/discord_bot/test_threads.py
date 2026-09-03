from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from shannon.discord_bot.errors import (
    ChannelNotFoundError,
    DiscordGatewayError,
    DiscordPermissionError,
    ThreadNotFoundError,
    ThreadStartedEmptyError,
)
from shannon.discord_bot.threads import (
    ARCHIVE_AFTER_MINUTES,
    THREAD_NAME_LIMIT,
    DiscordThreadGateway,
    truncate_thread_name,
    why_threads_will_not_open,
)
from shannon.domain.errors import PermanentError


def a_channel(kind: type, **permissions: bool) -> MagicMock:
    """A channel this bot has been given exactly the permissions named."""
    stub = MagicMock(spec=kind)
    if kind is discord.ForumChannel:
        stub.flags.require_tag = False
    held = discord.Permissions.none()
    for name, granted in permissions.items():
        setattr(held, name, granted)
    stub.permissions_for = MagicMock(return_value=held)
    return stub


class TestWhatThisBotCanDoInTheChannelItIsGiven:
    """A channel this bot cannot write in is the ordinary way `/set_channel` goes wrong, and it
    used to be accepted without a word.

    The type checks beside this one exist because a channel that refuses is otherwise found out
    hours later and behind the queue. A missing permission is worse than the cases they cover: it
    is permanent, so every delivery is dropped on its first attempt with a line in a log nobody
    is reading, and the person who ran the command was told it worked. A private channel, or a
    role that was never given Create Public Threads, is invisible from the command's side.
    """

    def test_a_text_channel_it_can_use_is_accepted(self) -> None:
        channel = a_channel(
            discord.TextChannel,
            view_channel=True,
            create_public_threads=True,
            send_messages_in_threads=True,
        )

        assert why_threads_will_not_open(channel) is None

    def test_a_text_channel_it_cannot_open_a_thread_in_is_refused(self) -> None:
        channel = a_channel(discord.TextChannel, view_channel=True, send_messages_in_threads=True)

        refusal = why_threads_will_not_open(channel)

        assert refusal is not None
        assert "Create Public Threads" in refusal

    def test_it_names_every_permission_that_is_missing(self) -> None:
        """One at a time is a person coming back three times."""
        channel = a_channel(discord.TextChannel)

        refusal = why_threads_will_not_open(channel)

        assert refusal is not None
        for wanted in ("View Channel", "Create Public Threads", "Send Messages in Threads"):
            assert wanted in refusal

    def test_a_forum_is_judged_on_posting_rather_than_on_threads(self) -> None:
        """A forum post is a thread, and creating one is Send Messages in the forum itself, so
        asking a forum for Create Public Threads would refuse one that works perfectly well."""
        forum = a_channel(
            discord.ForumChannel,
            view_channel=True,
            send_messages=True,
            send_messages_in_threads=True,
        )

        assert why_threads_will_not_open(forum) is None

    def test_manage_threads_is_not_required(self) -> None:
        """It is needed only to lock a finished item's thread, and both paths that want it step
        over a refusal and say so rather than failing."""
        channel = a_channel(
            discord.TextChannel,
            view_channel=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=False,
        )

        assert why_threads_will_not_open(channel) is None

    def test_a_guild_that_is_not_cached_yet_is_not_guessed_about(self) -> None:
        """A client still starting has no member object to ask with, and the answer would be a
        guess. The type checks are the ones this function exists for."""
        channel = MagicMock(spec=discord.TextChannel)
        channel.guild.me = None

        assert why_threads_will_not_open(channel) is None


def message(message_id: int) -> MagicMock:
    stub = MagicMock(spec=discord.Message)
    stub.id = message_id
    stub.edit = AsyncMock()
    return stub


def thread(
    thread_id: int = 500,
    name: str = "#7 Add the webhook endpoint",
    *,
    archived: bool = False,
    locked: bool = False,
) -> MagicMock:
    stub = MagicMock(spec=discord.Thread)
    stub.id = thread_id
    stub.name = name
    # Set explicitly: an unset attribute on a MagicMock is itself a Mock, which is truthy, so
    # leaving these out would have every test look like an archived and locked thread.
    stub.archived = archived
    stub.locked = locked
    stub.send = AsyncMock(return_value=message(900))
    stub.edit = AsyncMock()
    stub.delete = AsyncMock()
    stub.fetch_message = AsyncMock(return_value=message(600))
    return stub


def text_channel(created: MagicMock) -> MagicMock:
    stub = MagicMock(spec=discord.TextChannel)
    stub.create_thread = AsyncMock(return_value=created)
    return stub


def forum_channel(created: MagicMock, created_message: MagicMock) -> MagicMock:
    stub = MagicMock(spec=discord.ForumChannel)
    stub.create_thread = AsyncMock(return_value=MagicMock(thread=created, message=created_message))
    return stub


def client_with(channel: object, *, ready: bool = True) -> MagicMock:
    stub = MagicMock(spec=discord.Client)
    # Set explicitly. Left to the mock it answers with a truthy Mock, which is the right answer
    # by accident and would keep answering it if the gateway stopped asking.
    stub.is_ready = MagicMock(return_value=ready)
    stub.get_channel = MagicMock(return_value=channel)
    stub.fetch_channel = AsyncMock(return_value=channel)
    return stub


async def test_text_channel_thread_is_created_with_its_metadata_message() -> None:
    created = thread()
    channel = text_channel(created)
    gateway = DiscordThreadGateway(client_with(channel))

    handle = await gateway.create(channel_id=10, name="#7 Title", content="metadata")

    channel.create_thread.assert_awaited_once()
    assert channel.create_thread.await_args.kwargs["name"] == "#7 Title"
    created.send.assert_awaited_once_with("metadata")
    assert handle.thread_id == 500
    assert handle.message_id == 900


async def test_forum_channel_thread_carries_its_content_in_the_starter_post() -> None:
    created = thread()
    channel = forum_channel(created, message(901))
    gateway = DiscordThreadGateway(client_with(channel))

    handle = await gateway.create(channel_id=10, name="#7 Title", content="metadata")

    assert channel.create_thread.await_args.kwargs["content"] == "metadata"
    assert handle == type(handle)(thread_id=500, message_id=901)


async def test_creating_in_a_voice_channel_is_refused() -> None:
    gateway = DiscordThreadGateway(client_with(MagicMock(spec=discord.VoiceChannel)))

    with pytest.raises(ChannelNotFoundError, match="cannot hold threads"):
        await gateway.create(channel_id=10, name="x", content="y")


async def test_an_unreachable_channel_is_reported() -> None:
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "missing"))
    gateway = DiscordThreadGateway(client)

    with pytest.raises(ChannelNotFoundError):
        await gateway.create(channel_id=10, name="x", content="y")


async def test_update_edits_the_existing_metadata_message() -> None:
    existing = thread()
    edited = message(600)
    existing.fetch_message = AsyncMock(return_value=edited)
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=600, name=existing.name, content="new metadata"
    )

    edited.edit.assert_awaited_once_with(content="new metadata")
    existing.send.assert_not_awaited()
    assert handle.message_id == 600


async def test_update_renames_only_when_the_title_changed() -> None:
    existing = thread(name="#7 Old title")
    gateway = DiscordThreadGateway(client_with(existing))

    await gateway.update(thread_id=500, message_id=600, name="#7 Old title", content="x")
    existing.edit.assert_not_awaited()

    await gateway.update(thread_id=500, message_id=600, name="#7 New title", content="x")
    existing.edit.assert_awaited_once_with(name="#7 New title")


async def test_update_posts_a_replacement_when_the_message_was_deleted() -> None:
    existing = thread()
    existing.fetch_message = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=600, name=existing.name, content="metadata"
    )

    existing.send.assert_awaited_once_with("metadata")
    assert handle.message_id == 900


async def test_update_posts_a_first_message_when_none_was_stored() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    handle = await gateway.update(
        thread_id=500, message_id=None, name=existing.name, content="metadata"
    )

    existing.fetch_message.assert_not_awaited()
    existing.send.assert_awaited_once_with("metadata")
    assert handle.message_id == 900


async def test_update_never_creates_a_second_thread() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    await gateway.update(thread_id=500, message_id=600, name="#7 Renamed", content="x")

    assert not hasattr(existing, "create_thread") or not existing.create_thread.await_count


async def test_a_missing_thread_is_reported() -> None:
    client = MagicMock(spec=discord.Client)
    client.get_channel = MagicMock(return_value=None)
    client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    gateway = DiscordThreadGateway(client)

    with pytest.raises(ThreadNotFoundError):
        await gateway.update(thread_id=500, message_id=None, name="x", content="y")


async def test_a_channel_that_is_not_a_thread_is_refused() -> None:
    gateway = DiscordThreadGateway(client_with(MagicMock(spec=discord.TextChannel)))

    with pytest.raises(ThreadNotFoundError, match="is not a thread"):
        await gateway.update(thread_id=500, message_id=None, name="x", content="y")


async def test_post_sends_into_the_thread() -> None:
    existing = thread()
    gateway = DiscordThreadGateway(client_with(existing))

    message_id = await gateway.post(thread_id=500, content="<@1> you are on this one")

    existing.send.assert_awaited_once_with("<@1> you are on this one")
    assert message_id == 900


async def test_a_discord_failure_surfaces_as_a_gateway_error() -> None:
    existing = thread()
    existing.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
    gateway = DiscordThreadGateway(client_with(existing))

    with pytest.raises(DiscordGatewayError):
        await gateway.post(thread_id=500, content="x")


def test_long_thread_names_are_truncated() -> None:
    name = truncate_thread_name("x" * 500)

    assert len(name) == THREAD_NAME_LIMIT
    assert name.endswith("…")


def test_a_blank_thread_name_gets_a_placeholder() -> None:
    assert truncate_thread_name("   ") == "Untitled"


class TestArchivedThreads:
    """Discord archives a quiet thread by itself and then refuses every edit to it.

    A pull request nobody discusses for a day is completely ordinary, so without reopening
    first the mirror stops for good the first time that happens.
    """

    async def test_a_thread_is_created_with_the_longest_archive_window(self) -> None:
        created = thread()
        channel = text_channel(created)
        gateway = DiscordThreadGateway(client_with(channel))

        await gateway.create(channel_id=10, name="#7 Title", content="metadata")

        kwargs = channel.create_thread.await_args.kwargs
        assert kwargs["auto_archive_duration"] == ARCHIVE_AFTER_MINUTES

    async def test_a_forum_thread_gets_the_same_window(self) -> None:
        channel = forum_channel(thread(), message(901))
        gateway = DiscordThreadGateway(client_with(channel))

        await gateway.create(channel_id=10, name="#7 Title", content="metadata")

        assert channel.create_thread.await_args.kwargs["auto_archive_duration"] == (
            ARCHIVE_AFTER_MINUTES
        )

    async def test_updating_reopens_an_archived_thread_first(self) -> None:
        existing = thread(archived=True)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.update(thread_id=500, message_id=600, name=existing.name, content="new")

        assert existing.edit.await_args_list[0].kwargs == {"archived": False}

    async def test_posting_reopens_an_archived_thread_first(self) -> None:
        existing = thread(archived=True)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.post(thread_id=500, content="a comment")

        existing.edit.assert_awaited_once_with(archived=False)
        existing.send.assert_awaited_once_with("a comment")

    async def test_an_open_thread_is_not_edited_just_to_reopen_it(self) -> None:
        existing = thread(archived=False)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.post(thread_id=500, content="a comment")

        existing.edit.assert_not_awaited()

    async def test_locking_reopens_in_the_same_edit(self) -> None:
        """One call rather than two, and a locked thread needs Manage Threads either way."""
        existing = thread(archived=True, locked=False)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.set_locked(thread_id=500, locked=True)

        existing.edit.assert_awaited_once_with(archived=False, locked=True)

    async def test_an_archived_thread_is_reopened_even_when_the_lock_already_matches(self) -> None:
        existing = thread(archived=True, locked=True)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.set_locked(thread_id=500, locked=True)

        existing.edit.assert_awaited_once_with(archived=False, locked=True)

    async def test_nothing_happens_when_the_thread_is_already_as_wanted(self) -> None:
        existing = thread(archived=False, locked=True)
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.set_locked(thread_id=500, locked=True)

        existing.edit.assert_not_awaited()


class TestPartialCreation:
    """Opening a thread and writing in it are two calls and two separate permissions."""

    async def test_a_failed_first_message_still_reports_the_thread_id(self) -> None:
        created = thread()
        created.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
        gateway = DiscordThreadGateway(client_with(text_channel(created)))

        with pytest.raises(ThreadStartedEmptyError) as raised:
            await gateway.create(channel_id=10, name="#7 Title", content="metadata")

        assert raised.value.thread_id == 500

    async def test_a_missing_permission_is_not_worth_retrying(self) -> None:
        created = thread()
        created.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope"))
        gateway = DiscordThreadGateway(client_with(text_channel(created)))

        with pytest.raises(ThreadStartedEmptyError) as raised:
            await gateway.create(channel_id=10, name="#7 Title", content="metadata")

        assert isinstance(raised.value.__cause__, DiscordPermissionError)

    async def test_being_refused_the_thread_itself_is_a_permission_error(self) -> None:
        channel = MagicMock(spec=discord.TextChannel)
        channel.create_thread = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "nope")
        )
        gateway = DiscordThreadGateway(client_with(channel))

        with pytest.raises(DiscordPermissionError):
            await gateway.create(channel_id=10, name="x", content="y")

    async def test_a_permission_error_is_permanent(self) -> None:
        """The worker gives up on these at once rather than retrying for two hours."""
        assert issubclass(DiscordPermissionError, PermanentError)


class TestWhetherThisBotIsStillInTheServer:
    """Told apart from a permission it was never given, which Discord answers the same way.

    An admin kicks the bot and re-invites it. While it is out, discord.py empties the guild from
    its cache and every call falls through to a fetch that Discord refuses, and this project
    files that refusal as permanent, so the worker drops the delivery on its first attempt. The
    sixteen attempts over two hours that exist for exactly this go unused. The one thing that
    separates the two cases is whether the guild is there to be asked about.
    """

    def test_a_server_it_is_in_is_answered_yes(self) -> None:
        client = client_with(None)
        client.get_guild = MagicMock(return_value=MagicMock(spec=discord.Guild))

        assert DiscordThreadGateway(client).is_in(guild_id=1) is True

    def test_a_server_it_has_been_removed_from_is_answered_no(self) -> None:
        client = client_with(None)
        client.get_guild = MagicMock(return_value=None)

        assert DiscordThreadGateway(client).is_in(guild_id=1) is False

    def test_a_client_that_has_never_connected_is_not_asked(self) -> None:
        """The same refusal every other call here makes. A client with nothing behind it answers
        the cache lookup with an attribute error several frames down, and a guild that cannot be
        looked up must not read as a guild the bot has been removed from."""
        client = client_with(None, ready=False)

        with pytest.raises(DiscordGatewayError):
            DiscordThreadGateway(client).is_in(guild_id=1)


class TestDeletingAThread:
    async def test_a_stranded_thread_is_removed(self) -> None:
        existing = thread()
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.delete(thread_id=500)

        existing.delete.assert_awaited_once()

    async def test_failing_to_remove_one_is_not_worth_raising_over(self) -> None:
        existing = thread()
        existing.delete = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "x"))
        gateway = DiscordThreadGateway(client_with(existing))

        await gateway.delete(thread_id=500)


class TestDiscordFailingOnTheLookupItself:
    """The fetch is a network call like any other, and it can answer 500 like any other.

    Not a cold path either. discord.py drops a thread from the guild cache the moment it
    archives, so `get_channel` misses and the fetch is the only route to exactly the archived
    thread the write path exists to reopen. Left raw, that exception is discord.py's rather
    than this project's, and two things go wrong: `delete` suppresses this project's gateway
    error and would have let it through, taking down a sync that had already done its work and
    costing a delivery a retry; and the command replies match on this project's errors, so a
    Discord outage answered "something went wrong here" instead of saying Discord refused.
    """

    def _client_failing(self, error: Exception) -> MagicMock:
        stub = MagicMock(spec=discord.Client)
        stub.is_ready = MagicMock(return_value=True)
        stub.get_channel = MagicMock(return_value=None)
        stub.fetch_channel = AsyncMock(side_effect=error)
        return stub

    async def test_a_thread_lookup_that_fails_is_a_gateway_error(self) -> None:
        client = self._client_failing(discord.HTTPException(MagicMock(status=503), "unavailable"))
        gateway = DiscordThreadGateway(client)

        with pytest.raises(DiscordGatewayError, match=r"503|unavailable"):
            await gateway.update(thread_id=500, message_id=None, name="x", content="y")

    async def test_discord_being_down_is_a_gateway_error_too(self) -> None:
        """Raised by discord.py once its own five retries are spent, so it means it."""
        client = self._client_failing(discord.DiscordServerError(MagicMock(status=502), "bad"))
        gateway = DiscordThreadGateway(client)

        with pytest.raises(DiscordGatewayError):
            await gateway.update(thread_id=500, message_id=None, name="x", content="y")

    async def test_a_channel_lookup_that_fails_is_a_gateway_error(self) -> None:
        client = self._client_failing(discord.HTTPException(MagicMock(status=503), "unavailable"))
        gateway = DiscordThreadGateway(client)

        with pytest.raises(DiscordGatewayError):
            await gateway.create(channel_id=10, name="x", content="y")

    async def test_the_stranded_thread_it_cannot_look_up_is_still_not_worth_raising_over(
        self,
    ) -> None:
        """`delete` is called on the branch where another sync already attached the winner."""
        client = self._client_failing(discord.HTTPException(MagicMock(status=503), "unavailable"))
        gateway = DiscordThreadGateway(client)

        await gateway.delete(thread_id=500)

    async def test_a_refusal_is_still_kept_apart_from_an_outage(self) -> None:
        """Forbidden is an HTTPException, so the order of the arms is the whole distinction."""
        client = self._client_failing(discord.Forbidden(MagicMock(status=403), "no"))
        gateway = DiscordThreadGateway(client)

        with pytest.raises(DiscordPermissionError):
            await gateway.update(thread_id=500, message_id=None, name="x", content="y")


class TestRefusedIsNotGone:
    """A permission refusal must never read as a deleted thread.

    Callers rebuild on a missing thread. Reporting a 403 that way would have a temporary loss
    of access delete the item's record of its thread and open a replacement, orphaning
    everything already mirrored into the original.
    """

    def _client_refusing(self) -> MagicMock:
        stub = MagicMock(spec=discord.Client)
        stub.get_channel = MagicMock(return_value=None)
        stub.fetch_channel = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
        return stub

    async def test_a_refused_thread_is_a_permission_error(self) -> None:
        gateway = DiscordThreadGateway(self._client_refusing())

        with pytest.raises(DiscordPermissionError):
            await gateway.update(thread_id=500, message_id=None, name="x", content="y")

    async def test_a_refused_channel_is_a_permission_error(self) -> None:
        gateway = DiscordThreadGateway(self._client_refusing())

        with pytest.raises(DiscordPermissionError):
            await gateway.create(channel_id=10, name="x", content="y")

    async def test_a_missing_thread_is_still_reported_as_missing(self) -> None:
        client = MagicMock(spec=discord.Client)
        client.get_channel = MagicMock(return_value=None)
        client.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "x"))
        gateway = DiscordThreadGateway(client)

        with pytest.raises(ThreadNotFoundError):
            await gateway.update(thread_id=500, message_id=None, name="x", content="y")

    async def test_a_channel_that_is_gone_is_permanent(self) -> None:
        """Both a deleted channel and one that cannot hold threads need /set_channel."""
        assert issubclass(ChannelNotFoundError, PermanentError)

    async def test_a_missing_thread_is_not_permanent(self) -> None:
        """It is a signal to rebuild, which is work rather than a dead end."""
        assert not issubclass(ThreadNotFoundError, PermanentError)


class TestBeforeTheGatewayIsConnected:
    """Every operation reaches into the client's cache and then its websocket.

    On a client that has never connected, or one that has dropped, discord.py answers that from
    its internals with `AttributeError: '_MissingSentinel' object has no attribute 'is_set'`,
    which is not an `HTTPException` and so walks straight past the translation this module does.
    The worker then retried an internal error for two hours and wrote it into `last_error` for
    somebody to puzzle over. Found by running the real process with no token, which is a thing
    the README says is supported; every test in this file uses a mock that is always connected.
    """

    @pytest.mark.parametrize(
        ("what", "call"),
        [
            ("create", lambda g: g.create(channel_id=10, name="n", content="c")),
            ("update", lambda g: g.update(thread_id=1, message_id=2, name="n", content="c")),
            ("post", lambda g: g.post(thread_id=1, content="c")),
            ("set_locked", lambda g: g.set_locked(thread_id=1, locked=True)),
        ],
    )
    async def test_it_says_so_rather_than_leaking_an_internal_error(self, what, call) -> None:
        gateway = DiscordThreadGateway(client_with(thread(), ready=False))

        with pytest.raises(DiscordGatewayError, match="not connected"):
            await call(gateway)

    async def test_it_is_worth_retrying_rather_than_giving_up(self) -> None:
        """A bot that has dropped usually comes back, and the delivery should be waiting when it
        does. A PermanentError here would throw the work away on the first attempt."""
        gateway = DiscordThreadGateway(client_with(thread(), ready=False))

        with pytest.raises(DiscordGatewayError) as caught:
            await gateway.post(thread_id=1, content="c")

        assert not isinstance(caught.value, PermanentError)

    async def test_tidying_up_stays_best_effort(self, caplog: pytest.LogCaptureFixture) -> None:
        """`delete` removes a thread nobody is going to write to, and says so rather than
        failing. A disconnected gateway must not turn that into a failure either."""
        gateway = DiscordThreadGateway(client_with(thread(), ready=False))

        with caplog.at_level("WARNING", logger="shannon.discord_bot.threads"):
            await gateway.delete(thread_id=1)

        assert "could not remove the stranded thread" in caplog.text

    async def test_nothing_is_asked_of_a_client_that_cannot_answer(self) -> None:
        client = client_with(thread(), ready=False)

        with pytest.raises(DiscordGatewayError):
            await gateway_for(client).create(channel_id=10, name="n", content="c")

        client.get_channel.assert_not_called()
        client.fetch_channel.assert_not_awaited()


def gateway_for(client: MagicMock) -> DiscordThreadGateway:
    return DiscordThreadGateway(client)
