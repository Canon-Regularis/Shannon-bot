"""Installing commands, and the step that actually puts them in front of anybody.

`install` only remembers them. `setup_hook` is what adds them to the tree and tells Discord, and
it runs once at connect, which is nowhere a test had ever been. A command that never reaches the
tree does not fail: it stops existing in Discord and nothing anywhere says so.
"""

from __future__ import annotations

import pytest
from discord import app_commands

from shannon.discord_bot.client import ShannonBot


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
