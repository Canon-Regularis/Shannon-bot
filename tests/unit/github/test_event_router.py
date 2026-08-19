"""What the router will and will not accept a handler for.

Here rather than in the endpoint tests: registering is wiring, done once at startup, and has
nothing to do with answering an HTTP request.
"""

from __future__ import annotations

import pytest

from shannon.github.webhooks.router import EventRouter
from tests.fakes.handlers import RecordingHandler


def test_registering_an_unsupported_event_fails() -> None:
    with pytest.raises(ValueError, match="not a supported webhook event"):
        EventRouter().register("project_card", RecordingHandler())
