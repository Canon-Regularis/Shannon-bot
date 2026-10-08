from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import pytest
from pydantic import SecretStr

from shannon.config import Settings
from shannon.container import build_container
from shannon.discord_bot.permissions import MemberTiers
from shannon.services.sync.shutting import KeepsThreadsShut
from shannon.services.verification import OAuthClient
from tests.fakes.github import ClosingGitHub, FakeGitHubClient
from tests.fakes.threads import FakeThreadGateway
from tests.support import github_payloads as payloads
from tests.support.credentials import BOARD_KEY


class _Closeable:
    """Anything else the wiring opened, standing in for the two HTTP clients it does."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


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

    async def test_everything_else_opened_here_is_closed_too(self) -> None:
        """Not just `github`. The App's own client signs with a JWT rather than an installation
        token, so it is a second client, and the board's writer is a third, built in every
        deployment whether or not card writes are on. Both were built in the wiring, reachable
        from nowhere else, and closed by nothing."""
        container = container_with(DisposableEngine(), FakeGitHubClient())
        opened = _Closeable()
        container.also_opened = (*container.also_opened, opened)

        await container.aclose()

        assert opened.closed is True

    async def test_both_clients_it_owns_are_held_so_they_can_be(self) -> None:
        """Two, in every deployment. The App's own JWT client, and the board's writer.

        The writer is unconditional now, where it used to appear only when a project token was
        set: it carries no credential at all, because every board write passes the authorisation of
        whoever asked for it. So there is nothing for a deployment to turn on, and nothing to
        branch on here either.
        """
        container = container_with(DisposableEngine(), FakeGitHubClient())

        assert len(container.also_opened) == 2


class TestWhatItWiresUp:
    async def test_every_command_the_bot_installs_is_built(self) -> None:
        """A command missing here is a command that silently stops existing in Discord."""
        container = container_with(DisposableEngine(), FakeGitHubClient())

        assert sorted(command.name for command in container.commands) == [
            "assign",
            "board",
            "issue",
            "label",
            "link",
            "link_team",
            "log_conversation",
            "mentions",
            "pr",
            "priority",
            "refresh",
            "regenerate",
            "register",
            "remind",
            "request_review",
            "set_channel",
            "status",
            "stop_conversation",
            "unassign",
            "unlabel",
            "unregister",
            "unrequest_review",
        ]

    async def test_the_router_handles_every_event_the_webhook_accepts(self) -> None:
        container = container_with(DisposableEngine(), FakeGitHubClient())

        for event in (
            "pull_request",
            "issues",
            "issue_comment",
            "pull_request_review",
            "pull_request_review_comment",
            # Issue #112. Adding a key to `SUPPORTED_EVENTS` and forgetting to register a handler
            # answers `ignored` at the endpoint and writes no row, so the delivery is gone and
            # nothing anywhere says why.
            "check_suite",
            # Not about an item. These keep the account-to-installation map current, and a
            # deployment that dropped them would go on minting tokens against installations that
            # had been removed.
            "installation",
            "installation_repositories",
        ):
            assert container.event_router.handles(event), f"{event} would be dropped on arrival"

    def test_a_check_suite_naming_no_pull_request_is_never_written_down(self) -> None:
        """The wiring for it, which is the half the router's own tests cannot see.

        GitHub sends a suite for every branch running CI. Nothing here can turn a bare commit
        into a tracked item, so each one was a row of around 25kB held for the retention window
        and then dropped by the parser having done nothing. Declining it needs the question
        registered beside the handler, and forgetting that is silent.
        """
        container = container_with(DisposableEngine(), FakeGitHubClient())

        assert (
            container.event_router.will_act_on(
                "check_suite", "completed", payloads.check_suite_event(numbers=())
            )
            is False
        )
        assert (
            container.event_router.will_act_on(
                "check_suite", "completed", payloads.check_suite_event()
            )
            is True
        )


class TestTheOneCredentialTheAppCannotReplace:
    """GitHub publishes no App permission for a USER-owned Projects v2 board.

    The Projects permission exists at organisation level only, and `HttpProjectBoards` reads
    `/users/{owner}/projectsV2/...` because a personal account is what this runs against. So that
    one feature keeps a token of its own, and everything else goes through an installation.
    """

    async def test_a_deployment_that_authorises_no_board_still_builds(self) -> None:
        """Which is every deployment that does not use one, so none of the three board settings is
        one more thing everybody has to create."""
        container = container_with(
            DisposableEngine(), FakeGitHubClient(), Settings(github_webhook_secret="s")
        )

        assert container.poller is not None

    async def test_a_deployment_that_does_builds_too(self) -> None:
        """Asserted as construction rather than by reaching inside the poller: what matters is
        that the branch exists and is taken, and which object the reader holds is its own
        business."""
        container = container_with(
            DisposableEngine(),
            FakeGitHubClient(),
            Settings(
                github_webhook_secret="s",
                github_board_client_id="Ov23liBoard",
                github_board_client_secret=SecretStr("board-shh"),
                board_credential_key=SecretStr(BOARD_KEY),
            ),
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


class TestSayingWhenDiscordSignInIsMissing:
    """Found reviewing #201. Every one-time link now goes through Discord before GitHub, so a
    deployment that set up a GitHub sign-in and not the Discord half refuses every link - which is
    the decision, failing closed. Said once at boot, with the redirect it needs, rather than found
    out one refused command at a time."""

    SECRET = "placeholder-discord-client-secret"

    def booted(self, caplog: pytest.LogCaptureFixture, **settings: Any) -> Any:
        with caplog.at_level(logging.ERROR, logger="shannon.container"):
            return container_with(
                DisposableEngine(),
                FakeGitHubClient(),
                Settings(github_webhook_secret="x", **settings),
            )

    def test_an_app_sign_in_without_discord_is_said_at_boot(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        self.booted(
            caplog,
            github_app_client_secret=SecretStr("shh"),
            public_base_url="https://shannon.example.com/",
        )

        assert "SHANNON_DISCORD_CLIENT_ID" in caplog.text
        assert "SHANNON_DISCORD_CLIENT_SECRET" in caplog.text
        assert "https://shannon.example.com/oauth/discord/callback" in caplog.text

    def test_a_board_sign_in_without_discord_is_said_at_boot(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With the placeholder for a base URL nobody set, so the line still says where."""
        self.booted(caplog, github_board_client_id="Ov23liBoard")

        assert "SHANNON_DISCORD_CLIENT_SECRET" in caplog.text
        assert "<SHANNON_PUBLIC_BASE_URL>/oauth/discord/callback" in caplog.text

    def test_half_of_discord_is_still_said(self, caplog: pytest.LogCaptureFixture) -> None:
        self.booted(
            caplog,
            github_app_client_secret=SecretStr("shh"),
            discord_client_id="1180000000000000",
        )

        assert "SHANNON_DISCORD_CLIENT_SECRET" in caplog.text

    def test_all_of_it_says_nothing_and_signs_in_with_discord(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        container = self.booted(
            caplog,
            github_app_client_id="Iv23liAbC",
            github_app_client_secret=SecretStr("shh"),
            public_base_url="https://shannon.example.com",
            discord_client_id="1180000000000000",
            discord_client_secret=SecretStr(self.SECRET),
        )

        assert "SHANNON_DISCORD" not in caplog.text
        assert container.verification is not None
        assert container.verification.can_sign_in_with_discord is True
        assert container.verification.can_prove_identity is True
        # The wiring itself, in literals. A refactor that dropped the scope or crossed a secret
        # would leave every check above passing and every link dead at Discord.
        assert container.verification._discord == OAuthClient(
            client_id="1180000000000000", client_secret=self.SECRET, scope="identify"
        )

    def test_a_deployment_with_no_sign_in_at_all_says_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing hands out a link there, so a missing Discord half refuses nothing new."""
        container = self.booted(caplog)

        assert "SHANNON_DISCORD" not in caplog.text
        assert container.verification is not None
        assert container.verification.can_sign_in_with_discord is False

    def test_the_secret_is_never_said(self, caplog: pytest.LogCaptureFixture) -> None:
        """Even where the line fires because the other half is missing."""
        self.booted(
            caplog,
            github_board_client_id="Ov23liBoard",
            discord_client_secret=SecretStr(self.SECRET),
        )

        assert "SHANNON_DISCORD_CLIENT_ID" in caplog.text
        assert self.SECRET not in caplog.text


class TestTheBoardLinksSecondQuestion:
    """Found reviewing #201. Following a board link asks Discord, through the gateway the bot
    already holds, whether the member still holds the tier the command was gated on."""

    def test_it_is_asked_of_the_gateway(self) -> None:
        threads = FakeThreadGateway()
        container = build_container(
            threads=threads,
            settings=Settings(github_webhook_secret="x"),
            engine=DisposableEngine(),
            github=FakeGitHubClient(),
        )

        assert container.verification is not None
        tiers = container.verification._tiers
        assert isinstance(tiers, MemberTiers)
        assert tiers._members is threads


class TestTheReminderSender:
    """Issue #229. Built here and started by the lifespan, so this is the one place its wiring
    can be seen whole."""

    def test_it_posts_and_shuts_through_the_gateway_the_bot_holds(self) -> None:
        threads = FakeThreadGateway()
        container = build_container(
            threads=threads,
            settings=Settings(github_webhook_secret="x"),
            engine=DisposableEngine(),
            github=FakeGitHubClient(),
        )

        assert container.reminders._channels is threads
        shut_again = container.reminders._shut_again
        assert isinstance(shut_again, KeepsThreadsShut)
        assert shut_again._threads is threads

    def test_it_looks_as_often_as_the_setting_says(self) -> None:
        container = container_with(
            DisposableEngine(),
            FakeGitHubClient(),
            Settings(github_webhook_secret="x", reminder_tick_seconds=7.5),
        )

        assert container.reminders._tick == timedelta(seconds=7.5)


class TestWhatTurningOffCardWritesTurnsOff:
    """Issue #179. `SHANNON_BOARD_MAY_MOVE_CARDS` stops the bot WRITING to a board, and that is
    the whole of what it is for.

    It used to withhold the board object from the workflow entirely, which also withheld
    `order_for` - so a deployment that merely did not want its board written to silently lost the
    rule that refuses a move the board's column order forbids. One flag, two failures, and a
    `/status` that wrote the label, left the card where it was, and enforced nothing.

    Asserted through the POLLER, which is handed the same board object the workflow is. That is
    the only reachable end of the wire: `Container` deliberately exposes "only the pieces somebody
    outside the wiring asks for by name", and the workflow is not one of them. So what these pin
    is that the flag now reaches the writer rather than the object; that the workflow is handed
    the object at all is held by `test_moving_the_card.py`, which pins what "off" now MEANS - a
    board that answers NO_WRITER rather than a board that is absent.
    """

    def a_container(self, *, may_move: bool) -> object:
        return container_with(
            DisposableEngine(),
            FakeGitHubClient(),
            Settings(github_webhook_secret="s", board_may_move_cards=may_move),
        )

    def board_of(self, container: object) -> Any:
        """The one board reader every caller shares, reached through the poller."""
        return container.poller._projects  # type: ignore[attr-defined]

    async def test_with_writes_off_the_board_has_no_writer(self) -> None:
        """ "Off" as a fact of the wiring rather than a check somebody could forget: the flag is
        the only thing that withholds the writer."""
        board = self.board_of(self.a_container(may_move=False))

        assert board._writer is None

    async def test_with_writes_on_the_board_has_one(self) -> None:
        board = self.board_of(self.a_container(may_move=True))

        assert board._writer is not None

    async def test_the_reader_survives_either_way(self) -> None:
        """The half that broke. Whatever the flag says, the board can still be READ - which is
        what refusing a move the column order forbids costs, and all it costs."""
        assert self.board_of(self.a_container(may_move=False))._client is not None
        assert self.board_of(self.a_container(may_move=True))._client is not None

    async def test_the_writer_carries_no_credential_of_its_own(self) -> None:
        """What replaced "no token means no writer". There is no shared token to be missing now, so
        the flag is the only gate - but the writer it hands over holds no credential, so a write
        with nobody behind it goes out anonymous and GitHub answers 401. The rule that a card is
        moved AS somebody is a fact of the wiring rather than a check somebody could forget.
        """
        board = self.board_of(self.a_container(may_move=True))

        assert board._writer is not None
        assert board._writer._tokens is None, (
            "the board writer was given a credential of its own, which is what this replaced"
        )


class TestABoardWithNoTokenToReadItWith:
    """The failure this catches is silent and looks like something else.

    A board is reached through a separately registered OAuth App, and its authorisations are kept
    encrypted. Either setting without the other is a deployment that cannot do it, and the failure
    is silent and looks like something else: a board that will not open reads exactly like a wrong
    number. So each direction is said once, loudly, at startup.

    What this replaced was keyed to SHANNON_GITHUB_PROJECT_NUMBER, which linking a board by
    command had already made obsolete - so a deployment that did that got no check at all.
    This one is keyed to the settings themselves, which is the part a container can actually see:
    it has no event loop and no connection, so it cannot ask the database whether a board is
    linked. A board nobody authorised finds out on the poll path, per board, once.
    """

    def test_an_app_with_no_key_is_said_at_boot(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="shannon.container"):
            container_with(
                DisposableEngine(),
                FakeGitHubClient(),
                Settings(github_webhook_secret="x", github_board_client_id="Ov23liBoard"),
            )

        assert "SHANNON_BOARD_CREDENTIAL_KEY" in caplog.text
        assert "restart" in caplog.text

    def test_a_key_with_no_app_is_said_at_boot(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="shannon.container"):
            container_with(
                DisposableEngine(),
                FakeGitHubClient(),
                Settings(github_webhook_secret="x", board_credential_key=SecretStr(BOARD_KEY)),
            )

        assert "SHANNON_GITHUB_BOARD_CLIENT_ID" in caplog.text
        assert "restart" in caplog.text

    def test_both_together_say_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR, logger="shannon.container"):
            container_with(
                DisposableEngine(),
                FakeGitHubClient(),
                Settings(
                    github_webhook_secret="x",
                    github_board_client_id="Ov23liBoard",
                    github_board_client_secret=SecretStr("board-shh"),
                    board_credential_key=SecretStr(BOARD_KEY),
                ),
            )

        assert "SHANNON_BOARD_CREDENTIAL_KEY" not in caplog.text
        assert "SHANNON_GITHUB_BOARD_CLIENT_ID" not in caplog.text

    def test_neither_says_nothing_either(self, caplog: pytest.LogCaptureFixture) -> None:
        """Every deployment that does not use a board, which is the default."""
        with caplog.at_level(logging.ERROR, logger="shannon.container"):
            container_with(DisposableEngine(), FakeGitHubClient())

        assert "SHANNON_BOARD_CREDENTIAL_KEY" not in caplog.text
        assert "SHANNON_GITHUB_BOARD_CLIENT_ID" not in caplog.text
