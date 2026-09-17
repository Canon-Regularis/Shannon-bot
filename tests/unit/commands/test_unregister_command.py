"""`/unregister`, and the two runs it takes.

Issue #98. This is the one command that destroys a binding, so what it refuses matters more than
what it does. A Discord interaction cannot wait on somebody opening a browser, so the first run
hands out a one-time link and the second finishes the job.
"""

from __future__ import annotations

import pytest

from shannon.commands.unregister import build_unregister_command
from shannon.domain.errors import NotProvenError, NotRegisteredError, RepositoryMismatchError
from shannon.services.unregistration import UnregisterOutcome
from tests.fakes.discord_objects import FakeInteraction
from tests.unit.commands.conftest import administrator, default_gate, developer

pytestmark = pytest.mark.unit

ALICE = 555
REPO = "acme/widget"


class FakeVerification:
    def __init__(self, *, configured: bool = True, proved: str | None = "octocat") -> None:
        self.configured = configured
        self.proved = proved
        self.links_handed_out = 0

    async def already_proved(self, *, guild_id: int, discord_user_id: int) -> str | None:
        return self.proved

    async def link_for(self, *, guild_id: int, discord_user_id: int) -> str:
        self.links_handed_out += 1
        return "https://github.com/login/oauth/authorize?state=abc"


class FakeUnregistration:
    def __init__(self, *, outcome: UnregisterOutcome | None = None, error: Exception | None = None):
        self.outcome = outcome or UnregisterOutcome(full_name=REPO, threads_orphaned=0)
        self.error = error
        self.calls: list[tuple[int, str, str]] = []

    async def unregister(self, *, guild_id: int, full_name: str, login: str) -> UnregisterOutcome:
        self.calls.append((guild_id, full_name, login))
        if self.error is not None:
            raise self.error
        return self.outcome


def run_it(
    *,
    verification: FakeVerification | None = None,
    service: FakeUnregistration | None = None,
    admin: bool = True,
    repository: str = REPO,
):
    verification = verification or FakeVerification()
    service = service or FakeUnregistration()
    command = build_unregister_command(service, verification, default_gate())
    who = administrator() if admin else developer()
    who.id = ALICE
    return command, FakeInteraction(user=who), verification, service, repository


class TestWhoMayEvenAsk:
    async def test_run_outside_a_server_it_says_so(self) -> None:
        """`guild_only` keeps this out of a direct message, and the check stays anyway: the
        decorator is Discord's and this is what happens if it is ever removed or not enforced."""
        command, interaction, _, service, repo = run_it()
        interaction.guild_id = None

        await command.callback(interaction, repo)

        assert interaction.reply == "Run this inside a server channel."
        assert service.calls == []

    async def test_somebody_with_no_role_is_refused(self) -> None:
        command, interaction, _, service, repo = run_it(admin=False)

        await command.callback(interaction, repo)

        assert "You need one of these roles" in interaction.reply
        assert service.calls == []

    async def test_a_deployment_that_cannot_verify_anybody_refuses_rather_than_guessing(
        self,
    ) -> None:
        """It will not fall back to a Discord role. The role is what this command exists to stop
        being sufficient, so a half-configured deployment must do nothing rather than less."""
        command, interaction, _, service, repo = run_it(
            verification=FakeVerification(configured=False)
        )

        await command.callback(interaction, repo)

        assert "cannot verify who you are on GitHub" in interaction.reply
        assert service.calls == []


class TestTheFirstRun:
    async def test_somebody_who_has_not_proved_anything_gets_a_link(self) -> None:
        verification = FakeVerification(proved=None)
        command, interaction, _, _, repo = run_it(verification=verification)

        await command.callback(interaction, repo)

        assert "authorize" in interaction.reply
        assert verification.links_handed_out == 1

    async def test_nothing_is_unregistered_on_that_run(self) -> None:
        command, interaction, _, service, repo = run_it(verification=FakeVerification(proved=None))

        await command.callback(interaction, repo)

        assert service.calls == []


class TestTheSecondRun:
    async def test_a_recent_proof_finishes_the_job(self) -> None:
        service = FakeUnregistration()
        command, interaction, _, _, repo = run_it(service=service)

        await command.callback(interaction, repo)

        assert service.calls == [(1, REPO, "octocat")]
        assert "no longer mirrored" in interaction.reply

    async def test_the_login_it_acts_on_is_the_proved_one(self) -> None:
        """Never one out of `user_links`. That table is written by `/link`, which checks only that
        a login exists, so anybody with the Admin role can claim to be the repository owner."""
        service = FakeUnregistration()
        command, interaction, _, _, repo = run_it(
            verification=FakeVerification(proved="somebody-else"), service=service
        )

        await command.callback(interaction, repo)

        assert service.calls[0][2] == "somebody-else"

    async def test_orphaned_threads_are_named_because_they_are_the_surprise(self) -> None:
        service = FakeUnregistration(outcome=UnregisterOutcome(full_name=REPO, threads_orphaned=12))
        command, interaction, _, _, repo = run_it(service=service)

        await command.callback(interaction, repo)

        assert "12 threads" in interaction.reply
        assert "opens new ones" in interaction.reply

    async def test_one_orphan_is_one_thread(self) -> None:
        service = FakeUnregistration(outcome=UnregisterOutcome(full_name=REPO, threads_orphaned=1))
        command, interaction, _, _, repo = run_it(service=service)

        await command.callback(interaction, repo)

        assert "1 thread " in interaction.reply

    async def test_nothing_open_says_nothing_about_threads(self) -> None:
        command, interaction, _, _, repo = run_it()

        await command.callback(interaction, repo)

        assert "thread" not in interaction.reply


class TestWhatItRefuses:
    async def test_a_server_with_nothing_registered(self) -> None:
        command, interaction, _, _, repo = run_it(
            service=FakeUnregistration(error=NotRegisteredError("This server has no repository."))
        )

        await command.callback(interaction, repo)

        assert interaction.reply == "This server has no repository."

    async def test_a_name_that_does_not_match_what_is_registered(self) -> None:
        """The confirmation. It is irreversible and it cascades, so making somebody name the thing
        is the cheapest guard there is against the command being run by accident."""
        command, interaction, _, _, _ = run_it(
            service=FakeUnregistration(
                error=RepositoryMismatchError("This server is registered to acme/widget.")
            ),
            repository="acme/something-else",
        )

        await command.callback(interaction, "acme/something-else")

        assert "registered to acme/widget" in interaction.reply

    async def test_somebody_who_is_not_an_admin_on_the_repository(self) -> None:
        """The whole point. They proved who they are and it was not enough, which is a different
        answer from not having proved anything."""
        command, interaction, _, _, repo = run_it(
            service=FakeUnregistration(
                error=NotProvenError("You are signed in as octocat, who does not have admin.")
            )
        )

        await command.callback(interaction, repo)

        assert "does not have admin" in interaction.reply
