"""Installing commands, and the step that actually puts them in front of anybody.

`install` only remembers them. `setup_hook` is what adds them to the tree and tells Discord, and
it runs once at connect, which is nowhere a test had ever been. A command that never reaches the
tree does not fail: it stops existing in Discord and nothing anywhere says so.
"""

from __future__ import annotations

import discord
import pytest
from discord import app_commands

from shannon.discord_bot.client import ShannonBot, build_intents


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

    def test_nothing_privileged_is_asked_for(self) -> None:
        intents = build_intents()

        assert intents.members is False, "a Developer Portal toggle nothing here reads"
        assert intents.presences is False
        assert intents.message_content is False

    def test_guilds_is_asked_for_because_the_permission_gate_needs_the_role_cache(self) -> None:
        """`interaction.user.roles` resolves ids against the guild's roles, and that is where
        they come from. It is not privileged, and it is on by default; this says why."""
        assert build_intents().guilds is True

    def test_the_client_does_not_chunk_every_server_before_it_is_ready(self) -> None:
        """discord.py reads `members` as a request to chunk, so this follows from the first
        test rather than being set anywhere. Pinned because it is the expensive half."""
        client = ShannonBot(explain_error=lambda error: "no")

        assert client._connection._chunk_guilds is False


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
