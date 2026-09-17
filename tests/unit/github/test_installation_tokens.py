"""Minting an installation token, and not minting it again until it is nearly stale.

Issue #98. Every GitHub call now waits on this, so the two things worth being certain about are
that it does not mint per request and that a burst arriving together mints once between them. Both
are invisible from the outside: the wrong answer here is not an error, it is a rate limit spent
sixty times faster than it needs to be.

Driven through `httpx.MockTransport`, the way `test_client.py` drives the real client, so the
request GitHub would actually receive is the thing under test rather than a mock's call log.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shannon.github.errors import GitHubAuthError
from shannon.github.installations import (
    REFRESH_MARGIN,
    InstallationTokens,
)

pytestmark = pytest.mark.unit

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode("ascii")
CLIENT_ID = "Iv23liAbCdEfGhIjKlMn"
NOW = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)


class FakeDirectory:
    """Owner to installation, out of a dictionary. Records what it was asked."""

    def __init__(self, **owners: int | None) -> None:
        self.owners = owners
        self.calls: list[str] = []

    async def installation_for(self, owner: str) -> int | None:
        self.calls.append(owner)
        return self.owners.get(owner)


class Clock:
    """A clock a test moves by hand, because every cache decision here is about time."""

    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@asynccontextmanager
async def minting(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    directory: FakeDirectory | None = None,
    clock: Clock | None = None,
    client_id: str = CLIENT_ID,
    private_key_pem: str = PEM,
) -> AsyncIterator[InstallationTokens]:
    """The minter wired to a transport, with the HTTP client closed afterwards.

    A context manager here rather than on `InstallationTokens` itself: the real one is handed a
    client the container owns and closes, so giving it a lifecycle of its own would be API that
    exists only for tests.
    """
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    ) as http:
        yield InstallationTokens(
            client_id=client_id,
            private_key_pem=private_key_pem,
            http=http,
            directory=directory or FakeDirectory(octocat=42),
            now=clock or Clock(),
        )


def answers(token: str = "ghs_abc", *, minutes: int = 60, at: datetime = NOW):
    """A token response shaped the way GitHub sends one, and a log of the requests."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            201,
            content=json.dumps(
                {
                    "token": f"{token}-{len(seen)}",
                    "expires_at": (at + timedelta(minutes=minutes))
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
            ),
        )

    return handler, seen


class TestMintingOne:
    async def test_a_token_comes_back(self) -> None:
        handler, _ = answers()

        async with minting(handler) as tokens:
            assert await tokens.token_for("octocat") == "ghs_abc-1"

    async def test_it_asks_the_installation_it_was_told_about(self) -> None:
        handler, seen = answers()

        async with minting(handler, directory=FakeDirectory(octocat=42)) as tokens:
            await tokens.token_for("octocat")

        assert seen[0].url.path == "/app/installations/42/access_tokens"
        assert seen[0].method == "POST"

    async def test_the_mint_is_authorised_with_the_app_jwt(self) -> None:
        """A JWT rather than a token, which is the one place the App authenticates as itself.
        Getting this wrong answers 401 and reads downstream as every repository being missing."""
        handler, seen = answers()

        async with minting(handler) as tokens:
            await tokens.token_for("octocat")

        header = seen[0].headers["Authorization"]
        assert header.startswith("Bearer ")
        assert header.count(".") == 2, "that is not a JWT"


class TestNotMintingAgain:
    async def test_a_second_call_makes_no_request(self) -> None:
        """The whole point. Every GitHub call comes through here, so minting per request would
        spend the App's rate limit on authentication rather than on work."""
        handler, seen = answers()

        async with minting(handler) as tokens:
            first = await tokens.token_for("octocat")
            second = await tokens.token_for("octocat")

        assert first == second
        assert len(seen) == 1

    async def test_a_token_near_its_expiry_is_replaced_early(self) -> None:
        """A token handed out with two seconds left may arrive at GitHub expired. The margin is
        what stops a request failing for having been authorised slightly too late."""
        clock = Clock()
        handler, seen = answers(minutes=60)

        async with minting(handler, clock=clock) as tokens:
            await tokens.token_for("octocat")
            clock.at = NOW + timedelta(minutes=60) - REFRESH_MARGIN
            await tokens.token_for("octocat")

        assert len(seen) == 2

    async def test_a_token_comfortably_inside_its_life_is_kept(self) -> None:
        clock = Clock()
        handler, seen = answers(minutes=60)

        async with minting(handler, clock=clock) as tokens:
            await tokens.token_for("octocat")
            clock.at = NOW + timedelta(minutes=30)
            await tokens.token_for("octocat")

        assert len(seen) == 1

    async def test_two_accounts_get_two_tokens(self) -> None:
        """Cached per installation, not globally. One token for two accounts would hand a caller
        a credential for somebody else's repository, which is the whole thing this replaced."""
        handler, seen = answers()
        directory = FakeDirectory(octocat=42, hubot=99)

        async with minting(handler, directory=directory) as tokens:
            first = await tokens.token_for("octocat")
            second = await tokens.token_for("hubot")

        assert first != second
        assert [request.url.path for request in seen] == [
            "/app/installations/42/access_tokens",
            "/app/installations/99/access_tokens",
        ]

    async def test_a_burst_for_one_account_mints_once(self) -> None:
        """Ten deliveries for one repository arrive together and all miss the cache. Without the
        second check inside the lock they queue up and mint ten tokens, nine of which are thrown
        away and every one of which counts against the App's limit.

        The handler is held open until every caller has arrived, so the test fails on a build that
        happens to run them sequentially rather than passing by luck.
        """
        everybody_here = asyncio.Event()
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            await everybody_here.wait()
            return httpx.Response(
                201,
                content=json.dumps(
                    {
                        "token": "ghs_abc",
                        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )

        async with minting(handler) as tokens:
            work = [asyncio.create_task(tokens.token_for("octocat")) for _ in range(10)]
            await asyncio.sleep(0)
            everybody_here.set()
            results = await asyncio.gather(*work)

        assert len(seen) == 1, f"minted {len(seen)} times for one installation"
        assert set(results) == {"ghs_abc"}


class TestWhenThereIsNothingToMintAgainst:
    async def test_no_app_configured_is_no_token(self) -> None:
        """The state a deployment that has not set the App up is in. It has to read as "do
        without" rather than as a crash, which is how every other unset credential here behaves."""
        handler, seen = answers()

        async with minting(handler, client_id="") as tokens:
            assert await tokens.token_for("octocat") == ""

        assert seen == [], "it went to GitHub with no key to sign the request"

    async def test_no_private_key_is_no_token(self) -> None:
        handler, _ = answers()

        async with minting(handler, private_key_pem="") as tokens:
            assert await tokens.token_for("octocat") == ""

    async def test_an_account_with_no_installation_is_no_token(self) -> None:
        handler, seen = answers()

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("stranger") == ""

        assert seen == []

    async def test_an_installation_removed_since_the_lookup_is_no_token(self) -> None:
        """A 404 on the mint is somebody uninstalling between the directory read and this call.
        Permanent, and the same outcome as never having been installed, so it is not an error."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        async with minting(handler) as tokens:
            assert await tokens.token_for("octocat") == ""

    async def test_a_key_github_will_not_accept_raises(self) -> None:
        """The opposite of the 404. Reporting a rejected key as "no installation" would send
        somebody looking at their App's repository access instead of at the key."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=json.dumps({"message": "Bad credentials"}))

        async with minting(handler) as tokens:
            with pytest.raises(GitHubAuthError, match="id and private key"):
                await tokens.token_for("octocat")

    async def test_github_being_down_raises_rather_than_reading_as_uninstalled(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with minting(handler) as tokens:
            with pytest.raises(GitHubAuthError):
                await tokens.token_for("octocat")


class TestABodyThatIsNotWhatItShouldBe:
    """Refused rather than defaulted, because both plausible defaults are wrong for an hour."""

    @pytest.mark.parametrize(
        "body",
        [
            {"expires_at": "2026-09-17T13:00:00Z"},
            {"token": "", "expires_at": "2026-09-17T13:00:00Z"},
            {"token": 7, "expires_at": "2026-09-17T13:00:00Z"},
            {"token": "ghs_abc"},
            {"token": "ghs_abc", "expires_at": "not a time"},
            {"token": "ghs_abc", "expires_at": None},
            [],
        ],
    )
    async def test_a_token_with_no_token_or_no_expiry_is_refused(self, body: object) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, content=json.dumps(body))

        async with minting(handler) as tokens:
            with pytest.raises(GitHubAuthError, match="no token or no expiry"):
                await tokens.token_for("octocat")

    async def test_a_body_that_is_not_json_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, content=b"<html>an outage page</html>")

        async with minting(handler) as tokens:
            with pytest.raises(GitHubAuthError, match="non-JSON"):
                await tokens.token_for("octocat")

    async def test_a_refused_body_is_not_cached(self) -> None:
        """Otherwise one bad response poisons the account for an hour."""
        calls: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(201, content=json.dumps({"token": "ghs_abc"}))

        async with minting(handler) as tokens:
            for _ in range(2):
                with pytest.raises(GitHubAuthError):
                    await tokens.token_for("octocat")

        assert len(calls) == 2


class TestTheAppsOwnToken:
    async def test_it_is_a_jwt(self) -> None:
        handler, _ = answers()

        async with minting(handler) as tokens:
            assert tokens.app_token().count(".") == 2

    async def test_it_is_empty_when_no_app_is_configured(self) -> None:
        handler, _ = answers()

        async with minting(handler, client_id="") as tokens:
            assert tokens.app_token() == ""


def test_the_refresh_margin_is_generous_enough_to_matter() -> None:
    """Against literals. A margin of nothing is the bug this constant exists to prevent, and one
    approaching the hour GitHub grants would mint on every request instead."""
    assert timedelta(minutes=1) <= REFRESH_MARGIN
    assert timedelta(minutes=10) >= REFRESH_MARGIN


class TestAskingWhetherTheAppIsInstalled:
    """The question `/register` needs, which the directory cannot answer.

    A repository nobody has registered has no row, and a missing row deliberately means "ask
    GitHub" rather than "not installed". This is that ask.
    """

    async def test_an_installed_repository_answers_with_its_installation(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"id": 42}))

        async with minting(handler) as tokens:
            assert await tokens.installed_on("acme", "widget") == 42

    async def test_it_asks_the_right_path(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps({"id": 42}))

        async with minting(handler) as tokens:
            await tokens.installed_on("acme", "widget")

        assert seen[0].url.path == "/repos/acme/widget/installation"

    async def test_it_is_asked_with_the_app_jwt(self) -> None:
        """There is no token to use yet: finding the installation is what a token comes from."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps({"id": 42}))

        async with minting(handler) as tokens:
            await tokens.installed_on("acme", "widget")

        assert seen[0].headers["Authorization"].count(".") == 2

    async def test_a_name_with_a_slash_stays_one_path_segment(self) -> None:
        """Both halves come off a link somebody typed into Discord, and this is a path segment."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(404)

        async with minting(handler) as tokens:
            await tokens.installed_on("acme", "widget/../../secrets")

        assert b"/repos/acme/widget%2F..%2F..%2Fsecrets/installation" in seen[0].url.raw_path

    async def test_a_repository_the_app_cannot_see_answers_nothing(self) -> None:
        """404 covers both "no such repository" and "not installed there", and they are the same
        thing from here. Telling them apart would also say whether a private repository exists."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        async with minting(handler) as tokens:
            assert await tokens.installed_on("acme", "widget") is None

    async def test_no_app_configured_answers_nothing_without_asking(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps({"id": 42}))

        async with minting(handler, client_id="") as tokens:
            assert await tokens.installed_on("acme", "widget") is None

        assert seen == []

    async def test_a_rejected_key_raises_rather_than_reading_as_not_installed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=json.dumps({"message": "Bad credentials"}))

        async with minting(handler) as tokens:
            with pytest.raises(GitHubAuthError, match="id and private key"):
                await tokens.installed_on("acme", "widget")

    @pytest.mark.parametrize("body", [{}, {"id": "42"}, {"id": None}, []])
    async def test_a_body_with_no_usable_id_answers_nothing(self, body: object) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps(body))

        async with minting(handler) as tokens:
            assert await tokens.installed_on("acme", "widget") is None

    async def test_a_body_that_is_not_json_answers_nothing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>an outage page</html>")

        async with minting(handler) as tokens:
            assert await tokens.installed_on("acme", "widget") is None


class TestTheAppsOwnSlug:
    """Read from GitHub rather than configured, because it never changes and a setting for it is
    one more thing to type wrong in a way whose only symptom is a link that 404s."""

    async def test_it_comes_back(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"slug": "shannon-bot"}))

        async with minting(handler) as tokens:
            assert await tokens.app_slug() == "shannon-bot"

    async def test_it_is_read_once(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps({"slug": "shannon-bot"}))

        async with minting(handler) as tokens:
            await tokens.app_slug()
            await tokens.app_slug()

        assert len(seen) == 1

    async def test_a_failure_is_not_retried_on_every_register(self) -> None:
        """Empty and never-asked are different states on purpose. Without that, a GitHub outage
        would put an extra request in front of every `/register` for as long as it lasted."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps({}))

        async with minting(handler) as tokens:
            assert await tokens.app_slug() == ""
            assert await tokens.app_slug() == ""

        assert len(seen) == 1

    async def test_a_refusal_does_not_fail_the_command_behind_it(self) -> None:
        """The slug only makes a message more helpful. Failing `/register` because the link could
        not be prettified would be the wrong trade."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with minting(handler) as tokens:
            assert await tokens.app_slug() == ""

    async def test_no_app_configured_has_no_slug(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"slug": "shannon-bot"}))

        async with minting(handler, client_id="") as tokens:
            assert await tokens.app_slug() == ""
