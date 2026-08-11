from __future__ import annotations

import json
from collections.abc import Callable

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
