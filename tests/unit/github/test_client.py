from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from shannon.domain.models import RepositorySnapshot
from shannon.github.client import MAX_PAGES, HttpGitHubClient
from shannon.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)
from tests.support import github_payloads as payloads


def client_with(
    handler: Callable[[httpx.Request], httpx.Response], *, tokens: object | None = None
) -> HttpGitHubClient:
    """A client wired to a handler, otherwise built the way the real one is.

    `follow_redirects` is the way the real one is built, and leaving it off here made a whole
    class of defect invisible: a redirected write is downgraded to a read by the transport, and
    with the transport told not to follow anything, no test could see it happen.
    """
    transport = httpx.MockTransport(handler)
    return HttpGitHubClient(
        tokens=tokens,
        http_client=httpx.AsyncClient(
            transport=transport, base_url="https://api.github.com", follow_redirects=True
        ),
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


async def test_a_secondary_limit_answered_as_a_forbidden_is_still_a_rate_limit() -> None:
    """GitHub has two limits and they answer differently.

    The primary one is the hourly budget and says so in the counter. The secondary one is about
    how fast requests arrive, does not spend the budget, and marks itself only by asking for a
    wait: the counter beside it is untouched and often nowhere near zero. It is also the one this
    bot can actually reach, because a write costs several times what a read does against it and
    the poller writes.

    Read on the counter alone it was a refusal, the wait GitHub asked for was thrown away, the
    poller's one backoff could not fire, and it carried on at its ordinary interval, which is how
    GitHub's own documentation says an integration gets banned.
    """
    handler = responds(
        403,
        {"message": "You have exceeded a secondary rate limit"},
        {"retry-after": "60", "x-ratelimit-remaining": "4987"},
    )
    async with client_with(handler) as client:
        with pytest.raises(GitHubRateLimitError) as caught:
            await client.get_repository("owner", "repo")

    assert caught.value.retry_after == 60, "the wait GitHub asked for was thrown away"


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


async def test_unusable_pull_request_body_raises_unavailable() -> None:
    """The repository half read fine, so the failure is the pull request itself."""
    body = {"base": {"repo": payloads.repository()}, "number": None}

    async with client_with(responds(200, body)) as client:
        with pytest.raises(GitHubUnavailableError, match="unusable pull request"):
            await client.get_pull_request(payloads.OWNER, payloads.REPO, 7)


async def test_a_json_body_that_is_not_an_object_raises_unavailable() -> None:
    """Valid JSON, wrong shape. A list gets past `response.json()` and past nothing after it."""
    async with client_with(responds(200, ["not", "an", "object"])) as client:
        with pytest.raises(GitHubUnavailableError, match="unexpected body"):
            await client.get_repository("owner", "repo")


async def test_the_client_it_builds_itself_is_the_one_it_closes() -> None:
    """Every other test here injects a client, which this deliberately does not own."""
    client = HttpGitHubClient()

    await client.aclose()

    assert client._client.is_closed is True


class TestWritingLabels:
    """The only writes this bot makes to GitHub, and the record the workflow rests on."""

    def _recording(self, status: int = 200):
        seen: list[tuple[str, str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            return httpx.Response(status, content=json.dumps({}))

        return seen, handler

    async def test_adding_a_label_posts_it_to_the_issues_endpoint(self) -> None:
        """Pull requests are served from the issues endpoint too, so one path covers both."""
        seen, handler = self._recording()

        async with client_with(handler) as client:
            await client.add_label("acme", "widget", 7, "IN_REVIEW")

        assert seen == [("POST", "/repos/acme/widget/issues/7/labels", {"labels": ["IN_REVIEW"]})]

    async def test_adding_a_comment_posts_it_to_the_issues_endpoint(self) -> None:
        """The same endpoint as the labels above, and for the same reason: GitHub serves a pull
        request's comments from the issues path. Issue #103."""
        seen, handler = self._recording()

        async with client_with(handler) as client:
            await client.add_comment("acme", "widget", 7, "a transcript")

        assert seen == [("POST", "/repos/acme/widget/issues/7/comments", {"body": "a transcript"})]

    async def test_a_comment_escapes_the_repository_name(self) -> None:
        """Every write goes through `_repository`, which quotes both halves. A stray slash in a
        name would otherwise read as another path segment and post the comment elsewhere.

        Asserted against the raw path rather than `url.path`, which is the decoded form and shows
        the escape undone whether or not it was ever applied. Reading that one made this test pass
        for a moment while proving nothing.
        """
        wire: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps({}))

        async with client_with(handler) as client:
            await client.add_comment("acme", "widget/evil", 7, "hello")

        assert wire == [b"/repos/acme/widget%2Fevil/issues/7/comments"]

    async def test_a_refused_comment_is_reported_rather_than_swallowed(self) -> None:
        """What the flusher counts as a failure and eventually gives up a batch over."""
        async with client_with(responds(403, {"message": "Forbidden"})) as client:
            with pytest.raises(GitHubAuthError):
                await client.add_comment("acme", "widget", 7, "hello")

    async def test_removing_a_label_names_it_in_the_path(self) -> None:
        seen, handler = self._recording()

        async with client_with(handler) as client:
            await client.remove_label("acme", "widget", 7, "IN_REVIEW")

        assert seen == [("DELETE", "/repos/acme/widget/issues/7/labels/IN_REVIEW", None)]

    async def test_a_label_with_a_space_in_it_is_encoded(self) -> None:
        """`priority: high` is a real label style, and unencoded it changes which path is hit.

        Read off `raw_path`, which is what goes on the wire. `url.path` hands back the decoded
        form, so asserting on that would pass whether or not anything was encoded at all.
        """
        wire: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path.decode())
            return httpx.Response(200, content="{}")

        async with client_with(handler) as client:
            await client.remove_label("acme", "widget", 7, "priority: high")

        assert wire == ["/repos/acme/widget/issues/7/labels/priority%3A%20high"]

    async def test_removing_a_label_that_is_not_there_is_not_a_failure(self) -> None:
        """Removals are worked out from a snapshot read a moment earlier. A label somebody took
        off in between leaves the item where the caller wanted it, so failing would have them
        retrying towards a state they are already in."""
        async with client_with(responds(404, {"message": "Label does not exist"})) as client:
            await client.remove_label("acme", "widget", 7, "gone")

    async def test_a_refused_write_is_reported(self) -> None:
        async with client_with(responds(403, {"message": "Forbidden"})) as client:
            with pytest.raises(GitHubAuthError):
                await client.add_label("acme", "widget", 7, "DONE")

    async def test_a_write_that_never_reaches_github_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="Could not reach GitHub"):
                await client.add_label("acme", "widget", 7, "DONE")


class TestReadingTheChecksOnACommit:
    """Issue #112. The wrapper is the trap: this endpoint answers an OBJECT with the list under
    `check_runs`, unlike the labels list beside it, and a reader copied from that one pages
    happily and finds nothing at all."""

    async def test_it_reads_the_list_out_of_the_wrapper(self) -> None:
        body = payloads.check_runs_page(
            payloads.check_run(), payloads.check_run(id=2, name="CI", conclusion="failure")
        )

        async with client_with(responds(200, body)) as client:
            found = await client.list_check_runs("acme", "widget", "c" * 40)

        assert found is not None
        assert [run.name for run in found] == ["Lint, format and types", "CI"]
        assert [run.conclusion for run in found] == ["success", "failure"]

    async def test_it_asks_for_the_latest_attempt_only(self) -> None:
        """What makes a re-run replace its predecessor rather than sit beside it. Without it every
        attempt is listed at once and the counts are nonsense."""
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, content=json.dumps(payloads.check_runs_page()))

        async with client_with(handler) as client:
            await client.list_check_runs("acme", "widget", "c" * 40)

        assert seen[0]["filter"] == "latest"

    async def test_it_escapes_the_sha_and_the_repository(self) -> None:
        wire: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps(payloads.check_runs_page()))

        async with client_with(handler) as client:
            await client.list_check_runs("acme", "widget/evil", "a/b")

        assert wire[0].startswith(b"/repos/acme/widget%2Fevil/commits/a%2Fb/check-runs")

    async def test_a_commit_github_has_collected_answers_none(self) -> None:
        """None rather than raising, for the reason `ReadsCommits` gives: a SHA that has gone
        never comes back, so sixteen attempts over two hours reach the same answer."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            assert await client.list_check_runs("acme", "widget", "c" * 40) is None

    async def test_an_empty_suite_is_an_empty_list_rather_than_none(self) -> None:
        """A different answer from the one above. Nothing ran is not the same as nothing there."""
        async with client_with(responds(200, payloads.check_runs_page())) as client:
            assert await client.list_check_runs("acme", "widget", "c" * 40) == []


class TestPagingThroughAList:
    """The project endpoints paginate by a cursor in the Link header and have no page number.

    Asking for page two by number is not an error, it is silently the first page again, so a
    client that counted pages would read a long board over and over and mirror every card on it
    as many times as it looped.
    """

    def _pages(self, *bodies: object):
        seen: list[dict[str, str]] = []
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            index = calls["n"]
            calls["n"] += 1
            headers = (
                {"Link": f'<https://api.github.com/next?after=cursor{index}>; rel="next"'}
                if index < len(bodies) - 1
                else {}
            )
            return httpx.Response(200, content=json.dumps(bodies[index]), headers=headers)

        return seen, handler

    async def test_it_follows_the_link_header_to_the_end(self) -> None:
        _, handler = self._pages([{"id": 1}], [{"id": 2}], [{"id": 3}])

        async with client_with(handler) as client:
            pages = [page async for page in client.get_pages("/items", per_page=100)]

        assert pages == [[{"id": 1}], [{"id": 2}], [{"id": 3}]]

    async def test_the_original_parameters_are_not_repeated_after_the_first_page(self) -> None:
        """The next URL already carries the cursor. Sending the first request's parameters
        beside it is how a caller ends up asking for the same page again."""
        seen, handler = self._pages([{"id": 1}], [{"id": 2}])

        async with client_with(handler) as client:
            [page async for page in client.get_pages("/items", per_page=100)]

        assert seen[0] == {"per_page": "100"}
        assert "per_page" not in seen[1]

    async def test_one_page_is_one_request(self) -> None:
        seen, handler = self._pages([{"id": 1}])

        async with client_with(handler) as client:
            pages = [page async for page in client.get_pages("/items")]

        assert len(pages) == 1
        assert len(seen) == 1

    async def test_a_page_that_will_not_parse_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>maintenance</html>")

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="non-JSON"):
                [page async for page in client.get_pages("/items")]

    async def test_a_refused_page_is_reported(self) -> None:
        async with client_with(responds(403, {"message": "Forbidden"})) as client:
            with pytest.raises(GitHubAuthError):
                [page async for page in client.get_pages("/items")]

    async def test_a_page_that_never_arrives_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="Could not reach GitHub"):
                [page async for page in client.get_pages("/items")]

    async def test_a_cursor_that_points_at_itself_does_not_read_for_ever(self) -> None:
        """The cursor is opaque, so there is nothing to inspect that would tell a real next page
        from a Link header looping back. Bounded rather than trusted."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                200,
                content="[]",
                headers={"Link": '<https://api.github.com/items?after=same>; rel="next"'},
            )

        async with client_with(handler) as client:
            pages = [page async for page in client.get_pages("/items")]

        assert len(pages) == MAX_PAGES
        assert len(seen) == MAX_PAGES

    def _of_length(self, pages: int):
        """A list that really ends, in as many pages as asked for."""

        def handler(request: httpx.Request) -> httpx.Response:
            page = int(request.url.params.get("page", 1))
            links = (
                {"Link": f'<https://api.github.com/items?page={page + 1}>; rel="next"'}
                if page < pages
                else {}
            )
            return httpx.Response(200, content=json.dumps([page]), headers=links)

        return handler

    async def test_a_list_of_exactly_the_limit_is_not_reported_as_cut_short(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning used to hang off the loop's `else`, which runs whenever the range is
        exhausted, so a list that ended on the last page it was allowed was read whole and
        reported as truncated.

        A warning that fires when nothing is wrong is worse than no warning. It teaches whoever
        reads the log to skip the line, and the one time it means a board is being cut off looks
        exactly like the times it does not.
        """
        async with client_with(self._of_length(MAX_PAGES)) as client:
            with caplog.at_level("WARNING"):
                pages = [page async for page in client.get_pages("/items", page=1)]

        assert len(pages) == MAX_PAGES, "it did not read the whole list"
        assert "stopped following" not in caplog.text

    async def test_a_list_one_page_longer_is_reported_as_cut_short(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with client_with(self._of_length(MAX_PAGES + 1)) as client:
            with caplog.at_level("WARNING"):
                pages = [page async for page in client.get_pages("/items", page=1)]

        assert len(pages) == MAX_PAGES
        assert "stopped following" in caplog.text

    async def test_a_list_shorter_than_the_limit_says_nothing_either(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with client_with(self._of_length(3)) as client:
            with caplog.at_level("WARNING"):
                pages = [page async for page in client.get_pages("/items", page=1)]

        assert len(pages) == 3
        assert caplog.text == ""


class TestAskingWhetherAListChanged:
    """`get_pages_since` is the conditional read that makes a two-second board poll affordable.

    A 304 to `If-None-Match` costs no rate-limit budget at all and carries no body, so the whole
    value of this method sits in the answers that bring nothing back. Every test here is about one
    of two things: that the question gets asked, and that "nothing changed" is never confused with
    "there is nothing there".
    """

    def _answers(
        self, status: int, body: object = None, headers: dict[str, str] | None = None
    ) -> tuple[list[httpx.Request], Callable[[httpx.Request], httpx.Response]]:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if status == 304:
                # No body and no Location, which is what GitHub actually sends.
                return httpx.Response(304, headers=headers or {})
            return httpx.Response(status, content=json.dumps(body), headers=headers or {})

        return seen, handler

    async def test_no_validator_is_sent_when_none_is_held(self) -> None:
        seen, handler = self._answers(200, [{"id": 1}], {"ETag": '"abc"'})

        async with client_with(handler) as client:
            await client.get_pages_since("/items", per_page=100)

        assert "If-None-Match" not in seen[0].headers

    async def test_a_held_validator_is_sent(self) -> None:
        """The saving does not happen unless the question is asked."""
        seen, handler = self._answers(200, [{"id": 1}], {"ETag": '"new"'})

        async with client_with(handler) as client:
            await client.get_pages_since("/items", etag='"held"', per_page=100)

        assert seen[0].headers["If-None-Match"] == '"held"'

    async def test_a_304_says_nothing_changed_rather_than_raising(self) -> None:
        """The ordering this pins is load-bearing. `_raise_for_status` passes a 2xx and nothing
        else, so a 304 reaching it answers `GitHubUnavailableError("GitHub returned 304")` - an
        outage, once per poll, for the one reply that means everything is fine.
        """
        _, handler = self._answers(304)

        async with client_with(handler) as client:
            read = await client.get_pages_since("/items", etag='"held"')

        assert read.pages is None
        assert read.etag == '"held"', "the validator it answered to is still the one to send"

    async def test_a_304_is_not_mistaken_for_a_redirect(self) -> None:
        """A 304 sits inside httpx's redirect range and the real client follows redirects. It is
        spared only because it carries no Location, so this runs against a client built the way the
        real one is - a transport told to follow nothing is where that would stay invisible.
        """
        seen, handler = self._answers(304)

        async with client_with(handler) as client:
            read = await client.get_pages_since("/items", etag='"held"')

        assert read.pages is None
        assert len(seen) == 1, "it followed the 304 somewhere"

    async def test_an_unchanged_list_is_not_an_empty_one(self) -> None:
        """What the whole return type exists for. A board that answered 304 has every card it had;
        a board that answered `[]` has none. One spelling for both would either wipe a board or
        hide one, and which of those you got would depend on the caller.
        """
        _, unchanged = self._answers(304)
        _, empty = self._answers(200, [], {"ETag": '"e"'})

        async with client_with(unchanged) as client:
            nothing_changed = await client.get_pages_since("/items", etag='"held"')
        async with client_with(empty) as client:
            nothing_there = await client.get_pages_since("/items", etag='"held"')

        assert nothing_changed.pages is None
        assert nothing_there.pages == ([],)

    async def test_a_changed_list_carries_its_new_validator(self) -> None:
        _, handler = self._answers(200, [{"id": 1}], {"ETag": '"fresh"'})

        async with client_with(handler) as client:
            read = await client.get_pages_since("/items", etag='"stale"')

        assert read.pages == ([{"id": 1}],)
        assert read.etag == '"fresh"'

    async def test_a_list_with_no_validator_offers_none(self) -> None:
        """Nothing to ask with next time, which the board reader reads as do not keep this."""
        _, handler = self._answers(200, [{"id": 1}])

        async with client_with(handler) as client:
            read = await client.get_pages_since("/items")

        assert read.etag is None

    async def test_a_paged_list_offers_no_validator_and_is_read_whole(self) -> None:
        """The safety rule, at the client end. GitHub's ETag hashes ONE response body, so page
        one's tag says nothing about page two - and a 304 carries no Link header to ask with. A tag
        kept here would let a change confined to a later page go unseen for ever.
        """
        pages = [[{"id": 1}], [{"id": 2}], [{"id": 3}]]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            index = calls["n"]
            calls["n"] += 1
            headers = {"ETag": f'"page{index}"'}
            if index < len(pages) - 1:
                headers["Link"] = f'<https://api.github.com/items?after=c{index}>; rel="next"'
            return httpx.Response(200, content=json.dumps(pages[index]), headers=headers)

        async with client_with(handler) as client:
            read = await client.get_pages_since("/items", per_page=100)

        assert read.pages == tuple(pages), "it did not read the whole list"
        assert read.etag is None, "it kept a validator covering one page of three"

    async def test_the_original_parameters_are_not_repeated_after_the_first_page(self) -> None:
        """The cursor is in the next URL already. Sending the first request's parameters beside it
        is how a caller ends up asking for the same page twice.
        """
        seen: list[httpx.Request] = []
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            index = calls["n"]
            calls["n"] += 1
            links = (
                {"Link": '<https://api.github.com/items?after=c>; rel="next"'} if index == 0 else {}
            )
            return httpx.Response(200, content=json.dumps([index]), headers=links)

        async with client_with(handler) as client:
            await client.get_pages_since("/items", per_page=100)

        assert seen[0].url.params.get("per_page") == "100"
        assert "per_page" not in seen[1].url.params

    async def test_a_body_that_will_not_parse_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>maintenance</html>")

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="non-JSON"):
                await client.get_pages_since("/items")

    async def test_a_later_page_that_will_not_parse_is_reported(self) -> None:
        """The second read has its own decode, so it needs its own test: a first page that parsed
        proves nothing about the one the cursor leads to.
        """
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200,
                    content=json.dumps([{"id": 1}]),
                    headers={"Link": '<https://api.github.com/items?after=c>; rel="next"'},
                )
            return httpx.Response(200, content=b"<html>maintenance</html>")

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="non-JSON"):
                await client.get_pages_since("/items")

    async def test_a_refusal_is_reported(self) -> None:
        async with client_with(responds(403, {"message": "Forbidden"})) as client:
            with pytest.raises(GitHubAuthError):
                await client.get_pages_since("/items")

    async def test_a_refusal_on_a_later_page_is_reported(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200,
                    content=json.dumps([{"id": 1}]),
                    headers={"Link": '<https://api.github.com/items?after=c>; rel="next"'},
                )
            return httpx.Response(403, content=json.dumps({"message": "Forbidden"}))

        async with client_with(handler) as client:
            with pytest.raises(GitHubAuthError):
                await client.get_pages_since("/items")

    async def test_a_list_that_never_arrives_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="Could not reach GitHub"):
                await client.get_pages_since("/items")

    async def test_a_cursor_that_points_at_itself_does_not_read_for_ever(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The same bound `get_pages` keeps, for the same reason: the cursor is opaque, so a Link
        header looping back cannot be told from a real next page by inspection.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content="[]",
                headers={"Link": '<https://api.github.com/items?after=same>; rel="next"'},
            )

        async with client_with(handler) as client:
            with caplog.at_level("WARNING"):
                read = await client.get_pages_since("/items")

        assert read.pages is not None
        assert len(read.pages) == MAX_PAGES
        assert "stopped following pages" in caplog.text

    async def test_a_list_that_ends_on_its_last_allowed_page_is_not_cut_short(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning that fires when nothing is wrong teaches whoever reads the log to skip it."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            links = (
                {"Link": '<https://api.github.com/items?after=c>; rel="next"'}
                if calls["n"] < MAX_PAGES
                else {}
            )
            return httpx.Response(200, content=json.dumps([calls["n"]]), headers=links)

        async with client_with(handler) as client:
            with caplog.at_level("WARNING"):
                read = await client.get_pages_since("/items")

        assert read.pages is not None
        assert len(read.pages) == MAX_PAGES, "it did not read the whole list"
        assert caplog.text == ""


class TestFetchingAnyJson:
    """`get_json` hands the body over as it came, for endpoints that answer with arrays."""

    async def test_a_list_body_comes_back_as_a_list(self) -> None:
        async with client_with(responds(200, [{"id": 1}, {"id": 2}])) as client:
            assert await client.get_json("/fields") == [{"id": 1}, {"id": 2}]

    async def test_parameters_are_sent(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, content="[]")

        async with client_with(handler) as client:
            await client.get_json("/items", per_page=100, fields="1,2")

        assert seen == [{"per_page": "100", "fields": "1,2"}]

    async def test_a_non_json_body_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>nope</html>")

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="non-JSON"):
                await client.get_json("/fields")

    async def test_a_refusal_is_reported(self) -> None:
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            with pytest.raises(GitHubNotFoundError):
                await client.get_json("/fields")

    async def test_a_body_that_never_arrives_is_reported(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="Could not reach GitHub"):
                await client.get_json("/fields")


class FakeTokens:
    """A token per account, which is what an installation supplies."""

    def __init__(self, **owners: str) -> None:
        self.owners = owners
        self.asked: list[str] = []

    async def token_for(self, owner: str) -> str:
        self.asked.append(owner)
        return self.owners.get(owner, "")


class TestWhichCredentialACallCarries:
    """The change issue #98 turns on. There is no longer one token: each call carries one minted
    for the account it is about, so a server can only ever read what it was granted."""

    def test_the_static_headers_carry_no_credential(self) -> None:
        """`Authorization` used to be baked in here, because there was one token for everything.
        Leaving it would mean one credential on every request again."""
        from shannon.github.client import _headers

        assert "Authorization" not in _headers()

    async def test_a_read_is_authorised_as_the_account_it_is_about(self) -> None:
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps(payloads.repository()))

        async with client_with(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.get_repository("acme", "widget")

        assert seen == ["Bearer ghs_acme"]

    async def test_an_explicit_credential_is_the_one_that_goes_out(self) -> None:
        """Issue #170, and the one line that puts a person's authorisation on a real request.

        A project board is read and written under the authorisation of a particular PERSON, and the
        owner cannot identify one: two servers may link two different boards owned by the same
        account, so a credential chosen by owner alone would be one server's board read under the
        other server's member's grant. The board reader resolves whose it is and passes it here.
        """
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.get_json("/items", owner="acme", token="gho_a_person")

        assert seen == ["Bearer gho_a_person"]

    async def test_an_explicit_credential_beats_the_supplier(self) -> None:
        """Which is what makes it usable at all: the client it goes through may hold an
        installation token for that same account, and a board cannot be read with one."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler, tokens=FakeTokens(acme="ghs_installation")) as client:
            await client.get_json("/items", owner="acme", token="gho_a_person")

        assert seen == ["Bearer gho_a_person"], "the installation token won, so a board would 403"

    async def test_an_explicit_credential_goes_on_every_page(self) -> None:
        """Issue #201. Listing somebody's boards is a board read like any other, and a paged read
        had no way to carry a person's credential at all - so the picker's list went out as the App
        installation, which holds no Projects permission and cannot see a private board.

        Every page rather than the first, because the cursor's later pages are requests in their
        own right, and the supplier is never asked: an installation token on page two would be the
        same defect one request later.
        """
        seen: list[str | None] = []
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            calls["n"] += 1
            more = (
                {"Link": '<https://api.github.com/next?after=cursor>; rel="next"'}
                if calls["n"] == 1
                else {}
            )
            return httpx.Response(200, content=json.dumps([]), headers=more)

        tokens = FakeTokens(acme="ghs_installation")
        async with client_with(handler, tokens=tokens) as client:
            [page async for page in client.get_pages("/items", owner="acme", token="gho_chooser")]

        assert seen == ["Bearer gho_chooser", "Bearer gho_chooser"]
        assert tokens.asked == [], "the installation was asked, so a page could go out as the App"

    async def test_no_explicit_credential_still_asks_the_supplier(self) -> None:
        """The other arm, and every call in the project that is not about a board."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler, tokens=FakeTokens(acme="ghs_installation")) as client:
            await client.get_json("/items", owner="acme")

        assert seen == ["Bearer ghs_installation"]

    async def test_a_write_carries_an_explicit_credential_too(self) -> None:
        """The half that matters most: GitHub records whoever this names as having moved
        the card."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps({}))

        async with client_with(handler) as client:
            await client.patch_json(
                "/items/1", owner="acme", token="gho_the_mover", json={"fields": []}
            )

        assert seen == ["Bearer gho_the_mover"]

    async def test_two_accounts_are_authorised_differently(self) -> None:
        """One token for both would be the shared credential this replaced."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps(payloads.repository()))

        tokens = FakeTokens(acme="ghs_acme", other="ghs_other")
        async with client_with(handler, tokens=tokens) as client:
            await client.get_repository("acme", "widget")
            await client.get_repository("other", "thing")

        assert seen == ["Bearer ghs_acme", "Bearer ghs_other"]

    async def test_a_write_carries_one_too(self) -> None:
        """Labels are the only thing this bot writes to GitHub, and a write with no credential is
        a write that silently does nothing on a private repository."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler, tokens=FakeTokens(acme="ghs_acme")) as client:
            await client.add_label("acme", "widget", 7, "bug")

        assert seen == ["Bearer ghs_acme"]

    async def test_an_account_with_no_installation_sends_no_header_at_all(self) -> None:
        """No header rather than an empty bearer. `Bearer ` is a malformed credential and GitHub
        answers 401 to it; no header is an anonymous request that public endpoints answer, which
        is what a deployment with no App configured wants."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps(payloads.repository()))

        async with client_with(handler, tokens=FakeTokens()) as client:
            await client.get_repository("stranger", "widget")

        assert seen == [None]

    async def test_a_client_with_no_token_source_asks_for_none(self) -> None:
        """Exactly what an empty `SHANNON_GITHUB_TOKEN` used to produce, which is why a
        deployment that has not set the App up still answers about public repositories."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Authorization"))
            return httpx.Response(200, content=json.dumps(payloads.repository()))

        async with client_with(handler) as client:
            await client.get_repository("acme", "widget")

        assert seen == [None]

    async def test_the_user_lookup_is_anonymous(self) -> None:
        """`/link` asks about a login rather than about a repository, so there is no account to
        authorise as. The endpoint is public, which is why it works at all."""
        tokens = FakeTokens(acme="ghs_acme")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"id": 7}))

        async with client_with(handler, tokens=tokens) as client:
            await client.user_id("octocat")

        assert tokens.asked == [], "it tried to mint a token for a public endpoint"


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

    @pytest.mark.parametrize("date", [None, "the fourteenth of never"])
    async def test_without_a_usable_date_the_wait_is_measured_on_our_clock(
        self, date: str | None
    ) -> None:
        """GitHub's `date` is the clock the reset moment was measured on, when it sends one.

        A proxy that strips it, or sends something unparseable, leaves the local clock as the
        only one there is. Close enough is the most that can be claimed: the two clocks are not
        the same clock, which is the whole reason the header is preferred.
        """
        headers = {
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": str(int(time.time()) + 90),
        }
        if date is not None:
            headers["date"] = date

        async with client_with(responds(403, {"message": "rate limited"}, headers)) as client:
            with pytest.raises(GitHubRateLimitError) as caught:
                await client.get_repository("owner", "repo")

        assert caught.value.retry_after is not None
        assert 85 <= caught.value.retry_after <= 90

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


class TestAskingWhoHoldsALogin:
    """What `/link` checks before it records a login.

    The one thing this must not do is say yes when it does not know. A login nobody holds is
    recorded happily and then names that person in plain text for ever, which is exactly what
    somebody who never linked looks like, so nothing in the thread, the block or the log can
    tell the two apart.
    """

    async def test_an_account_that_is_there_answers_with_its_id(self) -> None:
        """The id rather than a yes: a login is not an identity, and what is stored beside the
        name is what a mention built later is checked against."""
        async with client_with(responds(200, {"login": "monalisa", "id": 583231})) as client:
            assert await client.user_id("monalisa") == 583231

    async def test_an_account_that_is_not_there_is_nobody(self) -> None:
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            assert await client.user_id("nobody-at-all") is None

    async def test_an_answer_with_no_id_in_it_is_nobody(self) -> None:
        """GitHub always sends one. Reading a body that does not carry it as an account would
        store a null and quietly fall back to matching on the name for ever."""
        async with client_with(responds(200, {"login": "monalisa"})) as client:
            assert await client.user_id("monalisa") is None

    async def test_it_asks_the_public_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps({"login": "x"}))

        async with client_with(handler) as client:
            await client.user_id("mona-lisa")

        assert seen == ["/users/mona-lisa"]

    async def test_a_login_with_a_slash_in_it_cannot_reach_another_endpoint(self) -> None:
        """The pattern upstream rules this out, and a path built by hand should not rely on it.

        Read as `raw_path`, which is what goes on the wire. `path` gives it back decoded, so a
        test written against that would pass whether or not anything was escaped.
        """
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        async with client_with(handler) as client:
            await client.user_id("../repos/acme/widgets")

        assert seen == [b"/users/..%2Frepos%2Facme%2Fwidgets"]

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_anything_else_github_says_is_raised_rather_than_answered(
        self, status: int
    ) -> None:
        """A question that could not be put is not an answer of no. Refusing sends the person
        back in a minute; answering no records nothing and tells them their login is wrong."""
        async with client_with(responds(status, {"message": "nope"})) as client:
            with pytest.raises(GitHubError):
                await client.user_id("monalisa")


class TestAskingWhichLoginAnAccountAnswersTo:
    """The other direction, for a stored login that may have moved since. Issue #133.

    A login is a label and GitHub hands it back out; the account behind it is what lasts. Asked
    with an id rather than a name, so the answer is about the person somebody meant rather than
    about whoever holds their old name now.
    """

    async def test_an_account_that_is_there_answers_with_its_login(self) -> None:
        async with client_with(responds(200, {"login": "monalisa", "id": 583231})) as client:
            assert await client.user_login(583231) == "monalisa"

    async def test_an_account_that_is_gone_is_nobody(self) -> None:
        """A deleted account, or an id nothing ever held. Both leave the caller with the name it
        already had, which is no worse than never having asked."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            assert await client.user_login(999_999) is None

    async def test_an_answer_with_no_login_in_it_is_nobody(self) -> None:
        """GitHub always sends one. Reading a body that does not carry it as an account would
        hand back None and read as a deletion, which is a different thing entirely."""
        async with client_with(responds(200, {"id": 583231})) as client:
            assert await client.user_login(583231) is None

    async def test_it_asks_the_account_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps({"login": "monalisa"}))

        async with client_with(handler) as client:
            await client.user_login(583231)

        assert seen == ["/user/583231"]

    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_anything_else_github_says_is_raised_rather_than_answered(
        self, status: int
    ) -> None:
        """The same rule the call above follows. A question that could not be put is not an
        answer, and answering None here would report a live account as deleted."""
        async with client_with(responds(status, {"message": "nope"})) as client:
            with pytest.raises(GitHubError):
                await client.user_login(583231)


class TestAWriteToARepositoryThatMoved:
    """The same 301, on the half of the client that changes something.

    Following redirects was turned on for reads, where an unfollowed one reached the person who
    ran the command as "GitHub could not be reached". On a write it is worse than not following:
    httpx re-issues a redirected POST as a bodyless GET, GitHub answers the label list with 200,
    and the client reports a write that never happened. Nothing after it can tell the difference,
    so `/set_in_review` says it worked, writes the status to the row and renders it into the
    thread, and the item on GitHub keeps whatever label it had.
    """

    def _renamed(self, seen: list[tuple[str, str]]):
        """The answer GitHub really gives, checked against the live API.

        A renamed repository answers 301 on this exact endpoint, and the Location it gives is
        the canonical numeric form with the rest of the path kept: asking for
        `/repos/facebook/jest/issues/1/labels` comes back pointing at
        `https://api.github.com/repositories/15062869/issues/1/labels`.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path))
            if request.url.path.startswith("/repositories/"):
                return httpx.Response(200, content=json.dumps([]))
            return httpx.Response(
                301,
                headers={
                    "Location": "https://api.github.com/repositories/15062869/issues/7/labels"
                },
            )

        return handler

    async def test_the_label_is_written_to_the_new_name_by_the_same_method(self) -> None:
        seen: list[tuple[str, str]] = []
        async with client_with(self._renamed(seen)) as client:
            await client.add_label("acme", "widgets", 7, "IN_REVIEW")

        assert seen == [
            ("POST", "/repos/acme/widgets/issues/7/labels"),
            ("POST", "/repositories/15062869/issues/7/labels"),
        ], "the redirected write was downgraded to a read"

    async def test_a_redirect_off_the_host_is_refused_rather_than_handed_the_token(self) -> None:
        """Following one by hand is deciding who the Authorization header goes to."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(301, headers={"Location": "https://example.invalid/labels"})

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match=r"example\.invalid"):
                await client.add_label("acme", "widgets", 7, "IN_REVIEW")

    async def test_a_redirect_that_says_nothing_about_where_is_refused(self) -> None:
        async with client_with(responds(301)) as client:
            with pytest.raises(GitHubUnavailableError, match="without saying where"):
                await client.add_label("acme", "widgets", 7, "IN_REVIEW")

    async def test_a_chain_that_never_resolves_stops_and_says_so(self) -> None:
        """Loud and retryable, which is what the write path answered before it followed any."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                301, headers={"Location": f"https://api.github.com{request.url.path}/on"}
            )

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError, match="301"):
                await client.add_label("acme", "widgets", 7, "IN_REVIEW")

    async def test_a_removal_follows_the_rename_too(self) -> None:
        seen: list[tuple[str, str]] = []
        async with client_with(self._renamed(seen)) as client:
            await client.remove_label("acme", "widgets", 7, "BACKLOG")

        assert [method for method, _ in seen] == ["DELETE", "DELETE"]


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
        client = HttpGitHubClient()

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


class TestComparingTwoCommits:
    """Reading what a push did to a branch.

    Every test here goes through the transport rather than round a stub, because the thing worth
    pinning is the request: the path shape, what is escaped into it, and which answers are turned
    into None instead of thrown.
    """

    def compare(self, **overrides: object) -> dict[str, object]:
        body = {
            "status": "ahead",
            "total_commits": 1,
            "commits": [
                {
                    "sha": "a" * 40,
                    "commit": {"message": "Add the endpoint"},
                    "author": {"login": "octocat", "id": 1},
                    "parents": [{"sha": "b" * 40}],
                }
            ],
        }
        body.update(overrides)
        return body

    async def test_a_compare_reads_as_a_range(self) -> None:
        async with client_with(responds(200, self.compare())) as client:
            found = await client.compare_commits(payloads.OWNER, payloads.REPO, "b" * 40, "a" * 40)

        assert found is not None
        assert found.status == "ahead"
        assert [commit.sha for commit in found.commits] == ["a" * 40]

    async def test_it_asks_the_compare_endpoint_with_both_ends_in_the_path(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps(self.compare()))

        async with client_with(handler) as client:
            await client.compare_commits(payloads.OWNER, payloads.REPO, "old", "new")

        assert seen == [f"/repos/{payloads.OWNER}/{payloads.REPO}/compare/old...new"]

    async def test_a_ref_with_a_slash_in_it_stays_one_path_segment(self) -> None:
        """The two ends arrive off a webhook payload, and this is the one place a value nobody
        validated decides which endpoint gets called. A branch name is a legal ref here, and
        `feat/x` unescaped reads as two more segments and asks GitHub something else entirely."""
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps(self.compare()))

        async with client_with(handler) as client:
            await client.compare_commits(payloads.OWNER, payloads.REPO, "feat/x", "main")

        assert b"/compare/feat%2Fx...main" in seen[0]

    async def test_a_compare_github_has_nothing_for_is_not_an_error(self) -> None:
        """A branch deleted between the push and this call never comes back. Raising would put
        the delivery through sixteen retries over two hours to be told the same thing, holding up
        everything behind it."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            found = await client.compare_commits(payloads.OWNER, payloads.REPO, "gone", "also")

        assert found is None

    async def test_a_body_with_no_status_is_nothing_rather_than_a_guess(self) -> None:
        async with client_with(responds(200, {"total_commits": 4})) as client:
            found = await client.compare_commits(payloads.OWNER, payloads.REPO, "b", "a")

        assert found is None

    async def test_github_being_down_still_raises(self) -> None:
        """The other half of the 404 rule. A push is worth retrying; only a thing that is
        permanently gone is worth giving up on."""
        async with client_with(responds(500)) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.compare_commits(payloads.OWNER, payloads.REPO, "b", "a")


class TestReadingTheCommitsInAPullRequest:
    """Who wrote what is on a pull request, which is how a CI result finds who to ring.

    The rows are the same shape a compare sends, so `mapping.commit_ref` is shared. What differs
    is that this one is read for the PEOPLE: a row whose `author` GitHub could not link to an
    account is not a broken row, it is a commit by somebody with no GitHub account on that email,
    and the honest answer is that it names nobody.
    """

    def row(self, sha: str = "a" * 40, login: str | None = "octocat", parents: int = 1) -> dict:
        """One commit as the endpoint sends it.

        `author` at the TOP level is the linked account; `commit.author` is the free text the
        committer put in their git config. The two are different fields and only the first is
        anybody's to prove, which is why this helper lets a test set them apart.
        """
        return {
            "sha": sha,
            "commit": {"message": "Add the thing", "author": {"name": "Somebody Else"}},
            "author": {"login": login, "id": 583231} if login is not None else None,
            "parents": [{"sha": "b" * 40}] * parents,
        }

    async def test_it_asks_the_commits_endpoint_for_that_pull_request(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert seen == [f"/repos/{payloads.OWNER}/{payloads.REPO}/pulls/7/commits"]

    async def test_a_page_of_rows_becomes_commits_with_their_accounts(self) -> None:
        handler = responds(200, [self.row("a" * 40, "octocat"), self.row("c" * 40, "hubot")])

        async with client_with(handler) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is not None
        assert [commit.author.login for commit in found if commit.author] == ["octocat", "hubot"]

    async def test_a_commit_with_no_linked_account_comes_back_with_no_author(self) -> None:
        """Not an error and not a gap. Somebody committed under an address no GitHub account
        holds, so there is nobody to ring, and saying so is the whole answer."""
        async with client_with(responds(200, [self.row(login=None)])) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is not None
        assert len(found) == 1
        assert found[0].author is None

    async def test_the_name_in_the_commit_is_never_read_as_the_account(self) -> None:
        """`commit.author.name` is whatever the committer typed into `git config`, so anybody who
        can push could put a colleague's name on their work. Only the resolved account counts."""
        async with client_with(responds(200, [self.row(login=None)])) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is not None
        assert found[0].author is None, "the free-text git name was read as an account"

    async def test_a_merge_commit_says_it_is_one(self) -> None:
        """The caller drops merges, because a merge's account is whoever pressed Update branch
        rather than whoever wrote anything."""
        handler = responds(200, [self.row(parents=2), self.row("c" * 40, parents=1)])

        async with client_with(handler) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is not None
        assert [commit.merge for commit in found] == [True, False]

    async def test_every_page_is_read(self) -> None:
        """A branch argued over for a week runs past one page, and a half-read list here leaves a
        contributor out without saying so - which is the defect this read exists to prevent."""
        asked: list[str] = []
        following = {"Link": '<https://api.github.com/next?page=2>; rel="next"'}

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            if len(asked) == 1:
                return httpx.Response(
                    200, content=json.dumps([self.row("a" * 40, "octocat")]), headers=following
                )
            return httpx.Response(200, content=json.dumps([self.row("c" * 40, "hubot")]))

        async with client_with(handler) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert len(asked) == 2
        assert found is not None
        assert [commit.author.login for commit in found if commit.author] == ["octocat", "hubot"]

    async def test_a_row_with_no_sha_is_dropped_without_losing_the_rest(self) -> None:
        handler = responds(200, [{"commit": {}}, self.row("c" * 40, "hubot")])

        async with client_with(handler) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is not None
        assert [commit.sha for commit in found] == ["c" * 40]

    async def test_a_body_that_is_not_an_array_is_read_as_no_commits(self) -> None:
        """This endpoint sends a bare array, unlike check runs, which wrap theirs in an object. A
        wrapper arriving here is GitHub changing shape, and reading one as a list of commits would
        be worse than reading it as none."""
        async with client_with(responds(200, {"commits": [self.row()]})) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found == []

    async def test_a_pull_request_github_has_lost_answers_none(self) -> None:
        """None rather than the empty list, and the caller tells them apart: nothing to read is a
        narrower audience, while no commits with accounts is the same audience said honestly."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            found = await client.list_pull_request_commits(payloads.OWNER, payloads.REPO, 7)

        assert found is None

    async def test_it_escapes_the_repository_in_the_path(self) -> None:
        """Read off `raw_path`, the bytes that go on the wire. `url.path` hands back the decoded
        string, so asserting on that would pass against a path that escaped nothing."""
        wire: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.list_pull_request_commits("acme", "widget/evil", 7)

        # `startswith`, because this is a paged read and `get_pages` puts `per_page` on the
        # query string. The check-runs test does the same for the same reason.
        assert wire[0].startswith(b"/repos/acme/widget%2Fevil/pulls/7/commits")


class TestReadingOneCommitsNumbers:
    def commit(self, **overrides: object) -> dict[str, object]:
        body = {"stats": {"additions": 42, "deletions": 7, "total": 49}, "files": [{}, {}, {}]}
        body.update(overrides)
        return body

    async def test_the_numbers_come_back(self) -> None:
        async with client_with(responds(200, self.commit())) as client:
            found = await client.commit_stats(payloads.OWNER, payloads.REPO, "a" * 40)

        assert found is not None
        assert (found.additions, found.deletions, found.changed_files) == (42, 7, 3)

    async def test_it_asks_the_commit_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps(self.commit()))

        async with client_with(handler) as client:
            await client.commit_stats(payloads.OWNER, payloads.REPO, "a" * 40)

        assert seen == [f"/repos/{payloads.OWNER}/{payloads.REPO}/commits/{'a' * 40}"]

    async def test_a_commit_that_has_been_collected_is_not_an_error(self) -> None:
        """A rebase during the push leaves SHAs the compare listed and the read cannot find. The
        caller carries on with the commits it can read rather than losing all of them."""
        async with client_with(responds(404, {"message": "No commit found"})) as client:
            found = await client.commit_stats(payloads.OWNER, payloads.REPO, "a" * 40)

        assert found is None

    async def test_a_commit_with_no_stats_block_is_nothing(self) -> None:
        async with client_with(responds(200, {"files": [{}]})) as client:
            found = await client.commit_stats(payloads.OWNER, payloads.REPO, "a" * 40)

        assert found is None

    async def test_github_being_down_still_raises(self) -> None:
        async with client_with(responds(503)) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.commit_stats(payloads.OWNER, payloads.REPO, "a" * 40)


class TestWhatOneAccountMayDoToARepository:
    """Read by `/unregister`, and the only question asked of GitHub anywhere on that path.

    The login handed in must be one GitHub itself vouched for a moment ago rather than one out of
    `user_links`, which records a claim somebody made about themselves.
    """

    def answering(self, permission: object):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"permission": permission}))

        return handler

    @pytest.mark.parametrize("permission", ["admin", "write", "read", "none"])
    async def test_it_answers_what_github_said(self, permission: str) -> None:
        async with client_with(self.answering(permission)) as client:
            assert await client.permission_for("acme", "widget", "octocat") == permission

    async def test_it_asks_the_collaborator_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps({"permission": "admin"}))

        async with client_with(handler) as client:
            await client.permission_for("acme", "widget", "octocat")

        assert seen == ["/repos/acme/widget/collaborators/octocat/permission"]

    async def test_every_part_of_the_path_is_escaped(self) -> None:
        """All three arrive from outside: two off a stored repository name and one from whatever
        GitHub answered at `/user`."""
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps({"permission": "admin"}))

        async with client_with(handler) as client:
            await client.permission_for("acme", "widget/..", "octo/cat")

        assert b"/repos/acme/widget%2F../collaborators/octo%2Fcat/permission" in seen[0]

    async def test_somebody_who_is_not_a_collaborator_at_all_has_nothing(self) -> None:
        """GitHub answers 404 for an account it has never heard of and for one with no
        relationship to the repository, and both mean the same thing here."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        async with client_with(handler) as client:
            assert await client.permission_for("acme", "widget", "stranger") == "none"

    @pytest.mark.parametrize("permission", [None, 7, [], {}])
    async def test_a_body_that_does_not_say_reads_as_nothing(self, permission: object) -> None:
        """Nothing rather than a guess. This answer decides whether a binding is destroyed, so the
        only safe reading of an unusable one is the one that refuses."""
        async with client_with(self.answering(permission)) as client:
            assert await client.permission_for("acme", "widget", "octocat") == "none"

    async def test_github_being_down_still_raises(self) -> None:
        """Unlike the 404. An outage is not an answer about somebody's permissions, and treating
        it as one would refuse a legitimate admin with a message blaming them."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        async with client_with(handler) as client:
            with pytest.raises(GitHubUnavailableError):
                await client.permission_for("acme", "widget", "octocat")


class TestWritingPeople:
    """Issue #106. The first writes this bot makes about a person rather than a label."""

    def _recording(self, status: int = 201):
        seen: list[tuple[str, str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else None
            seen.append((request.method, request.url.path, body))
            return httpx.Response(status, content=json.dumps({}))

        return seen, handler

    async def test_asking_for_a_review_uses_the_pulls_endpoint(self) -> None:
        """Not the issues one the labels use. GitHub keeps reviewers off that endpoint entirely,
        because an issue has none."""
        seen, handler = self._recording()

        async with client_with(handler) as client:
            await client.request_reviewers("acme", "widget", 7, ["alice"])

        assert seen == [
            ("POST", "/repos/acme/widget/pulls/7/requested_reviewers", {"reviewers": ["alice"]})
        ]

    async def test_withdrawing_a_review(self) -> None:
        seen, handler = self._recording(status=200)

        async with client_with(handler) as client:
            await client.remove_reviewers("acme", "widget", 7, ["alice"])

        assert seen == [
            ("DELETE", "/repos/acme/widget/pulls/7/requested_reviewers", {"reviewers": ["alice"]})
        ]

    async def test_assigning_uses_the_issues_endpoint(self) -> None:
        seen, handler = self._recording()

        async with client_with(handler) as client:
            await client.add_assignees("acme", "widget", 12, ["alice"])

        assert seen == [
            ("POST", "/repos/acme/widget/issues/12/assignees", {"assignees": ["alice"]})
        ]

    async def test_unassigning(self) -> None:
        seen, handler = self._recording(status=200)

        async with client_with(handler) as client:
            await client.remove_assignees("acme", "widget", 12, ["alice"])

        assert seen == [
            ("DELETE", "/repos/acme/widget/issues/12/assignees", {"assignees": ["alice"]})
        ]

    async def test_a_withdrawal_of_something_never_asked_for_is_done_rather_than_failed(
        self,
    ) -> None:
        """The same rule the label removal follows: a 404 means the end state is the wanted one."""
        _, handler = self._recording(status=404)

        async with client_with(handler) as client:
            await client.remove_reviewers("acme", "widget", 7, ["alice"])
            await client.remove_assignees("acme", "widget", 12, ["alice"])

    async def test_a_repository_name_with_a_slash_in_it_cannot_change_the_path(self) -> None:
        """`add_label` does not encode these and is the odd one out. A name is GitHub's to shape,
        and a stray separator would read as another path segment."""
        wire: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path.decode())
            return httpx.Response(201, content="{}")

        async with client_with(handler) as client:
            await client.request_reviewers("acme", "wid/get", 7, ["alice"])

        assert wire == ["/repos/acme/wid%2Fget/pulls/7/requested_reviewers"]


class TestAskingWhetherSomebodyCanBeAssigned:
    """GitHub is silent about this on the write itself, so it has to be asked separately."""

    async def test_a_person_it_would_take(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        async with client_with(handler) as client:
            assert await client.can_be_assigned("acme", "widget", "alice") is True

    async def test_a_person_it_would_not(self) -> None:
        """404 is an answer here rather than a failure, which is why it is caught."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({}))

        async with client_with(handler) as client:
            assert await client.can_be_assigned("acme", "widget", "stranger") is False

    async def test_the_login_is_encoded(self) -> None:
        wire: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            wire.append(request.url.raw_path.decode())
            return httpx.Response(204)

        async with client_with(handler) as client:
            await client.can_be_assigned("acme", "widget", "a/b")

        assert wire == ["/repos/acme/widget/assignees/a%2Fb"]


class TestARefusalToldApartFromAnOutage:
    """Issue #106. A 422 used to fall into the catch-all and read as GitHub being unreachable,
    which is both wrong and retryable, for a refusal that retrying can never change."""

    async def test_github_s_own_sentence_is_carried(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422,
                content=json.dumps(
                    {"message": "Reviews may only be requested from collaborators."}
                ),
            )

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError, match="only be requested from collaborators"):
                await client.request_reviewers("acme", "widget", 7, ["stranger"])

    async def test_a_body_that_is_not_json_still_says_something(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, content="not json at all")

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError, match="requested_reviewers"):
                await client.request_reviewers("acme", "widget", 7, ["stranger"])

    async def test_a_body_that_is_json_but_not_an_object(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, content=json.dumps(["nope"]))

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError, match="refused the request"):
                await client.request_reviewers("acme", "widget", 7, ["stranger"])

    async def test_a_body_with_no_message_in_it(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, content=json.dumps({"errors": []}))

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError, match="refused the request"):
                await client.request_reviewers("acme", "widget", 7, ["stranger"])

    async def test_it_is_not_reported_as_unavailable(self) -> None:
        """The distinction the type exists for: every caller treats unavailable as retryable."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, content=json.dumps({"message": "Nope."}))

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError):
                await client.add_assignees("acme", "widget", 12, ["stranger"])


class TestListingARepositoryLabels:
    """Issue #104. Read so a typed label can be checked before it is written, because GitHub
    creates a name it has never seen rather than refusing one."""

    async def test_it_reads_the_names(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=json.dumps([{"name": "bug"}, {"name": "good first issue"}])
            )

        async with client_with(handler) as client:
            assert await client.list_labels("acme", "widget") == ["bug", "good first issue"]

    async def test_it_asks_the_repository_s_own_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path.decode())
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.list_labels("acme", "wid/get")

        assert seen[0].startswith("/repos/acme/wid%2Fget/labels")

    async def test_a_row_that_is_not_a_label_is_skipped(self) -> None:
        """The body comes off the network. A row this cannot read is one label missing from the
        picker, not a command that fails."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=json.dumps(["nope", {"name": ""}, {"colour": "red"}, {"name": "bug"}]),
            )

        async with client_with(handler) as client:
            assert await client.list_labels("acme", "widget") == ["bug"]

    async def test_a_body_that_is_not_a_list_at_all(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"message": "nope"}))

        async with client_with(handler) as client:
            assert await client.list_labels("acme", "widget") == []

    async def test_a_repository_with_no_labels(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            assert await client.list_labels("acme", "widget") == []

    async def test_every_page_is_read(self) -> None:
        """A repository with a real taxonomy has more than one page of them, and a half-read list
        would refuse a label that exists."""
        asked: list[str] = []
        following = {"Link": '<https://api.github.com/repos/acme/widget/labels?page=2>; rel="next"'}

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            if len(asked) == 1:
                return httpx.Response(
                    200,
                    content=json.dumps([{"name": "bug"}]),
                    # Followed rather than built, because the cursor is GitHub's own.
                    headers=following,
                )
            return httpx.Response(200, content=json.dumps([{"name": "last"}]))

        async with client_with(handler) as client:
            found = await client.list_labels("acme", "widget")

        assert len(asked) == 2
        assert found == ["bug", "last"]


class TestEveryReviewOnAPullRequest:
    """Issue #155. Read from GitHub rather than tallied from the webhooks that arrive.

    A dismissed review is the same row with a different state and `dismissed` is not an action
    this bot subscribes to, so a tally kept here would go on counting an approval somebody had
    taken back.
    """

    def _repository(self) -> RepositorySnapshot:
        return RepositorySnapshot(
            github_repo_id=1,
            owner="Canon-Regularis",
            name="Shannon-bot",
            html_url="https://github.com/Canon-Regularis/Shannon-bot",
        )

    def _row(self, review_id: int, state: str, login: str = "monalisa") -> dict[str, object]:
        return {
            "id": review_id,
            "state": state,
            "user": {"login": login, "id": 200},
            "body": "",
            "html_url": "https://github.com/x/y/pull/7#pullrequestreview-1",
            "submitted_at": "2026-08-11T11:00:00Z",
        }

    async def test_it_asks_the_reviews_endpoint_for_that_pull_request(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.list_reviews(self._repository(), 7)

        assert seen == ["/repos/Canon-Regularis/Shannon-bot/pulls/7/reviews"]

    async def test_a_page_of_rows_becomes_snapshots(self) -> None:
        """The REST API sends the state uppercased and webhooks send it lowercased, which is
        what `ReviewSnapshot.verdict` exists to flatten."""
        handler = responds(200, [self._row(1, "APPROVED"), self._row(2, "CHANGES_REQUESTED")])

        async with client_with(handler) as client:
            found = await client.list_reviews(self._repository(), 7)

        assert found is not None
        assert [review.verdict for review in found] == ["approved", "changes_requested"]
        assert [review.review_id for review in found] == [1, 2]

    async def test_it_follows_the_link_header(self) -> None:
        """A pull request argued over for a week runs past one page, and a half-read list is the
        shape that reports agreement nobody reached."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            index = calls["n"]
            calls["n"] += 1
            headers = (
                {"Link": '<https://api.github.com/next?after=cursor>; rel="next"'}
                if index == 0
                else {}
            )
            return httpx.Response(
                200, content=json.dumps([self._row(index + 1, "APPROVED")]), headers=headers
            )

        async with client_with(handler) as client:
            found = await client.list_reviews(self._repository(), 7)

        assert found is not None
        assert [review.review_id for review in found] == [1, 2]

    async def test_a_body_that_is_not_a_list_is_read_as_nothing(self) -> None:
        """GitHub answers this one with an array rather than an object holding one, so a body
        shaped like the check-runs endpoint means something has changed rather than nothing."""
        async with client_with(responds(200, {})) as client:
            assert await client.list_reviews(self._repository(), 7) == []

    async def test_a_row_without_an_id_is_dropped_and_the_rest_kept(self) -> None:
        handler = responds(200, [{"state": "APPROVED"}, self._row(2, "APPROVED")])

        async with client_with(handler) as client:
            found = await client.list_reviews(self._repository(), 7)

        assert found is not None
        assert [review.review_id for review in found] == [2]

    async def test_a_pull_request_github_does_not_have_answers_none(self) -> None:
        """Distinct from the empty list, which is a pull request nobody has reviewed. A 404 is
        final, so retrying the delivery for two hours would spend every attempt on it."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            assert await client.list_reviews(self._repository(), 7) is None

    async def test_a_pull_request_nobody_has_reviewed_answers_an_empty_list(self) -> None:
        async with client_with(responds(200, [])) as client:
            assert await client.list_reviews(self._repository(), 7) == []


class TestEveryInlineCommentOnAPullRequest:
    """Issue #231. Read so a reply can find whoever it answers, which its webhook never says.

    GitHub has no endpoint for one thread, so the whole list is read and the caller picks the
    thread out by the comment that opened it.
    """

    def _repository(self) -> RepositorySnapshot:
        return RepositorySnapshot(
            github_repo_id=1,
            owner="Canon-Regularis",
            name="Shannon-bot",
            html_url="https://github.com/Canon-Regularis/Shannon-bot",
        )

    def _row(
        self, comment_id: int, login: str = "beedware", in_reply_to_id: int | None = None
    ) -> dict[str, object]:
        """A row as the list endpoint sends it, which is the object a webhook carries as its
        `comment`. A comment that opens a thread has no `in_reply_to_id` key at all."""
        row: dict[str, object] = {
            "id": comment_id,
            "path": "src/river.tex",
            "line": 246,
            "user": {"login": login, "id": 3001},
            "body": "Elongate the river",
            "html_url": f"https://github.com/x/y/pull/7#discussion_r{comment_id}",
            "created_at": "2026-10-06T15:13:49Z",
        }
        if in_reply_to_id is not None:
            row["in_reply_to_id"] = in_reply_to_id
        return row

    async def test_it_asks_the_comments_endpoint_for_that_pull_request(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps([]))

        async with client_with(handler) as client:
            await client.list_review_comments(self._repository(), 7)

        assert seen == ["/repos/Canon-Regularis/Shannon-bot/pulls/7/comments"]

    async def test_a_page_of_rows_becomes_snapshots(self) -> None:
        """Who wrote each one, and which thread it sits in, are the two things a caller reads."""
        handler = responds(200, [self._row(10), self._row(11, login="mkutay", in_reply_to_id=10)])

        async with client_with(handler) as client:
            found = await client.list_review_comments(self._repository(), 7)

        assert found is not None
        assert [comment.comment_id for comment in found] == [10, 11]
        assert [comment.in_reply_to_id for comment in found] == [None, 10]
        assert [comment.author.login for comment in found if comment.author] == [
            "beedware",
            "mkutay",
        ]
        assert {comment.item_number for comment in found} == {7}

    async def test_it_follows_the_link_header(self) -> None:
        """A review argued over for a week runs past one page, and a half-read list is how the
        person being answered goes unrung with nothing saying so."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            index = calls["n"]
            calls["n"] += 1
            headers = (
                {"Link": '<https://api.github.com/next?after=cursor>; rel="next"'}
                if index == 0
                else {}
            )
            return httpx.Response(200, content=json.dumps([self._row(index + 1)]), headers=headers)

        async with client_with(handler) as client:
            found = await client.list_review_comments(self._repository(), 7)

        assert found is not None
        assert [comment.comment_id for comment in found] == [1, 2]

    async def test_a_body_that_is_not_a_list_is_read_as_nothing(self) -> None:
        async with client_with(responds(200, {})) as client:
            assert await client.list_review_comments(self._repository(), 7) == []

    async def test_a_row_without_an_id_is_dropped_and_the_rest_kept(self) -> None:
        handler = responds(200, [{"body": "no id"}, self._row(2)])

        async with client_with(handler) as client:
            found = await client.list_review_comments(self._repository(), 7)

        assert found is not None
        assert [comment.comment_id for comment in found] == [2]

    async def test_a_pull_request_github_does_not_have_answers_none(self) -> None:
        """Distinct from the empty list, which is a pull request nobody has commented on inline.
        A 404 is final, so retrying the delivery for two hours would spend every attempt on it."""
        async with client_with(responds(404, {"message": "Not Found"})) as client:
            assert await client.list_review_comments(self._repository(), 7) is None

    async def test_a_pull_request_with_no_inline_comments_answers_an_empty_list(self) -> None:
        async with client_with(responds(200, [])) as client:
            assert await client.list_review_comments(self._repository(), 7) == []

    @pytest.mark.parametrize(
        ("status", "raised"),
        [(503, GitHubUnavailableError), (401, GitHubAuthError)],
    )
    async def test_anything_worse_than_a_404_is_raised_for_the_caller_to_judge(
        self, status: int, raised: type[GitHubError]
    ) -> None:
        """Only a 404 is answered here. Whether a reply waits for GitHub or goes out without the
        people it answers is the caller's decision, and it decides on which of these it was."""
        async with client_with(responds(status, {"message": "nope"})) as client:
            with pytest.raises(raised):
                await client.list_review_comments(self._repository(), 7)


class TestWhichKindOfAccountAnOwnerIs:
    """What `/link_team` asks before it writes a mapping.

    Only an organisation has teams, so a team pointed at a personal account's repository can
    never match anything GitHub sends. `GET /users/{owner}` answers for both kinds and says
    which, so one Metadata-only call settles it and no App permission has to be added - which
    matters, because granting one suspends event delivery until somebody accepts it.
    """

    def answering(self, kind: object):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=json.dumps({"type": kind}))

        return handler

    async def test_an_organisation(self) -> None:
        async with client_with(self.answering("Organization")) as client:
            assert await client.is_organisation("acme") is True

    @pytest.mark.parametrize("kind", ["User", "Bot", "", None, 7, [], {}])
    async def test_everything_else_is_a_person(self, kind: object) -> None:
        """Including shapes GitHub does not send today. This answer refuses rather than allows,
        and refusing on something unreadable is the right way round: a refusal is one command to
        run again, while a mapping written against a personal account is silent for ever."""
        async with client_with(self.answering(kind)) as client:
            assert await client.is_organisation("monalisa") is False

    async def test_an_account_github_does_not_have(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=json.dumps({"message": "Not Found"}))

        async with client_with(handler) as client:
            assert await client.is_organisation("nobody") is False

    async def test_it_asks_the_account_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=json.dumps({"type": "Organization"}))

        async with client_with(handler) as client:
            await client.is_organisation("acme")

        assert seen == ["/users/acme"]

    async def test_the_owner_is_escaped_into_the_path(self) -> None:
        """It arrives off a stored repository name, and every sibling here quotes what it
        interpolates."""
        seen: list[bytes] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path)
            return httpx.Response(200, content=json.dumps({"type": "User"}))

        async with client_with(handler) as client:
            await client.is_organisation("acme/..")

        assert b"/users/acme%2F.." in seen[0]


class TestWritingToAProjectBoard:
    """`patch_json`, the one write that does not go to a repository.

    Deliberately absent from the `GitHubClient` Protocol: that one carries the JSON readers only
    because the wiring hands the same object to the board reader, which sends each read with a
    person's authorisation, and a PATCH there would give every service the ability to write to
    any path.
    """

    async def test_it_sends_the_body_with_patch(self) -> None:
        seen: list[tuple[str, str, bytes]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, request.content))
            return httpx.Response(200, content=json.dumps({}))

        async with client_with(handler) as client:
            await client.patch_json(
                "/users/monalisa/projectsV2/6/items/99",
                owner="monalisa",
                json={"fields": [{"id": 39518, "value": "opt-done"}]},
            )

        assert seen[0][0] == "PATCH"
        assert seen[0][1] == "/users/monalisa/projectsV2/6/items/99"
        assert json.loads(seen[0][2]) == {"fields": [{"id": 39518, "value": "opt-done"}]}

    async def test_a_body_github_will_not_take_carries_its_own_words_back(self) -> None:
        """The reason this write is diagnosable at all. Its body shape came from published
        documentation rather than from a live board, so GitHub's 422 message is the first real
        evidence either way and has to survive the trip."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422, content=json.dumps({"message": "Could not resolve to a node"})
            )

        async with client_with(handler) as client:
            with pytest.raises(GitHubRefusedError, match="Could not resolve to a node"):
                await client.patch_json(
                    "/users/monalisa/projectsV2/6/items/99", owner="monalisa", json={}
                )
