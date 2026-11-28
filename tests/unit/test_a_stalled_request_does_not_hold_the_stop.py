"""A client that sends half a body and goes quiet does not keep the process alive (#272).

Uvicorn waits for every open request to end before it starts the lifespan's shutdown, and by
default it waits for as long as that takes. The webhook route reads the body before it can check
the signature, so one anonymous client could hold a stop open: the worker was never asked to
stop, and a process that halted itself never ended, so nothing restarted it.

The wait is uvicorn's and the limit is one keyword, so this runs a real uvicorn with the real app
and the real webhook route on a real socket. Only the length of the limit is smaller.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr

from shannon.api.app import create_app
from shannon.config import Settings
from shannon.main import run
from tests.support.waiting import until

SETTINGS = Settings(github_webhook_secret=SecretStr("x"))

# In place of the ten seconds production waits.
GRACE_SECONDS = 1

# How long the control watches for a shutdown that must not start. Twice the grace, counted from
# the moment the server stops listening, so a shutdown that was going to start has started.
WATCH_SECONDS = 2.0 * GRACE_SECONDS

# How long to wait for a step that does happen. Reached only when one does not, so it is far above
# what any of them takes on a loaded machine.
PATIENCE_SECONDS = 10.0

# Both headers the route asks for before it reads the body. With `Expect`, uvicorn answers "100
# Continue" when the route asks for the body, which is how the client knows that it has.
HEAD = (
    b"POST /webhooks/github HTTP/1.1\r\n"
    b"Host: 127.0.0.1\r\n"
    b"X-GitHub-Event: pull_request\r\n"
    b"X-GitHub-Delivery: stalled\r\n"
    b"Content-Length: 1000\r\n"
    b"Expect: 100-continue\r\n"
    b"\r\n"
)
PART_OF_A_BODY = b'{"action": '


@dataclass
class Stalled:
    """A server asked to stop while it is reading a body that does not arrive."""

    shutdown_began: asyncio.Event
    finished: asyncio.Task[None]
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


@asynccontextmanager
async def stopping_mid_request(grace: int | None) -> AsyncIterator[Stalled]:
    """Serve the app, stall one request in the webhook route, and ask the server to stop.

    Hands over once the server has stopped listening, which is when its wait for the open request
    begins. The client hangs up on the way out whatever happened, which ends the server as well.
    """
    shutdown_began = asyncio.Event()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        shutdown_began.set()

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings=SETTINGS, lifespan=lifespan),
            host="127.0.0.1",
            port=0,
            # Uvicorn's own would replace the logging of the whole test run.
            log_config=None,
            timeout_graceful_shutdown=grace,
        )
    )
    finished = asyncio.create_task(server.serve())
    await until(lambda: server.started, PATIENCE_SECONDS)
    listener = server.servers[0]
    reader, writer = await asyncio.open_connection(*listener.sockets[0].getsockname())
    try:
        async with asyncio.timeout(PATIENCE_SECONDS):
            writer.write(HEAD)
            assert b"100 Continue" in await reader.readuntil(b"\r\n\r\n")
            writer.write(PART_OF_A_BODY)
            await writer.drain()

            # What uvicorn's handler for SIGTERM sets, and all that it sets.
            server.should_exit = True
        await until(lambda: not listener.is_serving(), PATIENCE_SECONDS)
        yield Stalled(shutdown_began, finished, reader, writer)
    finally:
        server.should_exit = True
        writer.close()
        async with asyncio.timeout(PATIENCE_SECONDS):
            await finished


def the_grace_run_gives(monkeypatch: pytest.MonkeyPatch, scaled_to: int) -> int | None:
    """The limit `run` hands uvicorn, with its constant scaled down.

    Read out of the real call rather than written here, so the server under test stops the way the
    one in production does.
    """
    served: dict[str, object] = {}

    def record(app: object, **kwargs: object) -> None:
        served.update(kwargs)

    monkeypatch.setattr("shannon.main.uvicorn.run", record)
    monkeypatch.setattr("shannon.main.get_settings", lambda: SETTINGS)
    monkeypatch.setattr("shannon.main.configure_logging", lambda settings: None)
    monkeypatch.setattr("shannon.main.keep_oauth_queries_out_of_the_access_log", lambda: None)
    monkeypatch.setattr("shannon.main.build_app", lambda settings: None)
    # `raising=False`, so a `run` with no limit fails by what it does, which is to hold the stop
    # open, rather than by a name it does not have.
    monkeypatch.setattr("shannon.main.HTTP_GRACE_SECONDS", scaled_to, raising=False)

    run()

    grace = served.get("timeout_graceful_shutdown")
    assert grace is None or isinstance(grace, int)
    return grace


async def test_a_stalled_body_does_not_keep_the_shutdown_from_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grace = the_grace_run_gives(monkeypatch, scaled_to=GRACE_SECONDS)

    async with stopping_mid_request(grace) as stalled:
        async with asyncio.timeout(PATIENCE_SECONDS):
            await stalled.shutdown_began.wait()
            # The process ends as well, with the client still on the line.
            await stalled.finished
            # The cut request is answered, rather than dropped without a word.
            assert b" 500 " in await stalled.reader.readline()


async def test_without_a_limit_the_shutdown_waits_for_the_client_to_leave() -> None:
    """The control. Were this to fail, uvicorn would bound the wait by itself, and the test above
    would pass whatever `run` gave it."""
    async with stopping_mid_request(grace=None) as stalled:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(stalled.shutdown_began.wait(), WATCH_SECONDS)

        stalled.writer.close()

        async with asyncio.timeout(PATIENCE_SECONDS):
            await stalled.shutdown_began.wait()
            await stalled.finished
