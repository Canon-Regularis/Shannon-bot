"""How long the worker holds on, and that the shipped defaults say what they claim.

These read WorkerSettings and touch nothing else, so they belong beside the module rather than
in the file that drives a real queue.
"""

from __future__ import annotations

from datetime import timedelta

from shannon.config import Settings
from shannon.services.delivery.worker import WorkerSettings


async def test_backoff_doubles_up_to_the_cap() -> None:
    settings = WorkerSettings(first_backoff=timedelta(seconds=5), max_backoff=timedelta(minutes=1))

    waits = [settings.backoff_for(attempts).total_seconds() for attempts in range(6)]

    assert waits == [5, 10, 20, 40, 60, 60]


def test_the_retry_budget_is_the_two_hours_it_claims_to_be() -> None:
    """Six comments quote this figure. It used to be 36 minutes."""
    held = WorkerSettings().total_backoff()

    assert timedelta(hours=2) <= held <= timedelta(hours=2, minutes=30)


class TestTheRetryBudgetThatShips:
    """The dataclass default is not what runs; Settings is, and the two had drifted apart."""

    def test_the_configured_budget_is_the_two_hours_claimed(self) -> None:
        held = WorkerSettings.from_settings(Settings()).total_backoff()

        assert timedelta(hours=2) <= held <= timedelta(hours=2, minutes=30)

    def test_the_dataclass_default_agrees_with_it(self) -> None:
        assert WorkerSettings().max_attempts == Settings().worker_max_attempts
