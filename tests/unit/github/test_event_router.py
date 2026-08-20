"""What the router will and will not accept a handler for, and what it does with the rest.

Here rather than in the endpoint tests: registering is wiring, done once at startup, and has
nothing to do with answering an HTTP request.

The dispatch guards below look redundant against `will_act_on`, which the route asks first and
which refuses everything they refuse. They are not, because the two questions are asked at
different times. The route asks before a delivery is written down; the worker dispatches it
minutes or a deploy later, and what this bot acts on is a list that changes.
"""

from __future__ import annotations

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

    outcome = await router.dispatch("pull_request", "synchronize", {})

    assert outcome is WebhookOutcome.IGNORED
    assert handler.calls == [], "the handler was given an action the bot had stopped acting on"


async def test_an_event_with_no_handler_left_is_dropped_rather_than_raising() -> None:
    """The other half of the same deploy: the event survives in the queue, its handler does not.

    Raising would spend sixteen attempts and two hours of backoff on a delivery that will never
    be handled by this version of the bot.
    """
    outcome = await EventRouter().dispatch("issue_comment", "created", {})

    assert outcome is WebhookOutcome.IGNORED
