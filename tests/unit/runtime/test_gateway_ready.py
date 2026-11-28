"""Waiting for Discord, and what else that wait can end with. Issue #272."""

from __future__ import annotations

import asyncio

import pytest

from shannon.runtime.lifespan import gateway_ready


class StandInBot:
    """A client whose wait ends when, and how, the test says."""

    def __init__(self) -> None:
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.over: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waits_cancelled = 0

    async def start(self, token: str) -> None:
        await self.over

    async def wait_until_ready(self) -> None:
        try:
            await self.ready
        except asyncio.CancelledError:
            self.waits_cancelled += 1
            raise

    def gateway_is_up(self) -> bool:
        return self.ready.done()

    async def close(self) -> None: ...


async def waiting(bot: StandInBot) -> asyncio.Task[None]:
    """The wait as the worker runs it, already inside it, beside a bot that is running."""
    bot_task = asyncio.create_task(bot.start("a-token"))
    # Read, so that a bot a test ends with an error is not reported as one nobody looked at.
    bot_task.add_done_callback(lambda ended: ended.cancelled() or ended.exception())

    async def worker() -> None:
        await gateway_ready(bot, bot_task)()

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


async def test_a_connection_ends_the_wait() -> None:
    bot = StandInBot()
    task = await waiting(bot)
    assert not task.done()

    bot.ready.set_result(None)

    async with asyncio.timeout(10):
        await task


@pytest.mark.parametrize("error", [None, RuntimeError("Improper token has been passed")])
async def test_a_bot_that_ends_first_is_not_a_connection(error: Exception | None) -> None:
    bot = StandInBot()
    task = await waiting(bot)

    if error is None:
        bot.over.set_result(None)
    else:
        bot.over.set_exception(error)

    with pytest.raises(RuntimeError, match=r"^the Discord bot stopped before it ever connected$"):
        await asyncio.wait_for(task, timeout=10)
    assert bot.waits_cancelled == 1, "a wait on a bot that has gone was left running"


async def test_a_bot_that_ends_as_it_connects_is_not_a_connection() -> None:
    bot = StandInBot()
    task = await waiting(bot)

    bot.ready.set_result(None)
    bot.over.set_result(None)

    with pytest.raises(RuntimeError, match=r"^the Discord bot stopped before it ever connected$"):
        await asyncio.wait_for(task, timeout=10)


async def test_an_error_in_the_wait_is_not_a_connection() -> None:
    """discord.py raises this where a client is waited for before it has begun to log in.

    Unread, the wait was simply over: the worker and the reminder sender went on as if Discord
    were there, with the bot's task alive and nothing in the log until the error was collected.
    """
    bot = StandInBot()
    task = await waiting(bot)

    bot.ready.set_exception(RuntimeError("Client has not been properly initialised."))

    with pytest.raises(RuntimeError, match="not been properly initialised"):
        await asyncio.wait_for(task, timeout=10)


async def test_a_cancelled_caller_takes_its_wait_with_it() -> None:
    bot = StandInBot()
    task = await waiting(bot)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert bot.waits_cancelled == 1
