"""Installing commands, and the step that actually puts them in front of anybody.

`install` only remembers them. `setup_hook` is what adds them to the tree and tells Discord, and
it runs once at connect, which is nowhere a test had ever been. A command that never reaches the
tree does not fail: it stops existing in Discord and nothing anywhere says so.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from shannon.discord_bot.client import ShannonBot, build_intents
from tests.fakes.discord_objects import FakeAuthor, FakeChannel, a_message

THREAD = 9001


def a_command(name: str) -> app_commands.Command:
    async def run(interaction: object) -> None: ...

    return app_commands.Command(name=name, description=name, callback=run)


@pytest.fixture
def bot() -> ShannonBot:
    return ShannonBot(explain_error=str)


async def test_every_installed_command_reaches_the_tree(
    bot: ShannonBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[object] = []

    async def fake_sync(*_: object, **__: object) -> list[object]:
        synced.append(sorted(command.name for command in bot.tree.get_commands()))
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    bot.install(a_command("pr"), a_command("issue"))

    await bot.setup_hook()

    assert sorted(command.name for command in bot.tree.get_commands()) == ["issue", "pr"]
    # Told to Discord after they are in the tree, not before, or the sync uploads an empty set.
    assert synced == [["issue", "pr"]]


async def test_installing_nothing_still_tells_discord(
    bot: ShannonBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running without commands is a real state: it is what an unfinished deploy looks like.

    The sync still has to happen, because it is what removes the commands a previous version
    registered and this one no longer has.
    """
    calls = 0

    async def fake_sync(*_: object, **__: object) -> list[object]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    await bot.setup_hook()

    assert calls == 1


async def test_the_ready_hook_survives_not_knowing_who_it_is(
    bot: ShannonBot, caplog: pytest.LogCaptureFixture
) -> None:
    """discord.py calls this on every connect, including reconnects, and raising in it puts a
    traceback in the log each time rather than the line saying the gateway is back.

    `user` is None until the READY payload has been read, which is exactly when this runs.
    """
    with caplog.at_level("INFO", logger="shannon.discord_bot.client"):
        await bot.on_ready()

    assert "connected to Discord" in caplog.text


class TestWhatTheGatewayIsAskedFor:
    """A privileged intent is a setup step that fails the whole process when it is missed.

    `members` was asked for and never used. What made it worth removing rather than leaving
    alone is what discord.py does with it: it turns on `chunk_guilds_at_startup`, so every
    server's whole member list is pulled over the gateway before READY, and READY is what the
    delivery worker waits on before it will write anything to Discord.
    """

    def test_nothing_privileged_is_asked_for_by_default(self) -> None:
        intents = build_intents()

        assert intents.members is False, "a Developer Portal toggle nothing here reads"
        assert intents.presences is False
        assert intents.message_content is False

    def test_members_stays_off_even_where_messages_are_read(self) -> None:
        """The two are unrelated and only one of them was ever wanted. Issue #103 turns the
        second on; nothing about that makes the first any more useful than it was."""
        intents = build_intents(capture_messages=True)

        assert intents.members is False
        assert intents.presences is False

    def test_message_content_is_asked_for_when_a_deployment_wants_it(self) -> None:
        """The one privileged thing this bot ever asks for, and the cost is paid knowingly:
        without it every message arrives with `content` empty, so issue #103 cannot work at all.

        Behind a setting because it is a Developer Portal toggle, and a missed toggle closes the
        identify with 4014, which ends the bot task and halts the whole process.
        """
        assert build_intents(capture_messages=True).message_content is True

    def test_guilds_is_asked_for_because_the_permission_gate_needs_the_role_cache(self) -> None:
        """`interaction.user.roles` resolves ids against the guild's roles, and that is where
        they come from. It is not privileged, and it is on by default; this says why."""
        assert build_intents().guilds is True

    def test_the_client_does_not_chunk_every_server_before_it_is_ready(self) -> None:
        """discord.py reads `members` as a request to chunk, so this follows from the first
        test rather than being set anywhere. Pinned because it is the expensive half."""
        client = ShannonBot(explain_error=lambda error: "no")

        assert client._connection._chunk_guilds is False

    def test_reading_messages_does_not_make_it_chunk_either(self) -> None:
        """What makes `message_content` affordable where `members` was not, and the claim
        `build_intents` now rests its case on. Chunking is what delays READY, and READY is what
        the delivery worker waits for before it writes anything."""
        client = ShannonBot(explain_error=lambda error: "no", capture_messages=True)

        assert client._connection._chunk_guilds is False


class TestWhetherDiscordCanBeReached:
    """What the health check asks, and what it used to ask instead.

    `is_ready` reports whether the client's cache has ever been filled. discord.py sets it once,
    when READY arrives, and clears it only in `close`, so a gateway that came up and later fell
    over still reads as ready for the life of the process. The health check built on it could
    report the connection never being made, which is the case it was written for, and could never
    report the connection being lost, which is the more likely one: discord.py reconnects for
    ever by design, so a client whose reconnection keeps failing sits there answering healthy and
    delivering nothing.
    """

    async def test_a_client_that_has_never_connected_is_down(self) -> None:
        assert ShannonBot(explain_error=lambda error: "no").gateway_is_up() is False

    async def test_it_comes_up_when_the_cache_is_filled(self) -> None:
        bot = ShannonBot(explain_error=lambda error: "no")
        _pretend_the_cache_is_filled(bot)

        await bot.on_ready()

        assert bot.gateway_is_up() is True

    async def test_it_goes_down_when_the_connection_does(self) -> None:
        """The half `is_ready` cannot answer."""
        bot = ShannonBot(explain_error=lambda error: "no")
        _pretend_the_cache_is_filled(bot)
        await bot.on_ready()

        await bot.on_disconnect()

        assert bot.is_ready() is True, "discord.py stopped latching, so this proves nothing"
        assert bot.gateway_is_up() is False, "a gateway that has gone still reads as up"

    async def test_it_comes_back_on_a_resumed_session(self) -> None:
        """A reconnection that resumes sends this and no READY, so watching only for READY would
        leave a gateway that is up reading as down until something forced a fresh session."""
        bot = ShannonBot(explain_error=lambda error: "no")
        _pretend_the_cache_is_filled(bot)
        await bot.on_ready()
        await bot.on_disconnect()

        await bot.on_resumed()

        assert bot.gateway_is_up() is True


class TestBeingToldAChannelHasGone:
    """The deletions Discord does not report one by one.

    Deleting a channel deletes every thread in it, and the per-thread events alongside cover only
    the threads discord.py still had cached. It drops one the moment the thread archives, so the
    quiet threads are announced by nothing, and a quiet thread is exactly what the per-thread
    listener exists for.
    """

    async def test_the_channel_is_passed_on(self) -> None:
        gone: list[int] = []
        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_channel_goes(lambda channel_id: _record(gone, channel_id))

        await bot.on_guild_channel_delete(_channel(4242))

        assert gone == [4242]

    async def test_nothing_wired_in_is_not_an_error(self) -> None:
        await ShannonBot(explain_error=lambda error: "no").on_guild_channel_delete(_channel(1))

    async def test_a_failure_letting_go_does_not_reach_discord(self) -> None:
        async def refuses(channel_id: int) -> None:
            raise RuntimeError("the database went away")

        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_channel_goes(refuses)

        await bot.on_guild_channel_delete(_channel(4242))

    async def test_it_is_held_to_the_same_limit_as_a_thread_going(self) -> None:
        """Housekeeping, and never the reason a delivery is late. A server tidying up several
        channels at once would otherwise be several of these beside the per-thread ones."""
        now = 0
        most = 0
        released = asyncio.Event()

        async def slowly(channel_id: int) -> None:
            nonlocal now, most
            now += 1
            most = max(most, now)
            await released.wait()
            now -= 1

        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_channel_goes(slowly)

        letting_go = [
            asyncio.create_task(bot.on_guild_channel_delete(_channel(n))) for n in range(20)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running = most
        released.set()
        await asyncio.gather(*letting_go)

        assert running <= 2, f"a tidy-up ran {running} of these at once"


def _channel(channel_id: int):
    """Enough of a Discord channel for the listener, which reads one attribute."""
    return SimpleNamespace(id=channel_id)


def _pretend_the_cache_is_filled(bot: ShannonBot) -> None:
    """discord.py leaves `_ready` as a sentinel until the client is actually started."""
    bot._ready = asyncio.Event()
    bot._ready.set()


class TestBeingToldAThreadHasGone:
    """The only gateway event this listens to besides READY, and it earns its place.

    A pull request or an issue is told again by its next webhook, and the write path turns the
    refusal into a replacement thread. A draft card on a project board has no webhook: its only
    visitor is the poller, which decides from timestamps and a stored pointer without asking
    Discord, so it passes over a card whose thread has gone without a single call and without a
    line in the log. A card parked in Done that nobody edits again is then mirrored nowhere.
    """

    async def test_the_id_is_passed_on(self) -> None:
        gone: list[int] = []
        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_thread_goes(lambda thread_id: _record(gone, thread_id))

        await bot.on_raw_thread_delete(_deleted(4242))

        assert gone == [4242]

    async def test_nothing_wired_in_is_not_an_error(self) -> None:
        """The client is built before the thing that owns the rows exists."""
        await ShannonBot(explain_error=lambda error: "no").on_raw_thread_delete(_deleted(4242))

    async def test_a_failure_letting_go_does_not_reach_discord(self) -> None:
        """discord.py logs an event handler that raises and carries on, which is a traceback per
        deleted thread in a busy server for something nobody can act on."""

        async def refuses(thread_id: int) -> None:
            raise RuntimeError("the database went away")

        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_thread_goes(refuses)

        await bot.on_raw_thread_delete(_deleted(4242))

    async def test_a_whole_channel_going_does_not_run_all_at_once(self) -> None:
        """Deleting a channel deletes every thread in it, and discord.py dispatches one of these
        for each of them at once, each running a transaction of its own against a pool the
        delivery worker, the poller and every slash command are drawing on.

        Measured against a live database before this was bounded: a channel holding nine hundred
        threads made an ordinary query wait sixteen seconds for a connection, and the wait grows
        with the channel until it reaches the pool's own timeout and deliveries fail outright.
        Letting go of a thread is housekeeping and must never be why a delivery is late.
        """
        now = 0
        most = 0
        released = asyncio.Event()

        async def slowly(thread_id: int) -> None:
            nonlocal now, most
            now += 1
            most = max(most, now)
            await released.wait()
            now -= 1

        bot = ShannonBot(explain_error=lambda error: "no")
        bot.tell_when_a_thread_goes(slowly)

        letting_go = [asyncio.create_task(bot.on_raw_thread_delete(_deleted(n))) for n in range(50)]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        running = most
        released.set()
        await asyncio.gather(*letting_go)

        assert running <= 2, f"a channel going ran {running} of these at once"

    def test_the_cached_event_is_deliberately_not_handled(self) -> None:
        """`on_thread_delete` is the same event with the thread already resolved, and discord.py
        dispatches it only while that thread is in its cache. It drops one the moment the thread
        archives, and Discord archives a thread by itself after a few days of quiet, so the
        cached form covers busy threads and misses every quiet one. Quiet is the whole case this
        exists for: a card parked in Done, archived by age, then deleted.

        Pinned by absence because the two are one character apart and the wrong one looks
        correct in every test that dispatches by hand.
        """
        assert not hasattr(ShannonBot, "on_thread_delete")


async def _record(seen: list[int], thread_id: int) -> None:
    seen.append(thread_id)


def _deleted(thread_id: int) -> discord.RawThreadDeleteEvent:
    """The real payload rather than a stand-in, built from the shape Discord sends. Type 11 is
    a public thread, and the parent and guild are along for the ride."""
    return discord.RawThreadDeleteEvent(
        {"id": thread_id, "type": 11, "guild_id": 1, "parent_id": 2}
    )


async def test_a_per_message_allow_list_still_cannot_reach_everyone(bot: ShannonBot) -> None:
    """The client's own rule is the only thing stopping it.

    Every message carrying a `/mentions` allow-list sends an `AllowedMentions` built for that one
    message, and discord.py merges it over this. Built alone, such an object leaves `everyone` at
    a sentinel that is truthy, so its payload permits @everyone; the merge is what strips it. So
    deleting the keyword from `ShannonBot.__init__` does not fail anything obvious. It opens
    every mirrored GitHub comment to @everyone, which is what this asserts instead.
    """
    one_message = discord.AllowedMentions(users=[discord.Object(id=555)])

    merged = bot.allowed_mentions.merge(one_message).to_dict()

    assert "everyone" not in merged["parse"]
    assert "roles" in merged["parse"], "a review asked of a team stopped reaching its role"
    assert merged["users"] == [555]


class RecordingCapture:
    """The transcript side of the client, with every call written down.

    `awaited` is the one that matters. `on_message` fires for every message in every channel of
    every server, so whether the async path is even entered is the whole performance argument.
    """

    def __init__(self, *, armed: set[int] | None = None, error: Exception | None = None) -> None:
        self.armed = armed if armed is not None else {THREAD}
        self.error = error
        self.asked: list[int] = []
        self.awaited: list[object] = []
        self.said_nothing: list[int] = []
        self.forgotten: list[list[int]] = []

    def is_logging(self, thread_id: int) -> bool:
        self.asked.append(thread_id)
        return thread_id in self.armed

    def nothing_to_capture(self, thread_id: int) -> None:
        self.said_nothing.append(thread_id)

    async def capture(self, message: object) -> None:
        self.awaited.append(message)
        if self.error is not None:
            raise self.error

    async def forget(self, message_ids: Sequence[int]) -> None:
        self.forgotten.append(list(message_ids))
        if self.error is not None:
            raise self.error


class TestReadingAThread:
    """Issue #103, and the hottest handler in the process. The order of the checks is the design."""

    async def test_a_message_in_an_armed_thread_is_kept(self, bot: ShannonBot) -> None:
        capture = RecordingCapture()
        bot.tell_when_a_message_arrives(capture)

        await bot.on_message(a_message())

        assert len(capture.awaited) == 1
        assert capture.awaited[0].content == "hello"

    async def test_a_message_anywhere_else_reaches_no_coroutine(self, bot: ShannonBot) -> None:
        """One set lookup and nothing else. An async answer would allocate a coroutine and a task
        step for every message in the server, and a database answer would scan a column that
        carries no index on purpose."""
        capture = RecordingCapture(armed=set())
        bot.tell_when_a_message_arrives(capture)

        await bot.on_message(a_message(channel=FakeChannel(id=4242)))

        assert capture.asked == [4242]
        assert capture.awaited == []

    async def test_nothing_is_wired_up_at_all(self, bot: ShannonBot) -> None:
        """Which is every deployment that has not turned capture on."""
        await bot.on_message(a_message())

    async def test_this_bots_own_mirrored_comment_is_not_sent_back(self, bot: ShannonBot) -> None:
        """The loop this closes: a GitHub comment mirrored into a logged thread would otherwise be
        transcribed straight back to GitHub, carrying the round before it each time."""
        capture = RecordingCapture()
        bot.tell_when_a_message_arrives(capture)

        await bot.on_message(a_message(author=FakeAuthor(bot=True)))

        assert capture.awaited == []

    async def test_a_message_with_no_text_says_so_rather_than_passing_over_quietly(
        self, bot: ShannonBot
    ) -> None:
        """A message content intent granted in name only looks exactly like a thread where people
        post nothing but pictures, and without this it takes an hour to work out which."""
        capture = RecordingCapture()
        bot.tell_when_a_message_arrives(capture)

        await bot.on_message(a_message(content="  "))

        assert capture.said_nothing == [THREAD]
        assert capture.awaited == []

    async def test_a_capture_that_fails_is_logged_rather_than_swallowed(
        self, bot: ShannonBot, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unlike the two deletion handlers, which suppress because the item heals itself on the
        next webhook. A transcript line dropped here is gone and nothing rebuilds it."""
        capture = RecordingCapture(error=RuntimeError("the database went away"))
        bot.tell_when_a_message_arrives(capture)

        with caplog.at_level("ERROR", logger="shannon.discord_bot.client"):
            await bot.on_message(a_message())

        assert "will not be published" in caplog.text


class TestAMessageTakenBack:
    async def test_a_deletion_in_an_armed_thread_is_honoured(self, bot: ShannonBot) -> None:
        capture = RecordingCapture()
        bot.tell_when_a_message_arrives(capture)

        await bot.on_raw_message_delete(_a_deletion(THREAD, 501))

        assert capture.forgotten == [[501]]

    async def test_a_deletion_anywhere_else_costs_one_lookup(self, bot: ShannonBot) -> None:
        capture = RecordingCapture(armed=set())
        bot.tell_when_a_message_arrives(capture)

        await bot.on_raw_message_delete(_a_deletion(4242, 501))

        assert capture.forgotten == []

    async def test_nothing_wired_up(self, bot: ShannonBot) -> None:
        await bot.on_raw_message_delete(_a_deletion(THREAD, 501))

    async def test_a_purge_is_its_own_event(self, bot: ShannonBot) -> None:
        """Discord sends a bulk delete and no per-message deletion, so without this a moderator
        clearing a stretch of a thread leaves every message in it still queued to publish."""
        capture = RecordingCapture()
        bot.tell_when_a_message_arrives(capture)

        await bot.on_raw_bulk_message_delete(_a_purge(THREAD, [503, 501, 502]))

        assert capture.forgotten == [[501, 502, 503]]

    async def test_a_failure_to_forget_is_logged(
        self, bot: ShannonBot, caplog: pytest.LogCaptureFixture
    ) -> None:
        capture = RecordingCapture(error=RuntimeError("no"))
        bot.tell_when_a_message_arrives(capture)

        with caplog.at_level("ERROR", logger="shannon.discord_bot.client"):
            await bot.on_raw_message_delete(_a_deletion(THREAD, 501))

        assert "may still publish" in caplog.text


def _a_deletion(channel_id: int, message_id: int) -> discord.RawMessageDeleteEvent:
    """The real payload rather than a stand-in, built from the shape Discord sends."""
    return discord.RawMessageDeleteEvent(
        {"id": message_id, "channel_id": channel_id, "guild_id": 1}
    )


def _a_purge(channel_id: int, message_ids: list[int]) -> discord.RawBulkMessageDeleteEvent:
    return discord.RawBulkMessageDeleteEvent(
        {"ids": [str(one) for one in message_ids], "channel_id": channel_id, "guild_id": 1}
    )
