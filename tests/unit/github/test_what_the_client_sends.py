"""What `HttpGitHubClient` puts on the wire, and how it files what GitHub answers. Issue #271.

Characterisation, written and run before #271 split the transport out from under the public
methods: the same request has to go out afterwards, byte for byte where it matters, and the same
answer has to come back as the same error with the same words. `test_client.py` holds the reasons
for each rule; this file holds the rules still, side by side, across entry points that each used to
spell the transport out for themselves, so folding them into one could not quietly drop a header.

Built the way the container builds it, with its own `httpx.AsyncClient`, so the headers it gives
itself are on every request seen. Only httpx's transport is swapped. The raw reads, which were the
client's when this was written, are asked of `GitHubTransport`, where #271 put them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from shannon.domain.models import RepositorySnapshot
from shannon.github.client import HttpGitHubClient
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from shannon.github.paging import PagedRead
from shannon.github.transport import GitHubTransport, SuppliesTokens
from tests.support import github_payloads as payloads
from tests.unit.github.test_client import FakeTokens

Handler = Callable[[httpx.Request], httpx.Response]

# What httpx adds for itself on the way out. Not this project's choice, and `accept-encoding`
# varies with which optional decoders happen to be installed, so pinning it would pin the venv.
_TRANSPORT_HEADERS = frozenset({"host", "accept-encoding", "connection", "content-length"})

# The three every request carries whoever it is about, as `HttpGitHubClient` builds its own
# client. Lower case, because that is how httpx hands a request's headers back.
EVERY_REQUEST = {
    "accept": "application/vnd.github+json",
    "x-github-api-version": "2022-11-28",
    "user-agent": "shannon-bot",
}


def the_real_transport(
    handler: Handler, *, tokens: SuppliesTokens | None = None
) -> GitHubTransport:
    """`GitHubTransport` building its own `httpx.AsyncClient`, wired to `handler`.

    `test_client.client_with` hands one in, which is what most tests want and exactly what this
    file cannot use: a client handed in keeps its own headers, so the ones this project sets on
    every request would never be seen. Patched only for the construction, so nothing else in the
    test builds a client through the stand-in.
    """
    built = httpx.AsyncClient

    def with_the_handler(**settings: Any) -> httpx.AsyncClient:
        return built(transport=httpx.MockTransport(handler), **settings)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "AsyncClient", with_the_handler)
        return GitHubTransport(tokens=tokens)


@asynccontextmanager
async def the_real_client(
    handler: Handler, *, tokens: SuppliesTokens | None = None
) -> AsyncIterator[HttpGitHubClient]:
    """`HttpGitHubClient` over that transport, as the container builds one."""
    async with the_real_transport(handler, tokens=tokens) as transport:
        yield HttpGitHubClient(transport=transport)


def headers_of(request: httpx.Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k not in _TRANSPORT_HEADERS}


def answering(*bodies: object, link_to: str | None = None) -> tuple[Handler, list[httpx.Request]]:
    """A handler that answers each request with the next body, and the requests it was sent.

    `link_to` is put on every answer but the last as GitHub's Link header, so a paged read has a
    second page to follow.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        last = len(seen) >= len(bodies)
        body = bodies[min(len(seen), len(bodies)) - 1]
        links = {} if last or link_to is None else {"Link": f'<{link_to}>; rel="next"'}
        return httpx.Response(200, content=json.dumps(body), headers=links)

    return handler, seen


def sent(request: httpx.Request) -> tuple[str, str, dict[str, str]]:
    return request.method, request.url.path, dict(request.url.params)


REPOSITORY = payloads.repository(
    name="widget", full_name="acme/widget", owner=payloads.user("acme")
)


class TestWhatGoesOut:
    async def test_a_repository_read_is_one_get_as_the_account_it_is_about(self) -> None:
        handler, seen = answering(REPOSITORY)

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.get_repository("acme", "widget")

        assert [sent(request) for request in seen] == [("GET", "/repos/acme/widget", {})]
        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}

    async def test_with_no_tokens_a_read_goes_out_with_no_credential_at_all(self) -> None:
        handler, seen = answering(REPOSITORY)

        async with the_real_client(handler) as client:
            await client.get_repository("acme", "widget")

        assert headers_of(seen[0]) == EVERY_REQUEST

    async def test_an_owner_with_no_installation_goes_out_with_no_credential(self) -> None:
        handler, seen = answering(REPOSITORY)

        async with the_real_client(handler, tokens=FakeTokens()) as client:
            await client.get_repository("acme", "widget")

        assert headers_of(seen[0]) == EVERY_REQUEST

    async def test_a_pull_request_read_carrying_its_repository_is_one_get(self) -> None:
        body = payloads.pull_request(base={"repo": REPOSITORY})
        handler, seen = answering(body)

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.get_pull_request("acme", "widget", 7)

        assert [sent(request) for request in seen] == [("GET", "/repos/acme/widget/pulls/7", {})]
        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}

    async def test_a_pull_request_read_without_its_repository_reads_that_second(self) -> None:
        body = payloads.pull_request()
        body.pop("base")
        handler, seen = answering(body, REPOSITORY)

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.get_pull_request("acme", "widget", 7)

        assert [sent(request) for request in seen] == [
            ("GET", "/repos/acme/widget/pulls/7", {}),
            ("GET", "/repos/acme/widget", {}),
        ]
        assert all(
            headers_of(request) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}
            for request in seen
        )

    async def test_a_paged_read_sends_its_query_once_and_then_follows_the_link_as_given(
        self,
    ) -> None:
        following = "https://api.github.com/repositories/1/labels?per_page=100&page=2"
        handler, seen = answering([{"name": "bug"}], [{"name": "docs"}], link_to=following)

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            found = await client.list_labels("acme", "widget")

        assert found == ["bug", "docs"]
        assert [sent(request) for request in seen] == [
            ("GET", "/repos/acme/widget/labels", {"per_page": "100"}),
            ("GET", "/repositories/1/labels", {"per_page": "100", "page": "2"}),
        ]
        assert all(
            headers_of(request) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}
            for request in seen
        )

    async def test_the_open_items_are_asked_for_most_recently_moved_first(self) -> None:
        handler, seen = answering([])
        repository = RepositorySnapshot(
            github_repo_id=1, owner="acme", name="widget", html_url="https://github.com/acme/widget"
        )

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.list_open_pull_requests(repository)

        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}
        assert [sent(request) for request in seen] == [
            (
                "GET",
                "/repos/acme/widget/pulls",
                {"state": "open", "per_page": "100", "sort": "updated", "direction": "desc"},
            )
        ]

    async def test_a_label_write_posts_the_one_label_as_json(self) -> None:
        handler, seen = answering({})

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.add_label("acme", "widget", 7, "needs triage")

        assert [sent(request) for request in seen] == [
            ("POST", "/repos/acme/widget/issues/7/labels", {})
        ]
        assert json.loads(seen[0].content) == {"labels": ["needs triage"]}
        assert headers_of(seen[0]) == {
            **EVERY_REQUEST,
            "authorization": "Bearer ghs_acme",
            "content-type": "application/json",
        }

    async def test_a_label_removal_deletes_it_by_its_escaped_name(self) -> None:
        handler, seen = answering({})

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.remove_label("acme", "widget", 7, "needs triage")

        assert seen[0].method == "DELETE"
        assert seen[0].url.raw_path == b"/repos/acme/widget/issues/7/labels/needs%20triage"
        assert seen[0].content == b""
        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}

    async def test_a_files_history_is_one_page_from_one_commit(self) -> None:
        handler, seen = answering([], link_to="https://api.github.com/next?page=2")
        sha = "d" * 40

        async with the_real_client(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.list_commits_on_path("acme", "widget", sha=sha, file_path="a/b.py")

        assert [sent(request) for request in seen] == [
            ("GET", "/repos/acme/widget/commits", {"sha": sha, "path": "a/b.py", "per_page": "100"})
        ]
        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}


class TestAConditionalPagedRead:
    """`get_pages_since`, the board poller's read. The validator goes on the first request only,
    and an explicit token beats the owner's installation on every page."""

    async def test_the_validator_goes_on_the_first_page_and_the_query_only_there(self) -> None:
        following = "https://api.github.com/users/acme/projectsV2/1/items?after=abc"
        handler, seen = answering([1], [2], link_to=following)

        async with the_real_transport(handler, tokens=FakeTokens(acme="ghs_acme")) as transport:
            read = await transport.get_pages_since(
                "/users/acme/projectsV2/1/items", etag='W/"one"', owner="acme", per_page=50
            )

        assert read == PagedRead(etag=None, pages=([1], [2]))
        assert [sent(request) for request in seen] == [
            ("GET", "/users/acme/projectsV2/1/items", {"per_page": "50"}),
            ("GET", "/users/acme/projectsV2/1/items", {"after": "abc"}),
        ]
        assert headers_of(seen[0]) == {
            **EVERY_REQUEST,
            "authorization": "Bearer ghs_acme",
            "if-none-match": 'W/"one"',
        }
        assert headers_of(seen[1]) == {**EVERY_REQUEST, "authorization": "Bearer ghs_acme"}

    async def test_an_explicit_token_is_the_credential_on_it(self) -> None:
        handler, seen = answering([1])
        tokens = FakeTokens(acme="ghs_acme")

        async with the_real_transport(handler, tokens=tokens) as transport:
            await transport.get_pages_since("/items", owner="acme", token="gho_person")

        assert headers_of(seen[0]) == {**EVERY_REQUEST, "authorization": "Bearer gho_person"}
        assert tokens.asked == []

    async def test_one_page_keeps_its_validator(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps([1]), headers={"ETag": 'W/"two"'})

        async with the_real_transport(handler) as transport:
            read = await transport.get_pages_since("/items")

        assert read == PagedRead(etag='W/"two"', pages=([1],))

    async def test_not_modified_is_no_pages_and_the_validator_sent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(304)

        async with the_real_transport(handler) as transport:
            read = await transport.get_pages_since("/items", etag='W/"one"')

        assert read == PagedRead(etag='W/"one"', pages=None)


# Every entry point the classification below is pinned through, with the path its message names.
# Each one reached GitHub by a different spelling of the transport before #271, which is the
# reason for crossing them with every answer rather than trusting one to speak for the rest.
Call = Callable[[GitHubTransport], Awaitable[object]]


async def _drain(transport: GitHubTransport) -> object:
    return [page async for page in transport.get_pages("/repos/owner/repo/labels")]


ENTRY_POINTS: list[tuple[str, Call, str]] = [
    (
        "get_repository",
        lambda t: HttpGitHubClient(transport=t).get_repository("owner", "repo"),
        "/repos/owner/repo",
    ),
    ("get_json", lambda t: t.get_json("/repos/owner/repo"), "/repos/owner/repo"),
    ("get_pages", _drain, "/repos/owner/repo/labels"),
    ("get_pages_since", lambda t: t.get_pages_since("/projects/1"), "/projects/1"),
    (
        "add_label",
        lambda t: HttpGitHubClient(transport=t).add_label("owner", "repo", 7, "bug"),
        "/repos/owner/repo/issues/7/labels",
    ),
]

# Without a 404, which `list_commits_on_path` answers with None rather than raising.
HISTORY: tuple[str, Call, str] = (
    "list_commits_on_path",
    lambda t: HttpGitHubClient(transport=t).list_commits_on_path(
        "owner", "repo", sha="a" * 40, file_path="x.py"
    ),
    "/repos/owner/repo/commits",
)

every_entry_point = pytest.mark.parametrize(
    ("call", "path"),
    [pytest.param(call, path, id=name) for name, call, path in [*ENTRY_POINTS, HISTORY]],
)
every_entry_point_that_raises_a_404 = pytest.mark.parametrize(
    ("call", "path"), [pytest.param(call, path, id=name) for name, call, path in ENTRY_POINTS]
)


def responding(status: int, body: object, headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=json.dumps(body), headers=headers)

    return handler


async def raised_by(call: Call, handler: Handler) -> GitHubError:
    async with the_real_transport(handler) as transport:
        with pytest.raises(GitHubError) as caught:
            await call(transport)
    return caught.value


SERVED = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class TestHowAnAnswerIsFiled:
    """Each status as the class it is filed under, exactly, and the words it carries."""

    @every_entry_point
    async def test_a_spent_budget_is_a_rate_limit_with_the_wait_to_the_reset(
        self, call: Call, path: str
    ) -> None:
        headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(SERVED.timestamp()) + 90),
            "date": format_datetime(SERVED, usegmt=True),
        }

        error = await raised_by(call, responding(403, {"message": "API rate limit"}, headers))

        assert type(error) is GitHubRateLimitError
        assert str(error) == "GitHub rate limit reached"
        assert error.retry_after == 90

    @every_entry_point
    async def test_a_forbidden_that_asks_for_a_wait_is_a_rate_limit_with_that_wait(
        self, call: Call, path: str
    ) -> None:
        handler = responding(403, {"message": "Forbidden"}, {"retry-after": "30"})

        error = await raised_by(call, handler)

        assert type(error) is GitHubRateLimitError
        assert str(error) == "GitHub rate limit reached"
        assert error.retry_after == 30

    @every_entry_point
    async def test_too_many_requests_is_a_rate_limit(self, call: Call, path: str) -> None:
        handler = responding(429, {"message": "Too Many Requests"}, {"retry-after": "12"})

        error = await raised_by(call, handler)

        assert type(error) is GitHubRateLimitError
        assert str(error) == "GitHub rate limit reached"
        assert error.retry_after == 12

    @every_entry_point
    async def test_a_plain_forbidden_is_a_refused_credential(self, call: Call, path: str) -> None:
        error = await raised_by(call, responding(403, {"message": "Forbidden"}))

        assert type(error) is GitHubAuthError
        assert str(error) == f"GitHub refused the request for {path} (403)"

    @every_entry_point
    async def test_unauthorised_is_a_refused_credential(self, call: Call, path: str) -> None:
        error = await raised_by(call, responding(401, {"message": "Bad credentials"}))

        assert type(error) is GitHubAuthError
        assert str(error) == f"GitHub refused the request for {path} (401)"

    @every_entry_point_that_raises_a_404
    async def test_not_found_is_nothing_there(self, call: Call, path: str) -> None:
        error = await raised_by(call, responding(404, {"message": "Not Found"}))

        assert type(error) is GitHubNotFoundError
        assert str(error) == f"GitHub has nothing at {path}"

    @every_entry_point
    async def test_unprocessable_is_a_refusal_in_githubs_own_words(
        self, call: Call, path: str
    ) -> None:
        error = await raised_by(call, responding(422, {"message": "Validation Failed"}))

        assert type(error) is GitHubRefusedError
        assert str(error) == "Validation Failed"

    @every_entry_point
    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_a_server_error_is_github_unavailable(
        self, call: Call, path: str, status: int
    ) -> None:
        error = await raised_by(call, responding(status, {"message": "Server Error"}))

        assert type(error) is GitHubUnavailableError
        assert str(error) == f"GitHub returned {status} for {path}"

    @every_entry_point
    async def test_a_request_that_never_arrives_is_github_unreachable(
        self, call: Call, path: str
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        error = await raised_by(call, handler)

        assert type(error) is GitHubUnavailableError
        assert str(error) == "Could not reach GitHub: connection refused"
        assert isinstance(error.__cause__, httpx.ConnectError)

    @pytest.mark.parametrize(
        ("call", "path"),
        [pytest.param(call, path, id=name) for name, call, path in [*ENTRY_POINTS[:4], HISTORY]],
    )
    async def test_a_body_that_is_not_json_is_github_unavailable(
        self, call: Call, path: str
    ) -> None:
        """Not for a write, whose answer is only ever whether it worked."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>maintenance</html>")

        error = await raised_by(call, handler)

        assert type(error) is GitHubUnavailableError
        assert str(error) == f"GitHub returned a non-JSON body for {path}"
