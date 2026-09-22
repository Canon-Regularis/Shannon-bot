"""What the router will and will not accept a handler for, and what it does with the rest.

Here rather than in the endpoint tests: registering is wiring, done once at startup, and has
nothing to do with answering an HTTP request.

The dispatch guards below look redundant against `will_act_on`, which the route asks first and
which refuses everything they refuse. They are not, because the two questions are asked at
different times. The route asks before a delivery is written down; the worker dispatches it
minutes or a deploy later, and what this bot acts on is a list that changes.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from shannon.github.webhooks.events import WebhookOutcome
from shannon.github.webhooks.router import EventRouter
from tests.fakes.handlers import RecordingHandler


def test_registering_an_unsupported_event_fails() -> None:
    with pytest.raises(ValueError, match="not a supported webhook event"):
        EventRouter().register("star", RecordingHandler())


async def test_an_action_no_longer_acted_on_never_reaches_the_handler() -> None:
    """A delivery accepted under an older list of actions, leased under a newer one."""
    handler = RecordingHandler()
    router = EventRouter()
    router.register("pull_request", handler)

    outcome = await router.dispatch("pull_request", "milestoned", {})

    assert outcome is WebhookOutcome.IGNORED
    assert handler.calls == [], "the handler was given an action the bot had stopped acting on"


async def test_an_event_with_no_handler_left_is_dropped_rather_than_raising() -> None:
    """The other half of the same deploy: the event survives in the queue, its handler does not.

    Raising would spend sixteen attempts and two hours of backoff on a delivery that will never
    be handled by this version of the bot.
    """
    outcome = await EventRouter().dispatch("issue_comment", "created", {})

    assert outcome is WebhookOutcome.IGNORED


class TestWhatIsWorthWritingDown:
    """The second question the route asks, for events the table alone cannot settle.

    `check_suite` is the only one: GitHub sends one for every branch running CI, and nothing in
    this project can turn a bare commit into a tracked item, so a suite naming no pull request
    was stored as around 25kB of JSONB, held for the retention window, leased, dispatched, and
    dropped by the parser having done nothing. The registered question moves that to the route.
    """

    def test_an_event_registered_with_no_question_is_recorded_on_the_table_alone(self) -> None:
        router = EventRouter()
        router.register("pull_request", RecordingHandler())

        assert router.will_act_on("pull_request", "opened", {}) is True

    def test_a_delivery_the_question_refuses_is_not_recorded(self) -> None:
        router = EventRouter()
        router.register("check_suite", RecordingHandler(), worth_recording=lambda payload: False)

        assert router.will_act_on("check_suite", "completed", {}) is False

    def test_a_delivery_the_question_allows_is_recorded(self) -> None:
        router = EventRouter()
        router.register("check_suite", RecordingHandler(), worth_recording=lambda payload: True)

        assert router.will_act_on("check_suite", "completed", {}) is True

    def test_the_question_is_never_asked_about_an_action_nothing_acts_on(self) -> None:
        """Order matters: the cheap table lookup settles it first, and a payload that would
        trip the question over is never handed to it."""
        asked: list[Mapping[str, object]] = []

        def worth_recording(payload: Mapping[str, object]) -> bool:
            asked.append(payload)
            return True

        router = EventRouter()
        router.register("check_suite", RecordingHandler(), worth_recording=worth_recording)

        assert router.will_act_on("check_suite", "requested", {}) is False
        assert asked == []
