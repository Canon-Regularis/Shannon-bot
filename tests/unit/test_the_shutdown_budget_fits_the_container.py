"""The two shutdown waits add up, and nothing in the code says what they add up to.

`worker_shutdown_grace_seconds` and `CLAIM_GRACE_SECONDS` were each written against Docker's ten
second default, in different files, neither aware of the other. They run one after the other, so
what the container actually has to allow is their sum, and Compose is the only place that can be
told. Raising either of them without raising `stop_grace_period` puts it back to being killed
part way through, which strands the rest of a leased batch for a fifteen minute lease while the
replacement process polls a queue that looks empty.

So this reads both numbers out of the code, reads the allowance out of the deployment, and holds
them to the one relationship that matters.
"""

from __future__ import annotations

import re
from pathlib import Path

from shannon.config import Settings
from shannon.services.sync.threads import CLAIM_GRACE_SECONDS

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

# `1h30s` and `500ms` are legal here too. Only seconds are used, and a unit this does not
# understand fails the match rather than being read as something it is not.
GRACE = re.compile(r"^\s*stop_grace_period:\s*(\d+)s\s*$", re.MULTILINE)


def worst_case() -> float:
    """How long shutdown can take, adding the waits rather than assuming they overlap.

    The worker and the poller are both told to stop before either is waited on, so in practice
    the poller's wait is already over. In the worst case it is not, and a budget worked out from
    the usual case is not a budget.
    """
    grace = Settings(github_webhook_secret="x").worker_shutdown_grace_seconds
    return grace * 2 + CLAIM_GRACE_SECONDS


def test_the_container_is_given_longer_than_the_process_can_take() -> None:
    allowed = [int(seconds) for seconds in GRACE.findall(COMPOSE.read_text(encoding="utf-8"))]

    assert allowed, "docker-compose.yml sets no stop_grace_period, so Docker allows ten seconds"
    assert min(allowed) > worst_case(), (
        f"shutdown can take {worst_case()}s and the container is killed after {min(allowed)}s"
    )
