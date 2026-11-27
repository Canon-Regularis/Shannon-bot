"""What a task the process cannot do without does when it dies, checked in a real process.

Everything else about this is testable with a stand-in, and the one step that matters is not: the
signal itself. `ask_the_process_to_stop` raises SIGTERM, and whether that ends up somewhere that
runs the ordinary shutdown, rather than killing the process where it stands, is a property of the
interpreter, the platform and uvicorn rather than of this code.

So all but the last test run it in a subprocess, which also means a mistake here cannot take the
test run with it. Two of them catch the signal with a handler of their own, and one leaves it to
uvicorn (#272).
"""

from __future__ import annotations

import signal
import subprocess
import sys
import textwrap

from shannon.runtime.supervision import ask_the_process_to_stop

CHILD = textwrap.dedent(
    """
    import signal, sys

    from shannon.runtime.supervision import ask_the_process_to_stop

    caught = []
    signal.signal(signal.SIGTERM, lambda *_: caught.append(True))

    ask_the_process_to_stop()

    # Raising a signal sets a flag the interpreter reads at the next bytecode boundary, so the
    # handler has run by the time anything after this does.
    print("caught" if caught else "missed")
    sys.exit(0)
    """
)


# Port 0, so that nothing here can collide with a port already in use. What the lifespan prints is
# read by the parent, and "shut down" is printed only once uvicorn has run the shutdown half.
UVICORN_CHILD = textwrap.dedent(
    """
    import asyncio, contextlib

    import uvicorn
    from fastapi import FastAPI

    from shannon.runtime.supervision import ask_the_process_to_stop

    @contextlib.asynccontextmanager
    async def lifespan(app):
        print("started", flush=True)
        asyncio.get_running_loop().call_later(0.2, ask_the_process_to_stop)
        yield
        # Gives the loop away, as every real shutdown step does, so a process that ends as soon as
        # the shutdown has begun never prints the line.
        await asyncio.sleep(0.1)
        print("shut down", flush=True)

    uvicorn.run(FastAPI(lifespan=lifespan), host="127.0.0.1", port=0)
    """
)


def test_the_signal_reaches_a_handler_rather_than_ending_the_process() -> None:
    """A handler gets the signal, which is all that uvicorn's own handler needs.

    Killing the process outright would leave the delivery in hand half done and the rest of its
    leased batch locked for the lease, which is what shutting down properly exists to avoid.
    """
    finished = subprocess.run(
        [sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=60
    )

    assert finished.stdout.strip().endswith("caught"), finished.stderr
    assert finished.returncode == 0


def test_it_says_what_it_is_doing_before_it_does_it() -> None:
    """The line before the signal is the only thing that explains the exit in the log."""
    finished = subprocess.run(
        [sys.executable, "-c", "import logging;logging.basicConfig(level='ERROR');" + CHILD],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert "stopping the process so it can be restarted" in finished.stderr


def test_uvicorn_runs_the_lifespans_shutdown_and_then_the_process_ends() -> None:
    """The two halves of a halt: the ordinary shutdown runs, and then there is an exit to act on.

    The exit code is left alone. Uvicorn raises the signal again once it is done, so the code is
    the platform's, and the restart policy acts on the exit whatever the code (#272).

    The timeout is far above what this takes, and under the one pytest puts on every test, so a
    process that never ends fails here by name and is killed.
    """
    finished = subprocess.run(
        [sys.executable, "-c", UVICORN_CHILD], capture_output=True, text=True, timeout=45
    )

    assert finished.stdout.split("\n")[:2] == ["started", "shut down"], finished.stderr


def test_it_raises_the_signal_in_this_process_too() -> None:
    """The same call, in the process running the suite, with a handler of our own in the way.

    Safe because the subprocess above proves a handler catches it rather than the interpreter
    ending the process, and the previous handler goes back whatever happens here.
    """
    caught: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda number, frame: caught.append(number))
    try:
        ask_the_process_to_stop()
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert caught == [signal.SIGTERM]
