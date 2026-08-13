from __future__ import annotations

import pytest

from shannon.config import Settings
from shannon.container import build_container
from tests.fakes.github import FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class ClosingGitHub(FakeGitHubClient):
    def __init__(self, *, raises: bool = False) -> None:
        super().__init__()
        self.raises = raises
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        if self.raises:
            raise RuntimeError("the HTTP pool had already gone")


def container_with(engine: FakeEngine, github: FakeGitHubClient):
    return build_container(
        threads=FakeThreadGateway(),
        settings=Settings(github_webhook_secret="x"),
        engine=engine,
        github=github,
    )


class TestClosingTheContainer:
    async def test_both_the_client_and_the_engine_are_closed(self) -> None:
        engine, github = FakeEngine(), ClosingGitHub()

        await container_with(engine, github).aclose()

        assert github.closed is True
        assert engine.disposed is True

    async def test_a_client_that_fails_to_close_still_releases_the_pool(self) -> None:
        """One step raising used to skip every step after it, which is a leaked pool.

        Shutdown reports the failure and carries on, so without the engine being disposed in a
        finally the connections stay open with nothing left to notice them.
        """
        engine, github = FakeEngine(), ClosingGitHub(raises=True)

        with pytest.raises(RuntimeError):
            await container_with(engine, github).aclose()

        assert engine.disposed is True, "the HTTP client took the database pool down with it"

    async def test_a_client_with_nothing_to_close_is_fine(self) -> None:
        """The protocol does not require aclose, and a fake standing in has nothing to close."""
        engine = FakeEngine()

        await container_with(engine, FakeGitHubClient()).aclose()

        assert engine.disposed is True


class TestWhatItWiresUp:
    async def test_every_command_the_bot_installs_is_built(self) -> None:
        """A command missing here is a command that silently stops existing in Discord."""
        container = container_with(FakeEngine(), FakeGitHubClient())

        assert sorted(command.name for command in container.commands()) == [
            "issue",
            "link",
            "pr",
            "register",
            "set_channel",
        ]

    async def test_the_router_handles_every_event_the_webhook_accepts(self) -> None:
        container = container_with(FakeEngine(), FakeGitHubClient())

        for event in ("pull_request", "issues", "issue_comment", "pull_request_review"):
            assert container.event_router.handles(event), f"{event} would be dropped on arrival"
