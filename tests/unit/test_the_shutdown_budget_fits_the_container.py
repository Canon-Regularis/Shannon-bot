"""The shutdown waits add up, and nothing in the code says what they add up to.

`worker_shutdown_grace_seconds` and `CLAIM_GRACE_SECONDS` were each written against Docker's ten
second default, in different files, neither aware of the other. They run one after the other, so
what the container actually has to allow is their sum, and Compose is the only place that can be
told. Raising either of them without raising `stop_grace_period` puts it back to being killed
part way through, which strands the rest of a leased batch for a fifteen minute lease while the
replacement process polls a queue that looks empty.

So this reads the numbers out of the code, reads the allowance out of the deployment, and holds
them to the one relationship that matters.

Every loop the shutdown waits on is counted, read off the record of what is running rather than
written down here. The count used to be a literal two - the worker and the poller - and the
transcript flusher was a third loop waited on with the same grace for weeks before anything
noticed, which is exactly how a literal goes stale. The claim's wait was counted once, too, when
the board poller opens threads as the worker does and can be cancelled part way through one; with
both counted, shutdown could already take thirty-five seconds against the thirty it was given.
Both compose files are held to it as well: production is the one that matters, and it was not
read at all.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from shannon.config import Settings
from shannon.runtime.lifespan import _Running
from shannon.services.sync.threads import CLAIM_GRACE_SECONDS

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (ROOT / "docker-compose.yml", ROOT / "compose.prod.yaml")

# `1h30s` and `500ms` are legal here too. Only seconds are used, and a unit this does not
# understand fails the match rather than being read as something it is not.
GRACE = re.compile(r"^\s*stop_grace_period:\s*(\d+)s\s*$", re.MULTILINE)

# The loops that open threads, so can be cancelled part way through opening one and then wait out
# its claim: the worker for deliveries and the poller for board cards. Named rather than read,
# because nothing in the code says which loop reaches `ItemThreads._open`; a loop that comes to
# open threads as well has to be added here.
OPENING_THREADS = frozenset({"worker_task", "poller_task"})


def waited_loops() -> list[str]:
    """Every task the shutdown stops and then waits out with the shared grace.

    All of `_Running`'s tasks but the bot's, which is closed rather than waited out. Read off the
    record itself, so a loop added to the process is counted the moment it can be running.
    """
    return [
        field.name
        for field in dataclasses.fields(_Running)
        if field.name.endswith("_task") and field.name != "bot_task"
    ]


def worst_case() -> float:
    """How long shutdown can take, adding the waits rather than assuming they overlap.

    Every loop is told to stop before any is waited on, so in practice the later waits are already
    over. In the worst case they are not, and a budget worked out from the usual case is not a
    budget. Each loop is given the shared grace, and each that opens threads may then wait out a
    claim as well.
    """
    grace = Settings(github_webhook_secret="x").worker_shutdown_grace_seconds
    loops = set(waited_loops())
    return grace * len(loops) + CLAIM_GRACE_SECONDS * len(OPENING_THREADS & loops)


def test_the_loops_are_read_off_what_is_running() -> None:
    """The reading checked against the loops that are always there. Were the record's names to
    change shape, the count above would quietly fall to nothing and every budget would pass."""
    assert set(waited_loops()) >= OPENING_THREADS


@pytest.mark.parametrize("compose", COMPOSE_FILES, ids=lambda path: path.name)
def test_the_container_is_given_longer_than_the_process_can_take(compose: Path) -> None:
    allowed = [int(seconds) for seconds in GRACE.findall(compose.read_text(encoding="utf-8"))]

    assert allowed, f"{compose.name} sets no stop_grace_period, so Docker allows ten seconds"
    assert min(allowed) > worst_case(), (
        f"shutdown can take {worst_case()}s and {compose.name} has the container killed after "
        f"{min(allowed)}s"
    )
