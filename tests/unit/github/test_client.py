from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from shannon.github.client import HttpGitHubClient
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
)
from tests.support import github_payloads as payloads


def client_with(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGitHubClient:
    transport = httpx.MockTransport(handler)
    return HttpGitHubClient(
        http_client=httpx.AsyncClient(transport=transport, base_url="https://api.github.com")
    )


def responds(status: int, body: object = None, headers: dict[str, str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body), headers=headers)

    return handler


async def test_get_repository_returns_a_snapshot() -> None:
    async with client_with(responds(200, payloads.repository())) as client:
        repo = await client.get_repository(payloads.OWNER, payloads.REPO)

    assert repo.github_repo_id == payloads.REPO_ID
    assert repo.full_name == f"{payloads.OWNER}/{payloads.REPO}"
    assert repo.html_url == f"https://github.com/{payloads.OWNER}/{payloads.REPO}"


async def test_get_repository_calls_the_right_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, content=json.dumps(payloads.repository()))

    async with client_with(handler) as client:
        await client.get_repository("owner", "repo")

    assert seen == ["/repos/owner/repo"]


async def test_get_pull_request_returns_a_snapshot() -> None:
    async with client_with(responds(200, payloads.pull_request())) as client:
        pr = await client.get_pull_request(payloads.OWNER, payloads.REPO, 7)

    assert pr.number == 7
    assert pr.title == "Add the webhook endpoint"
    assert pr.state == "open"
    assert pr.author is not None and pr.author.login == "octocat"
    assert [a.login for a in pr.assignees] == ["hubot"]
    assert [r.login for r in pr.reviewers] == ["monalisa"]
    assert pr.label_names == ("backend",)
    assert pr.repository.github_repo_id == payloads.REPO_ID
    assert pr.updated_at is not None


async def test_merged_pull_request_is_flagged() -> None:
    body = payloads.pull_request(state="closed", merged=True, merged_at="2026-08-10T13:00:00Z")
    async with client_with(responds(200, body)) as client:
        pr = await client.get_pull_request(payloads.OWNER, payloads.REPO, 7)

    assert pr.merged is True
    assert pr.state == "closed"


async def test_pull_request_without_embedded_repository_falls_back_to_a_second_call() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/pulls/7"):
            body = payloads.pull_request()
            body.pop("base")
            return httpx.Response(200, content=json.dumps(body))
        return httpx.Response(200, content=json.dumps(payloads.repository()))

    async with client_with(handler) as client:
        pr = await client.get_pull_request(payloads.OWNER, payloads.REPO, 7)

    assert pr.repository.github_repo_id == payloads.REPO_ID
    assert len(calls) == 2


async def test_missing_repository_raises_not_found() -> None:
    async with client_with(responds(404, {"message": "Not Found"})) as client:
        with pytest.raises(GitHubNotFoundError):
            await client.get_repository("owner", "nope")


async def test_bad_credentials_raise_auth_error() -> None:
    async with client_with(responds(401, {"message": "Bad credentials"})) as client:
        with pytest.raises(GitHubAuthError):
            await client.get_repository("owner", "repo")


async def test_spent_rate_limit_raises_rate_limit_error() -> None:
    handler = responds(403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0"})
    async with client_with(handler) as client:
        with pytest.raises(GitHubRateLimitError):
            await client.get_repository("owner", "repo")


async def test_secondary_rate_limit_carries_retry_after() -> None:
    async with client_with(
        responds(429, {"message": "slow down"}, {"retry-after": "60"})
    ) as client:
        with pytest.raises(GitHubRateLimitError) as caught:
            await client.get_repository("owner", "repo")

    assert caught.value.retry_after == 60


async def test_forbidden_without_rate_limit_header_is_an_auth_error() -> None:
    async with client_with(responds(403, {"message": "Forbidden"})) as client:
        with pytest.raises(GitHubAuthError):
            await client.get_repository("owner", "repo")


async def test_server_error_raises_unavailable() -> None:
    async with client_with(responds(502, {"message": "Bad gateway"})) as client:
        with pytest.raises(GitHubUnavailableError):
            await client.get_repository("owner", "repo")


async def test_network_failure_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with client_with(handler) as client:
        with pytest.raises(GitHubUnavailableError, match="Could not reach GitHub"):
            await client.get_repository("owner", "repo")


async def test_non_json_body_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>maintenance</html>")

    async with client_with(handler) as client:
        with pytest.raises(GitHubUnavailableError, match="non-JSON"):
            await client.get_repository("owner", "repo")


async def test_unusable_repository_body_raises_unavailable() -> None:
    async with client_with(responds(200, {"nothing": "useful"})) as client:
        with pytest.raises(GitHubUnavailableError, match="unusable repository"):
            await client.get_repository("owner", "repo")


def test_token_is_sent_as_a_bearer_header() -> None:
    from shannon.github.client import _headers

    assert _headers("abc123")["Authorization"] == "Bearer abc123"
    assert "Authorization" not in _headers("")


class TestHowLongToWait:
    """The two headers GitHub answers with are not the same kind of number."""

    async def test_retry_after_is_already_a_delay(self) -> None:
        async with client_with(
            responds(429, {"message": "slow down"}, {"retry-after": "60"})
        ) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after == 60

    async def test_the_reset_header_is_a_moment_and_becomes_a_delay(self) -> None:
        """It is the epoch second the window reopens. Reported raw it reads as fifty-six years."""
        served = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(served.timestamp()) + 90),
            "date": format_datetime(served, usegmt=True),
        }

        async with client_with(responds(403, {"message": "rate limited"}, headers)) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after == 90

    async def test_a_window_that_has_already_reopened_asks_for_no_wait(self) -> None:
        served = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
        headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(served.timestamp()) - 30),
            "date": format_datetime(served, usegmt=True),
        }

        async with client_with(responds(403, {"message": "rate limited"}, headers)) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after == 0

    async def test_retry_after_wins_over_the_reset_moment(self) -> None:
        headers = {"retry-after": "12", "x-ratelimit-reset": "9999999999"}

        async with client_with(responds(429, {"message": "slow down"}, headers)) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after == 12

    async def test_neither_header_means_no_answer(self) -> None:
        async with client_with(
            responds(403, {"message": "rate limited"}, {"x-ratelimit-remaining": "0"})
        ) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after is None


def serves(issue: dict | None = None):
    """Answer both calls get_issue makes: the issue, then its repository.

    The issues endpoint carries no repository object, only a URL, so the client has to fetch it
    separately. A handler that answers everything with the issue body fails on the second call.
    """
    body = issue if issue is not None else payloads.issue()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/issues/" in request.url.path:
            return httpx.Response(200, content=json.dumps(body))
        return httpx.Response(200, content=json.dumps(payloads.repository()))

    return handler


class TestFetchingAnIssue:
    """The issues endpoint, whose body had never been executed by any test."""

    async def test_it_returns_a_snapshot(self) -> None:
        async with client_with(serves()) as client:
            issue = await client.get_issue(payloads.OWNER, payloads.REPO, 12)

        assert issue.number == 12
        assert issue.title == payloads.issue()["title"]
        assert issue.repository.github_repo_id == payloads.REPO_ID

    async def test_it_asks_the_issues_endpoint_then_the_repository(self) -> None:
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return serves()(request)

        async with client_with(handler) as client:
            await client.get_issue(payloads.OWNER, payloads.REPO, 12)

        assert paths == [
            f"/repos/{payloads.OWNER}/{payloads.REPO}/issues/12",
            f"/repos/{payloads.OWNER}/{payloads.REPO}",
        ]

    async def test_a_pull_request_served_from_this_endpoint_is_not_an_issue(self) -> None:
        """GitHub answers /issues/N for a pull request too. Tracking it as one would be wrong."""
        body = payloads.issue()
        body["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/12"}

        async with client_with(serves(body)) as client:
            with pytest.raises(GitHubNotFoundError, match="is a pull request, not an issue"):
                await client.get_issue(payloads.OWNER, payloads.REPO, 12)

    async def test_a_missing_issue_is_reported(self) -> None:
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            with pytest.raises(GitHubNotFoundError):
                await client.get_issue(payloads.OWNER, payloads.REPO, 999)

    async def test_a_body_it_cannot_read_is_reported(self) -> None:
        async with client_with(serves({"number": None})) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.get_issue(payloads.OWNER, payloads.REPO, 12)

    async def test_github_refusing_is_reported_rather_than_raised_raw(self) -> None:
        async with client_with(responds(500, {"message": "boom"})) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.get_issue(payloads.OWNER, payloads.REPO, 12)


class TestARepositoryThatMoved:
    """GitHub answers 301 for a renamed repository or owner, and for a transferred issue.

    That is a documented, ordinary answer. Unfollowed it lands in the catch-all and comes back
    to the person who ran the command as "GitHub could not be reached", so `/register` on the
    old link never succeeds. `/pr` is worse: after a rename the stored name is stale, so the
    guard that would have checked the id sees a match, skips the check, and asks for the old
    name, and the command stays broken until a webhook happens to arrive and correct the name.
    """

    def test_the_client_this_builds_follows_redirects(self) -> None:
        """Pinned separately because every other test here injects its own client."""
        client = HttpGitHubClient(token="t")

        assert client._client.follow_redirects is True

    async def test_a_renamed_repository_resolves_to_its_new_name(self) -> None:
        # full_name is built from the owner and the name rather than read, so those are what
        # have to move for the snapshot to report the new location.
        moved = payloads.repository()
        moved["name"] = "new-name"
        moved["owner"] = {"login": "acme", "id": 1, "type": "User"}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/new-name"):
                return httpx.Response(200, content=json.dumps(moved))
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repos/acme/new-name"}
            )

        client = HttpGitHubClient(
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://api.github.com",
                follow_redirects=True,
            )
        )

        assert (await client.get_repository("acme", "old-name")).full_name == "acme/new-name"
