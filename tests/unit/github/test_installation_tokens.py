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
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shannon.github.errors import GitHubAuthError
from shannon.github.installations import (
    MAX_INSTALLATION_PAGES,
    REFRESH_MARGIN,
    InstallationTokens,
    Knows,
    MapSays,
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
    """The installation map, out of a dictionary, with the same three answers as the real one.

    `remember` and `forget` write into the dictionary `installation_for` reads, rather than
    only recording the call, because what this fake exists to prove is that the SECOND call
    comes out of the map. A fake that logged the write and answered the same way again would
    have agreed with the bug this file was written to catch.
    """

    def __init__(self, **owners: int) -> None:
        self.owners: dict[str, MapSays] = {
            login: MapSays(Knows.AN_INSTALLATION, installation)
            for login, installation in owners.items()
        }
        self.calls: list[str] = []
        self.kept: list[tuple[str, int, int | None, bool]] = []
        self.forgotten: list[int] = []

    async def installation_for(self, owner: str) -> MapSays:
        self.calls.append(owner)
        # Lowered, the way the store keys every row, so a test can ask in the capitals
        # somebody typed and still be answered.
        return self.owners.get(owner.strip().lower(), MapSays(Knows.NOTHING))

    async def remember(
        self,
        *,
        installation_id: int,
        account_login: str,
        account_id: int | None,
        suspended: bool,
    ) -> None:
        self.kept.append((account_login, installation_id, account_id, suspended))
        self.owners[account_login.strip().lower()] = (
            MapSays(Knows.A_SUSPENSION)
            if suspended
            else MapSays(Knows.AN_INSTALLATION, installation_id)
        )

    async def forget(self, installation_id: int) -> None:
        self.forgotten.append(installation_id)
        self.owners = {
            login: says
            for login, says in self.owners.items()
            if says.installation_id != installation_id
        }

    def suspend(self, owner: str) -> FakeDirectory:
        """The state the suspend webhook leaves behind: a row that is there and refused."""
        self.owners[owner.strip().lower()] = MapSays(Knows.A_SUSPENSION)
        return self


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


def pages(*of_rows: list[object], token: str = "ghs_abc"):
    """A transport serving the installation list across several pages, and the mint.

    Each argument is one page, joined by the `Link` header GitHub sends and the client follows.
    Worth a helper of its own because reading one page and reporting "not installed" states
    something that was never observed, and only a second page can prove it does not.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path != "/app/installations":
            return httpx.Response(
                201,
                content=json.dumps(
                    {
                        "token": token,
                        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )

        at = int(request.url.params.get("page", 1))
        headers = (
            {"Link": f'<https://api.github.com/app/installations?page={at + 1}>; rel="next"'}
            if at < len(of_rows)
            else {}
        )
        return httpx.Response(200, content=json.dumps(of_rows[at - 1]), headers=headers)

    return handler, seen


def lists(*rows: object, token: str = "ghs_abc"):
    """A transport answering both the installation list and the mint, routed by path.

    One transport for both because the real client has one, and the recovery is a request made
    on the way to another: a test that stubbed them apart could not see the order or the count.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/app/installations":
            return httpx.Response(200, content=json.dumps(list(rows)))
        return httpx.Response(
            201,
            content=json.dumps(
                {
                    "token": token,
                    "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                }
            ),
        )

    return handler, seen


def installed(
    login: str = "octocat",
    *,
    installation: int = 42,
    account_id: object = 1,
    suspended: bool = False,
) -> dict[str, object]:
    """One row of GitHub's installation list, the fields this reads off it."""
    account: dict[str, object] = {"login": login}
    if account_id is not None:
        account["id"] = account_id
    return {
        "id": installation,
        "account": account,
        "suspended_at": "2026-09-17T11:00:00Z" if suspended else None,
    }


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


class TestRecoveringAnInstallationNobodyWroteDown:
    """The map is a cache and GitHub is the source, which is what `for_owner` always said.

    `installation.created` is sent once and never again for an App that is already installed.
    So an App installed before this process existed, a delivery missed during an outage, or a
    database restored from before the install all leave the map empty for an owner that IS
    installed - and every call for that owner then went out anonymous, for ever. A private
    repository reads as a repository that does not exist, and the only cure anybody could name
    was uninstalling the App and installing it again.
    """

    async def test_an_owner_the_map_never_heard_of_is_asked_of_github(self) -> None:
        handler, _ = lists(installed())

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == "ghs_abc"

    async def test_it_asks_for_the_whole_list_rather_than_one_kind_of_owner(self) -> None:
        """The per-owner endpoints are split into `/users/` and `/orgs/`, so asking one
        means already knowing which kind of account this is - which is the thing being looked
        up. The list is kind-agnostic."""
        handler, seen = lists(installed())

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("octocat")

        assert [request.url.path for request in seen] == [
            "/app/installations",
            "/app/installations/42/access_tokens",
        ]

    async def test_it_is_asked_with_the_app_jwt(self) -> None:
        """There is no installation token yet: this is where one comes from."""
        handler, seen = lists(installed())

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("octocat")

        assert seen[0].headers["Authorization"].count(".") == 2, "that is not a JWT"

    async def test_what_github_said_is_written_down(self) -> None:
        """Without this the recovery happens again on every call, which turns one missed
        webhook into a permanent extra request per event."""
        handler, _ = lists(installed(installation=99, account_id=7))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            await tokens.token_for("octocat")

        assert directory.kept == [("octocat", 99, 7, False)]

    async def test_a_second_call_does_not_ask_github_again(self) -> None:
        handler, seen = lists(installed())

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("octocat")
            await tokens.token_for("octocat")

        assert [request.url.path for request in seen].count("/app/installations") == 1

    async def test_an_owner_already_in_the_map_is_not_looked_up(self) -> None:
        """The recovery is for a miss. Asking on every call would spend a request per
        event on a question the database had already answered."""
        handler, seen = lists(installed())

        async with minting(handler, directory=FakeDirectory(octocat=42)) as tokens:
            await tokens.token_for("octocat")

        assert [request.url.path for request in seen] == ["/app/installations/42/access_tokens"]

    async def test_the_login_is_matched_whatever_capitals_github_kept(self) -> None:
        """GitHub keeps the capitals somebody typed when they made the account, and the
        owner here comes off a webhook payload or a parsed link. Matching exactly would leave
        the recovery failing for exactly the accounts that need it."""
        handler, _ = lists(installed("OctoCat"))

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == "ghs_abc"

    async def test_the_right_account_is_picked_out_of_several(self) -> None:
        handler, seen = lists(
            installed("someone-else", installation=1),
            installed("hubot", installation=2),
            installed("octocat", installation=3),
        )

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("hubot")

        assert seen[1].url.path == "/app/installations/2/access_tokens"

    async def test_an_account_github_gave_no_id_for_is_still_written_down(self) -> None:
        """The account id only tells a rename apart from somebody taking a freed name.
        Worth keeping where GitHub sends one, and not worth refusing the row over."""
        handler, _ = lists(installed(account_id=None))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("octocat") == "ghs_abc"

        assert directory.kept == [("octocat", 42, None, False)]

    async def test_an_account_id_that_is_not_a_number_is_dropped(self) -> None:
        handler, _ = lists(installed(account_id="1"))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            await tokens.token_for("octocat")

        assert directory.kept == [("octocat", 42, None, False)]

    async def test_an_owner_github_does_not_list_is_no_token(self) -> None:
        handler, seen = lists(installed("somebody-else"))

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("stranger") == ""

        assert [request.url.path for request in seen] == ["/app/installations"], (
            "it tried to mint against an installation GitHub does not have"
        )

    async def test_an_owner_github_does_not_list_says_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler, _ = lists()

        with caplog.at_level(logging.INFO):
            async with minting(handler, directory=FakeDirectory()) as tokens:
                await tokens.token_for("stranger")

        assert "no installation" in caplog.text

    async def test_a_refusal_is_not_remembered(self) -> None:
        """Deliberately asked again. Caching "not installed" is the bug this whole path
        exists to undo, and an App installed a minute from now has to be found without a
        restart."""
        handler, seen = lists()

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("stranger")
            await tokens.token_for("stranger")

        assert len(seen) == 2

    async def test_a_suspended_installation_is_written_down_and_still_reads_nothing(
        self,
    ) -> None:
        """Minting against a suspended installation fails, so the id is no use today -
        but the App being installed here is worth keeping, exactly as the suspend webhook
        writes the row before applying the suspension. What the write buys is the NEXT call:
        the map answers "a suspension" and nothing asks GitHub again. `TestAnAppSomebodyPaused`
        is where that half is proved."""
        handler, seen = lists(installed(suspended=True))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("octocat") == ""

        assert directory.kept == [("octocat", 42, 1, True)]
        assert [request.url.path for request in seen] == ["/app/installations"]

    async def test_a_suspended_installation_says_so(self, caplog: pytest.LogCaptureFixture) -> None:
        handler, _ = lists(installed(suspended=True))

        with caplog.at_level(logging.INFO):
            async with minting(handler, directory=FakeDirectory()) as tokens:
                await tokens.token_for("octocat")

        assert "suspended" in caplog.text

    async def test_it_says_what_it_wrote_down(self, caplog: pytest.LogCaptureFixture) -> None:
        """The one line that tells somebody their bot repaired itself, rather than leaving
        them to wonder which webhook finally arrived."""
        handler, _ = lists(installed())

        with caplog.at_level(logging.INFO):
            async with minting(handler, directory=FakeDirectory()) as tokens:
                await tokens.token_for("octocat")

        assert "had not been told" in caplog.text


class TestAnAppSomebodyPaused:
    """The map HAS this account and is refusing it, which is not the same as having nothing.

    Both used to answer None, and the fallback could not tell them apart - so a suspended App
    was re-listed from GitHub and its row re-written, identically, on every single call, for as
    long as it stayed paused. Not a wasted GET either: `remember` opens a write transaction and
    takes a per-account advisory lock, so a read path that previously did no writing at all
    began serialising every caller for that owner on one Postgres lock.

    A suspension is an answer. Only nothing is a question.
    """

    async def test_a_suspended_owner_is_not_asked_about(self) -> None:
        handler, seen = lists(installed(suspended=True))

        async with minting(handler, directory=FakeDirectory().suspend("octocat")) as tokens:
            assert await tokens.token_for("octocat") == ""

        assert seen == [], "it went to GitHub about an App it already knows is paused"

    async def test_a_suspended_owner_is_not_written_down_again(self) -> None:
        handler, _ = lists(installed(suspended=True))
        directory = FakeDirectory().suspend("octocat")

        async with minting(handler, directory=directory) as tokens:
            await tokens.token_for("octocat")

        assert directory.kept == []

    async def test_learning_of_a_suspension_settles_it_for_the_next_call(self) -> None:
        """The write is the whole return on discovering a suspended installation. Without
        it the map answers "nothing" again next call and the list is read again; with it the
        map answers "a suspension" and `token_for` stops before discovery is reached.

        Two calls, deliberately. One call cannot see a loop."""
        handler, seen = lists(installed(suspended=True))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("octocat") == ""
            assert await tokens.token_for("octocat") == ""

        assert [request.url.path for request in seen] == ["/app/installations"], (
            "the second call asked GitHub about a suspension the first one wrote down"
        )
        assert len(directory.kept) == 1

    async def test_unsuspending_makes_it_resolve_without_a_restart(self) -> None:
        handler, _ = lists(installed())
        directory = FakeDirectory().suspend("octocat")

        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("octocat") == ""
            # What `installation.unsuspend` does to the row.
            await directory.remember(
                installation_id=42,
                account_login="octocat",
                account_id=None,
                suspended=False,
            )
            assert await tokens.token_for("octocat") == "ghs_abc"

    async def test_a_burst_learning_of_a_suspension_together_asks_once(self) -> None:
        """The other half of re-reading the map inside the discovery lock. Sequentially the second
        call never reaches discovery at all, because `token_for` stops on the suspension first - so
        only a burst can reach the arm where a caller takes the lock and finds a suspension
        somebody ahead of it has just written down. Without that arm it would list again."""
        everybody_here = asyncio.Event()
        seen: list[httpx.Request] = []
        directory = FakeDirectory()

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            await everybody_here.wait()
            return httpx.Response(200, content=json.dumps([installed(suspended=True)]))

        async with minting(handler, directory=directory) as tokens:
            work = [asyncio.create_task(tokens.token_for("octocat")) for _ in range(5)]
            await asyncio.sleep(0)
            everybody_here.set()
            results = await asyncio.gather(*work)

        assert set(results) == {""}
        assert len(seen) == 1, f"listed the app's installations {len(seen)} times"
        assert len(directory.kept) == 1


class TestAnInstallationGitHubNoLongerHas:
    """A row naming an installation that has gone is worse than no row at all.

    It answers confidently, so discovery never runs, and the mint 404s. A 404 mints no token,
    so there is nothing to put in the token cache and nothing absorbs the repeat: every later
    call posts the same doomed request. A reinstall keeps the login and issues a NEW id, so
    this is exactly what a missed `installation.deleted` plus a missed `installation.created`
    leaves behind - the same missed-delivery premise the whole fallback is built on.
    """

    async def test_a_404_on_the_mint_forgets_the_row(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        directory = FakeDirectory(octocat=42)
        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("octocat") == ""

        assert directory.forgotten == [42]

    async def test_the_next_call_finds_the_id_that_replaced_it(self) -> None:
        """The point of forgetting. Two GitHub answers in sequence: the stale id 404s and
        is dropped, then the list names the new one and the token comes back - all without a
        restart and without anybody reinstalling the App."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/app/installations":
                return httpx.Response(200, content=json.dumps([installed(installation=99)]))
            if request.url.path == "/app/installations/42/access_tokens":
                return httpx.Response(404, content=json.dumps({"message": "Not Found"}))
            return httpx.Response(
                201,
                content=json.dumps(
                    {
                        "token": "ghs_new",
                        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )

        async with minting(handler, directory=FakeDirectory(octocat=42)) as tokens:
            assert await tokens.token_for("octocat") == ""
            assert await tokens.token_for("octocat") == "ghs_new"

        assert [request.url.path for request in seen] == [
            "/app/installations/42/access_tokens",
            "/app/installations",
            "/app/installations/99/access_tokens",
        ]


class TestReadingMoreThanOnePageOfInstallations:
    """One page is not the list, it is the first page.

    GitHub's maximum is a hundred rows and it documents no order, so which accounts land on
    page one is arbitrary. Stopping there and logging "GitHub lists no installation covering
    this account" asserts something that was never observed, and sends the owner back to the
    anonymous request this whole path exists to prevent.
    """

    async def test_an_owner_on_the_second_page_is_found(self) -> None:
        handler, _ = pages(
            [installed("somebody-else", installation=1)],
            [installed("octocat", installation=42)],
        )

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == "ghs_abc"

    async def test_it_stops_once_the_owner_turns_up(self) -> None:
        """Pages are read until the answer is found, not exhaustively. A bot installed on
        a thousand accounts should not read ten pages to authorise one request."""
        handler, seen = pages(
            [installed("octocat", installation=42)],
            [installed("somebody-else", installation=1)],
        )

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("octocat")

        listings = [request for request in seen if request.url.path == "/app/installations"]
        assert len(listings) == 1

    async def test_the_cursor_replaces_the_original_parameters(self) -> None:
        """The next URL carries the page already. Sending `per_page` beside it again is how
        a paged read quietly starts over, which the client says in the same words."""
        handler, seen = pages(
            [installed("somebody-else", installation=1)],
            [installed("octocat", installation=42)],
        )

        async with minting(handler, directory=FakeDirectory()) as tokens:
            await tokens.token_for("octocat")

        assert seen[0].url.params.get("per_page") == "100"
        assert seen[1].url.params.get("page") == "2"
        assert "per_page" not in seen[1].url.params

    async def test_a_list_that_never_ends_is_given_up_on_and_said_so(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A Link header pointing past itself for ever, which is indistinguishable from a
        very long list because the cursor is opaque. Bounded rather than trusted, and it says
        when it stopped: a truncation logged as an answer is how somebody ends up looking for
        the wrong thing entirely."""
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                content=json.dumps([installed("somebody-else", installation=1)]),
                headers={"Link": '<https://api.github.com/app/installations?page=9>; rel="next"'},
            )

        with caplog.at_level(logging.WARNING):
            async with minting(handler, directory=FakeDirectory()) as tokens:
                assert await tokens.token_for("octocat") == ""

        assert len(seen) == MAX_INSTALLATION_PAGES
        assert "never settled" in caplog.text


class TestWhoseLoginGetsWrittenDown:
    """GitHub's, not the caller's.

    The store keys every row on `account_login.strip().lower()`, so the row has to be written
    under a spelling that can be read back. Writing the string the caller asked about put that
    at the mercy of whatever the caller had - and the owner is not always a login this bot
    vouched for: a `/pr` or `/issue` link's owner arrives in whatever case it was pasted in.
    """

    async def test_the_login_github_gave_is_the_one_kept(self) -> None:
        handler, _ = lists(installed("OctoCat"))
        directory = FakeDirectory()

        async with minting(handler, directory=directory) as tokens:
            await tokens.token_for("octocat")

        assert directory.kept == [("OctoCat", 42, 1, False)]

    async def test_an_owner_that_only_case_folds_onto_a_login_is_not_a_match(self) -> None:
        """U+017F LATIN SMALL LETTER LONG S casefolds to `s` and `lower()` leaves it alone, so
        `"\u017fomebody".casefold()` is exactly `"somebody"` while its `lower()` is not.

        Which is the whole test: under a casefold match this selects the REAL `somebody` row and
        writes that account's installation id back under a login GitHub has never heard of.
        Matching the way the store keys its rows is what closes it, and nothing is lost, because
        GitHub logins are ASCII - which is precisely why the two agree on every real one.

        No caller can hand this a non-ASCII owner today: a pasted link's owner and `/board link`'s
        are both held to ASCII first. So this is defence in depth - it keeps the match in line
        with how the store keys its rows, whatever a future caller passes.
        """
        handler, _ = lists(installed("somebody"))
        directory = FakeDirectory()
        assert "\u017fomebody".casefold() == "somebody", "the test's own premise"
        assert "\u017fomebody".lower() != "somebody"

        async with minting(handler, directory=directory) as tokens:
            assert await tokens.token_for("\u017fomebody") == ""

        assert directory.kept == [], "it wrote a real installation under a made-up login"


class TestABurstOnAColdMap:
    """The argument the mint lock makes, one step earlier, where nothing answered it.

    Discovery runs on a miss, so a map with nothing in it - the state this whole path exists
    for - means every caller misses together. The mint lock cannot cover it: that lock is keyed
    on an installation id, which is the thing not known yet.
    """

    async def test_ten_callers_for_one_unknown_owner_ask_github_once(self) -> None:
        everybody_here = asyncio.Event()
        seen: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/app/installations":
                await everybody_here.wait()
                return httpx.Response(200, content=json.dumps([installed()]))
            return httpx.Response(
                201,
                content=json.dumps(
                    {
                        "token": "ghs_abc",
                        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )

        async with minting(handler, directory=FakeDirectory()) as tokens:
            work = [asyncio.create_task(tokens.token_for("octocat")) for _ in range(10)]
            await asyncio.sleep(0)
            everybody_here.set()
            results = await asyncio.gather(*work)

        listings = [r for r in seen if r.url.path == "/app/installations"]
        assert len(listings) == 1, f"listed the app's installations {len(listings)} times"
        assert set(results) == {"ghs_abc"}

    async def test_the_callers_behind_the_first_take_the_row_it_wrote(self) -> None:
        """Re-read inside the lock, exactly as the mint re-checks its cache. Without it the
        queue behind the first caller each takes its own turn at GitHub once the lock frees."""
        everybody_here = asyncio.Event()
        directory = FakeDirectory()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/app/installations":
                await everybody_here.wait()
                return httpx.Response(200, content=json.dumps([installed()]))
            return httpx.Response(
                201,
                content=json.dumps(
                    {
                        "token": "ghs_abc",
                        "expires_at": (NOW + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
                    }
                ),
            )

        async with minting(handler, directory=directory) as tokens:
            work = [asyncio.create_task(tokens.token_for("octocat")) for _ in range(5)]
            await asyncio.sleep(0)
            everybody_here.set()
            await asyncio.gather(*work)

        assert len(directory.kept) == 1, f"wrote the row {len(directory.kept)} times"


class TestWhenTheRecoveryItselfCannotBeDone:
    """Logged and given up on, never raised. Nobody asked this question: it is a repair
    attempted on the way past a cache miss, and failing it should leave the caller with the
    anonymous request it was about to make anyway rather than an exception out of a path that
    did not exist yesterday.
    """

    @pytest.mark.parametrize("status", [401, 403, 500])
    async def test_github_refusing_the_list_is_no_token(self, status: int) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, content=json.dumps({"message": "nope"}))

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == ""

    async def test_github_refusing_the_list_says_where_to_look(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, content=json.dumps({"message": "Bad credentials"}))

        with caplog.at_level(logging.WARNING):
            async with minting(handler, directory=FakeDirectory()) as tokens:
                await tokens.token_for("octocat")

        assert "id and private key" in caplog.text

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"total_count": 1}, id="an object rather than a list"),
            pytest.param(["octocat"], id="a row that is not an object"),
            pytest.param([{"id": 42}], id="a row with no account"),
            pytest.param([{"id": 42, "account": "octocat"}], id="an account that is a string"),
            pytest.param([{"id": 42, "account": {}}], id="an account with no login"),
            pytest.param([{"id": 42, "account": {"login": 7}}], id="a login that is a number"),
            pytest.param([{"account": {"login": "octocat"}}], id="a row with no installation id"),
            pytest.param(
                [{"id": "42", "account": {"login": "octocat"}}], id="an id that is a string"
            ),
        ],
    )
    async def test_a_body_that_is_not_what_it_should_be_is_no_token(self, body: object) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, content=json.dumps(body))

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == ""

        assert len(seen) == 1, "it minted against something it had not read"

    async def test_a_list_that_is_not_json_is_no_token(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>an outage page</html>")

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("octocat") == ""


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

    async def test_an_account_neither_the_map_nor_github_knows_is_no_token(self) -> None:
        """One request, and it is the recovery. It used to be none: a miss read as
        "not installed" without anybody being asked."""
        handler, seen = lists()

        async with minting(handler, directory=FakeDirectory()) as tokens:
            assert await tokens.token_for("stranger") == ""

        assert [request.url.path for request in seen] == ["/app/installations"]

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
