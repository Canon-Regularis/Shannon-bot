from __future__ import annotations

import functools
import re
from collections.abc import Iterator

import pytest
from hypothesis import settings

from shannon.config import Settings
from tests.fakes.threads import FakeThreadGateway

# Hypothesis fails an example that runs longer than its deadline. That measures the machine
# rather than the property: the first call into a module pays for importing it, and this shares
# a box with the integration tier. What these tests assert is what holds, not how fast.
settings.register_profile("shannon", deadline=None)
settings.load_profile("shannon")

# An account mention, and nothing else. A role is `<@&123>` and is deliberately left alone: one
# member cannot opt out of a role ping and this bot must not pretend otherwise. A defused one
# carries a zero-width space between the bracket and the at sign, so it does not match either,
# which is right because it reaches nobody in the first place.
_AN_ACCOUNT_MENTION = re.compile(r"<@\d+>")


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


@pytest.fixture(autouse=True)
def _a_message_that_names_somebody_says_who_it_may_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Every write carrying an account mention has to say who it is allowed to ring.

    The thread gateway's allow-list defaults to saying nothing, which leaves the client's own
    rule in force and notifies everybody named. That default is deliberate: the failure this
    project has been burned by twice is a ping that silently never arrives, and a default of
    "nobody" would mute any path that forgot, with nothing anywhere to say so. A default of
    "everybody" fails the other way, loudly, at somebody who asked not to be rung and will say
    so. But it is only defensible if something catches the path that forgot, and this is it.

    Wrapped on the CLASS rather than on a fixture's instance, because several tests build their
    own gateway inline or subclass it to fail on demand, and those are exactly the paths a
    fixture-scoped check cannot see. A subclass calls `super()`, so it is covered too.

    What this cannot catch, said plainly rather than left to be discovered: a producer that no
    test exercises at all. The coverage floor is the other half of the pair, and neither half is
    worth much alone.

    Wrapped with `functools.wraps`, which is not decoration. This fixture is autouse, so the
    replacement is what every other test in the suite sees on the class, and one of them reads
    the signatures off it to check the fake still matches the protocol it stands in for. Without
    the `__wrapped__` that copies over, that test reads this wrapper's `*args, **kwargs` and
    reports the fake as having lost every parameter it has.
    """
    seen: list[tuple[str, str]] = []
    for name in ("create", "update", "post"):
        original = getattr(FakeThreadGateway, name)

        @functools.wraps(original)
        def watched(self, *args, _name=name, _original=original, **kwargs):
            if _AN_ACCOUNT_MENTION.search(kwargs.get("content", "")) and "notify" not in kwargs:
                seen.append((_name, kwargs["content"]))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(FakeThreadGateway, name, watched)

    yield

    assert not seen, (
        "a message named somebody and said nothing about who it may notify, so anybody who ran "
        f"/mentions off would still be rung by it: {seen}"
    )
