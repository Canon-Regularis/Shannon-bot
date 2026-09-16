"""Reading every open pull request and issue off a repository, for `/refresh`.

Its own file rather than a fifteenth section of `test_client.py`, because what is interesting
here is not the HTTP — that is `get_pages`, tested there already — but which endpoint is asked
and what survives the trip. Issue #74.

The endpoint choice is the whole design and the test that proves it is
`test_a_pull_request_keeps_the_reviewers_it_was_waiting_on`. GitHub serves pull requests from the
issues endpoint too, which would be one call instead of two, and the rows it gives back are the
issue shape: no reviewers, no teams. Mirroring a backlog from those opens a thread on every pull
request saying nobody was asked to review it.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from shannon.domain.models import RepositorySnapshot
from shannon.github.client import HttpGitHubClient
from shannon.github.errors import GitHubNotFoundError, GitHubUnavailableError
from tests.support import github_payloads as payloads

pytestmark = pytest.mark.unit

REPOSITORY = RepositorySnapshot(
    github_repo_id=payloads.REPO_ID,
    owner=payloads.OWNER,
    name=payloads.REPO,
    html_url=f"https://github.com/{payloads.OWNER}/{payloads.REPO}",
)


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGitHubClient:
    return HttpGitHubClient(
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
            follow_redirects=True,
        )
    )


def responds(body: object, status: int = 200, headers: dict[str, str] | None = None):
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body), headers=headers)

    return handler


def recording(body: object) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.URL]]:
    asked: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url)
        return httpx.Response(200, content=json.dumps(body))

    return handler, asked


class TestWhatItAsksFor:
    async def test_pull_requests_come_from_the_pulls_endpoint(self) -> None:
        handler, asked = recording([])

        async with client_with(handler) as client:
            await client.list_open_pull_requests(REPOSITORY)

        assert asked[0].path == f"/repos/{payloads.OWNER}/{payloads.REPO}/pulls"

    async def test_issues_come_from_the_issues_endpoint(self) -> None:
        handler, asked = recording([])

        async with client_with(handler) as client:
            await client.list_open_issues(REPOSITORY)

        assert asked[0].path == f"/repos/{payloads.OWNER}/{payloads.REPO}/issues"

    async def test_it_asks_for_open_items_a_hundred_at_a_time_newest_first(self) -> None:
        """A hundred because a backlog is read whole and round trips are the cost. Newest first
        because a run that reaches its cap should spend it on what somebody is working on, and
        because it leaves the quietest items in the tail that `MAX_PAGES` cuts off.
        """
        handler, asked = recording([])

        async with client_with(handler) as client:
            await client.list_open_pull_requests(REPOSITORY)

        assert dict(asked[0].params) == {
            "state": "open",
            "per_page": "100",
            "sort": "updated",
            "direction": "desc",
        }

    async def test_the_repository_it_was_given_is_the_one_it_reads(self) -> None:
        """Not the owner and name of anything else in scope. A refresh resolves the repository
        first and hands the answer down, so a rename that happened since is followed."""
        renamed = RepositorySnapshot(
            github_repo_id=payloads.REPO_ID,
            owner="somebody-else",
            name="renamed",
            html_url="https://github.com/somebody-else/renamed",
        )
        handler, asked = recording([])

        async with client_with(handler) as client:
            await client.list_open_issues(renamed)

        assert asked[0].path == "/repos/somebody-else/renamed/issues"


class TestWhatComesBack:
    async def test_a_pull_request_keeps_the_reviewers_it_was_waiting_on(self) -> None:
        """Why this reads the pulls endpoint rather than the issues one, which also answers with
        pull requests. A pull request row there is the issue shape and carries neither of these.
        """
        row = payloads.pull_request()
        row["requested_reviewers"] = [payloads.user("hubot", 100)]
        row["requested_teams"] = [{"slug": "backend", "name": "Backend"}]

        async with client_with(responds([row])) as client:
            found = await client.list_open_pull_requests(REPOSITORY)

        assert [person.login for person in found[0].reviewers] == ["hubot"]
        assert [group.login for group in found[0].reviewer_teams] == ["backend"]

    async def test_an_open_pull_request_is_not_read_as_merged(self) -> None:
        """List rows carry no `merged` key at all, only `merged_at`, so the flag has to come out
        False from its absence rather than from anything being said."""
        row = payloads.pull_request()
        row.pop("merged", None)
        row["merged_at"] = None

        async with client_with(responds([row])) as client:
            found = await client.list_open_pull_requests(REPOSITORY)

        assert found[0].merged is False

    async def test_an_issue_carries_the_repository_it_was_asked_about(self) -> None:
        """GitHub's issue rows have no repository object, which is the whole reason this takes a
        snapshot rather than two strings."""
        async with client_with(responds([payloads.issue()])) as client:
            found = await client.list_open_issues(REPOSITORY)

        assert found[0].repository.github_repo_id == payloads.REPO_ID

    async def test_nothing_open_is_an_empty_answer_rather_than_a_failure(self) -> None:
        async with client_with(responds([])) as client:
            assert await client.list_open_pull_requests(REPOSITORY) == []


class TestWhatItLeavesOut:
    async def test_a_pull_request_in_the_issues_list_is_dropped(self) -> None:
        """GitHub serves pull requests from the issues endpoint and marks them with one key.
        Without this a refresh would track every pull request twice, once under each type."""
        async with client_with(responds([payloads.pull_request_as_issue()])) as client:
            assert await client.list_open_issues(REPOSITORY) == []

    async def test_a_row_that_cannot_be_read_is_skipped_rather_than_failing_the_page(self) -> None:
        """One unusable row is not a reason to abandon the other ninety-nine."""
        async with client_with(responds([{"nothing": "usable"}, payloads.issue()])) as client:
            found = await client.list_open_issues(REPOSITORY)

        assert [item.number for item in found] == [payloads.issue()["number"]]

    async def test_a_body_that_is_not_a_list_is_read_as_nothing(self) -> None:
        """GitHub answers a list endpoint with an array. Anything else is a bug or an outage, and
        iterating a dict here would silently read its keys as rows."""
        async with client_with(responds({"message": "Not Found"})) as client:
            assert await client.list_open_issues(REPOSITORY) == []


class TestReadingMoreThanOnePage:
    def pages(self, bodies: list[object]) -> Callable[[httpx.Request], httpx.Response]:
        sent = iter(bodies)

        def handler(request: httpx.Request) -> httpx.Response:
            body = next(sent)
            headers = {}
            if request.url.params.get("page") != "last":
                headers["Link"] = '<https://api.github.com/next?page=last>; rel="next"'
            return httpx.Response(200, content=json.dumps(body), headers=headers)

        return handler

    async def test_every_page_is_read(self) -> None:
        first = payloads.issue(number=1, id=8001)
        second = payloads.issue(number=2, id=8002)

        async with client_with(self.pages([[first], [second]])) as client:
            found = await client.list_open_issues(REPOSITORY)

        assert [item.number for item in found] == [1, 2]

    async def test_a_row_handed_back_twice_is_kept_once(self) -> None:
        """GitHub says outright that a list edited while it is being paged can repeat a row. Here
        that would open two threads for one item and count it twice on the way out, and the
        caller reads what is already mirrored once at the start so it could not tell.
        """
        repeated = payloads.issue(number=1, id=8001)

        async with client_with(self.pages([[repeated], [repeated]])) as client:
            found = await client.list_open_issues(REPOSITORY)

        assert [item.number for item in found] == [1]


class TestWhenGitHubRefuses:
    async def test_a_repository_that_is_gone_is_reported_as_such(self) -> None:
        async with client_with(responds({"message": "Not Found"}, status=404)) as client:
            with pytest.raises(GitHubNotFoundError):
                await client.list_open_pull_requests(REPOSITORY)

    async def test_a_body_that_is_not_json_is_an_outage(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>502</html>")

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.list_open_issues(REPOSITORY)
