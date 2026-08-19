from __future__ import annotations

import pytest

from tests.support.webhooks import RecordingHandler


@pytest.fixture
def handler() -> RecordingHandler:
    return RecordingHandler()
