"""The wait for a busy item ends before the deadline that would cancel it.

`LOCK_WAIT_SECONDS` and `worker_delivery_timeout_seconds` are in different files and neither
mentions the other, but the whole reason the first exists is to come in under the second.

A handler cancelled at its deadline while parked in `pg_advisory_xact_lock` is the bad case:
cancelling a task blocked in asyncpg opens a second socket for the cancel and waits on that with
nothing bounding it, which is how one health check stalled eleven minutes. Postgres ending the
wait itself is an ordinary error with a sentence in it, and the caller is retried. Raise the
first past the second and every contended delivery goes back to taking the bad path.

There also has to be time left for the sync the caller came to do, which is what the margin is
about; how much is a judgement, that the wait comes first is not.
"""

from __future__ import annotations

from pydantic import SecretStr

from shannon.config import Settings
from shannon.services.sync.one_at_a_time import LOCK_WAIT_SECONDS


def budget() -> float:
    return Settings(github_webhook_secret=SecretStr("x")).worker_delivery_timeout_seconds


def test_postgres_ends_the_wait_before_the_worker_cancels_it() -> None:
    assert budget() > LOCK_WAIT_SECONDS, (
        f"a writer waits {LOCK_WAIT_SECONDS}s for an item and is cancelled after {budget()}s, "
        "so the wait ends by cancelling asyncpg mid-query"
    )


def test_a_writer_that_wins_the_wait_still_has_time_to_work() -> None:
    """Most of the budget, not a sliver of it: what follows the wait is the whole sync."""
    assert budget() / 2 >= LOCK_WAIT_SECONDS
