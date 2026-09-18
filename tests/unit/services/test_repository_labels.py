"""The labels a repository has, remembered for a little while.

Issue #104. This exists because the picker beside the label field is asked on every keystroke and
Discord allows an autocomplete about three seconds. Typing ten characters has to be one call to
GitHub rather than ten, so what these pin is the arithmetic of that and the one thing the cache
must not get wrong: which spelling comes back out.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from shannon.services.labels import RepositoryLabels

pytestmark = pytest.mark.unit

START = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


class StubLabels:
    def __init__(self, names: Sequence[str] | None = None) -> None:
        self.names = list(names if names is not None else ["bug", "good first issue"])
        self.calls: list[tuple[str, str]] = []

    async def list_labels(self, owner: str, name: str) -> Sequence[str]:
        self.calls.append((owner, name))
        return list(self.names)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def cached(github: StubLabels, clock: Clock) -> RepositoryLabels:
    return RepositoryLabels(github, now=clock, lifetime=timedelta(minutes=2))


class TestAskingGitHubAsLittleAsPossible:
    async def test_the_first_ask_reaches_github(self) -> None:
        github, clock = StubLabels(), Clock()

        assert await cached(github, clock).names("acme", "widget") == ("bug", "good first issue")
        assert github.calls == [("acme", "widget")]

    async def test_several_keystrokes_are_one_call(self) -> None:
        """The whole reason this class exists."""
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)

        for _ in range(10):
            await labels.names("acme", "widget")

        assert github.calls == [("acme", "widget")]

    async def test_it_asks_again_once_the_answer_is_old(self) -> None:
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)
        await labels.names("acme", "widget")

        clock.now = START + timedelta(minutes=3)
        await labels.names("acme", "widget")

        assert len(github.calls) == 2

    async def test_it_does_not_ask_again_a_moment_later(self) -> None:
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)
        await labels.names("acme", "widget")

        clock.now = START + timedelta(seconds=30)
        await labels.names("acme", "widget")

        assert len(github.calls) == 1

    async def test_two_repositories_do_not_share_an_answer(self) -> None:
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)

        await labels.names("acme", "widget")
        await labels.names("acme", "gadget")

        assert github.calls == [("acme", "widget"), ("acme", "gadget")]

    async def test_one_repository_named_two_ways_is_one_repository(self) -> None:
        """GitHub treats a repository path case insensitively, and a thread's stored name and a
        typed one need not agree on case."""
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)

        await labels.names("acme", "widget")
        await labels.names("Acme", "Widget")

        assert len(github.calls) == 1


class TestWhichSpellingComesBack:
    async def test_the_repository_s_own(self) -> None:
        """Not the one that was typed. GitHub matches a label name without regard to case, so
        writing `Bug` onto a repository holding `bug` attaches what was already there while the
        block, which compares case-folded, sees nothing change."""
        github, clock = StubLabels(["Bug"]), Clock()

        assert await cached(github, clock).spelled("acme", "widget", "bug") == "Bug"

    async def test_an_exact_match_is_still_the_repository_s(self) -> None:
        github, clock = StubLabels(["bug"]), Clock()

        assert await cached(github, clock).spelled("acme", "widget", "bug") == "bug"

    async def test_surrounding_space_does_not_stop_a_match(self) -> None:
        github, clock = StubLabels(["bug"]), Clock()

        assert await cached(github, clock).spelled("acme", "widget", "  bug ") == "bug"

    async def test_a_label_the_repository_does_not_have(self) -> None:
        github, clock = StubLabels(["bug"]), Clock()

        assert await cached(github, clock).spelled("acme", "widget", "bugg") is None

    async def test_a_repository_with_no_labels_at_all(self) -> None:
        github, clock = StubLabels([]), Clock()

        assert await cached(github, clock).spelled("acme", "widget", "bug") is None

    async def test_looking_one_up_uses_the_same_cached_list(self) -> None:
        github, clock = StubLabels(), Clock()
        labels = cached(github, clock)

        await labels.names("acme", "widget")
        await labels.spelled("acme", "widget", "bug")

        assert len(github.calls) == 1
