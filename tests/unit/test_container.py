from __future__ import annotations

import logging

import pytest

from shannon.config import Settings
from shannon.container import build_container
from tests.fakes.github import ClosingGitHub, FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway


class DisposableEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def container_with(
    engine: DisposableEngine, github: FakeGitHubClient, settings: Settings | None = None
):
    return build_container(
        threads=FakeThreadGateway(),
        settings=settings or Settings(github_webhook_secret="x"),
        engine=engine,
        github=github,
    )


class TestClosingTheContainer:
    async def test_both_the_client_and_the_engine_are_closed(self) -> None:
        engine, github = DisposableEngine(), ClosingGitHub()

        await container_with(engine, github).aclose()

        assert github.closed is True
        assert engine.disposed is True

    async def test_a_client_that_fails_to_close_still_releases_the_pool(self) -> None:
        """One step raising used to skip every step after it, which is a leaked pool.

        Shutdown reports the failure and carries on, so without the engine being disposed in a
        finally the connections stay open with nothing left to notice them.
        """
        engine, github = DisposableEngine(), ClosingGitHub(raises=True)

        with pytest.raises(RuntimeError):
            await container_with(engine, github).aclose()

        assert engine.disposed is True, "the HTTP client took the database pool down with it"

    async def test_a_client_with_nothing_to_close_is_fine(self) -> None:
        """The protocol does not require aclose, and a fake standing in has nothing to close."""
        engine = DisposableEngine()

        await container_with(engine, FakeGitHubClient()).aclose()

        assert engine.disposed is True


class TestWhatItWiresUp:
    async def test_every_command_the_bot_installs_is_built(self) -> None:
        """A command missing here is a command that silently stops existing in Discord."""
        container = container_with(DisposableEngine(), FakeGitHubClient())

        assert sorted(command.name for command in container.commands) == [
            "issue",
            "link",
            "link_team",
            "mentions",
            "pr",
            "refresh",
            "register",
            "set_backlog",
            "set_channel",
            "set_done",
            "set_high_priority",
            "set_in_review",
            "set_low_priority",
            "set_med_priority",
            "set_not_reviewed",
            "set_ready_for_merge",
            "unregister",
        ]

    async def test_the_router_handles_every_event_the_webhook_accepts(self) -> None:
        container = container_with(DisposableEngine(), FakeGitHubClient())

        for event in (
            "pull_request",
            "issues",
            "issue_comment",
            "pull_request_review",
            # Not about an item. These keep the account-to-installation map current, and a
            # deployment that dropped them would go on minting tokens against installations that
            # had been removed.
            "installation",
            "installation_repositories",
        ):
            assert container.event_router.handles(event), f"{event} would be dropped on arrival"


class TestTheOneCredentialTheAppCannotReplace:
    """GitHub publishes no App permission for a USER-owned Projects v2 board.

    The Projects permission exists at organisation level only, and `HttpProjectBoards` reads
    `/users/{owner}/projectsV2/...` because a personal account is what this runs against. So that
    one feature keeps a token of its own, and everything else goes through an installation.
    """

    async def test_the_board_reads_with_its_own_token_when_one_is_set(self) -> None:
        from shannon.container import _OneToken

        supplier = _OneToken("ghp_board")

        assert await supplier.token_for("acme") == "ghp_board"
        assert await supplier.token_for("anybody-else") == "ghp_board"

    async def test_a_deployment_with_no_board_token_builds_without_one(self) -> None:
        """Which is every deployment leaving the project number at zero, so the narrow credential
        stays unset rather than being one more thing everybody has to create."""
        container = container_with(
            DisposableEngine(), FakeGitHubClient(), Settings(github_webhook_secret="s")
        )

        assert container.poller is not None

    async def test_setting_one_still_builds(self) -> None:
        """The board then reads through a client of its own rather than the shared one. Asserted
        as construction rather than by reaching inside the poller, because what matters is that
        the branch exists and is taken; which object the reader holds is its own business."""
        container = container_with(
            DisposableEngine(),
            FakeGitHubClient(),
            Settings(github_webhook_secret="s", github_project_token="ghp_board"),
        )

        assert container.poller is not None


class TestSayingWhenNoAppIsConfigured:
    """The failure an unconfigured App causes is silent and looks like something else: every
    request goes out anonymous, and every private repository then reports as one that does not
    exist. So it is said once, at wiring time, where somebody can act on it."""

    async def test_a_deployment_with_no_app_is_told_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.ERROR):
            container_with(DisposableEngine(), FakeGitHubClient())

        assert "no GitHub App is configured" in caplog.text

    async def test_a_deployment_with_one_is_not(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR):
            container_with(
                DisposableEngine(),
                FakeGitHubClient(),
                Settings(
                    github_webhook_secret="s",
                    github_app_client_id="Iv23liAbC",
                    # Any non-empty value will do: the container only asks whether a key is set.
                    # A string shaped like a PEM header would be indistinguishable to a secret
                    # scanner from one somebody had committed by accident.
                    github_app_private_key="placeholder-github-app-private-key",
                ),
            )

        assert "no GitHub App is configured" not in caplog.text
