"""What a task the process cannot do without does when it dies, checked in a real process.

Everything else about this is testable with a stand-in, and the one step that matters is not: the
signal itself. `ask_the_process_to_stop` raises SIGTERM, and whether that ends up somewhere that
runs the ordinary shutdown, rather than killing the process where it stands, is a property of the
interpreter and the platform rather than of this code.

So it runs in a subprocess, which also means a mistake here cannot take the test run with it.
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


def test_the_signal_reaches_a_handler_rather_than_ending_the_process() -> None:
    """Uvicorn installs a handler for this and shuts down on it, which is the whole point.

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
