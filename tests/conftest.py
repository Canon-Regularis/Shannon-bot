from __future__ import annotations

import pytest
from hypothesis import settings

from shannon.config import Settings

# Hypothesis fails an example that runs longer than its deadline. That measures the machine
# rather than the property: the first call into a module pays for importing it, and this shares
# a box with the integration tier. What these tests assert is what holds, not how fast.
settings.register_profile("shannon", deadline=None)
settings.load_profile("shannon")


@pytest.fixture(autouse=True)
def _settings_without_a_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the suite from reading whatever `.env` the machine happens to have.

    `Settings` names `.env` as a source, which is right for running the bot and wrong for
    testing it: nine tests build a bare `Settings()` and assert on what they get, and every one
    of them was answering from a file that exists on a developer's machine and not in CI. So the
    suite passed everywhere it was run and would have failed for anybody who had actually
    configured the bot, which is the one group certain to run it.

    Found exactly that way, by writing a `.env` to start the bot for the first time and watching
    an unrelated test go red.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
