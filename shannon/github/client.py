"""The GitHub calls this project makes, and the narrow handles it makes them through.

Each protocol below names only what one caller needs, so a handle that can put a reviewer on a
pull request cannot also read a commit or write a label. They are declared here rather than
beside their implementations, which reach the database and would drag storage in behind HTTP.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx

from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.domain.models import (
    CheckRun,
    CommitRange,
    CommitStats,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewSnapshot,
)
from shannon.github import mapping
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRefusedError,
    GitHubUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

# How far a paged read will follow the Link header. At a hundred rows a page this is more
# than any board or list this bot reads, and it is the only thing standing between a
# self-referential cursor and a loop that never ends.
MAX_PAGES = 50

# How far a write will follow a redirect. GitHub answers a renamed repository with the current
# name in one hop rather than a chain, so this is a bound on something that should not happen
# rather than room to work in.
MAX_WRITE_REDIRECTS = 3

# GitHub's maximum, and the right end of the range to be at. A backlog is read whole, so round
# trips are the thing to spend the fewest of; there is no partial answer worth asking for.
LIST_PAGE_SIZE = 100

# Constrained rather than bound, so reading the pulls endpoint gives back pull requests and the
# issues endpoint gives back issues. One paging helper serves both and neither caller has to say
# which of the two it got.
_Item = TypeVar("_Item", PullRequestSnapshot, IssueSnapshot)


def _an_issue(payload: Any, repository: RepositorySnapshot) -> IssueSnapshot | None:
    """An issue row, or None for the pull requests GitHub mixes into the issues endpoint."""
    if mapping.is_pull_request(payload):
        return None
    return mapping.issue(payload, repository)


class SuppliesTokens(Protocol):
    """A bearer token for calls about one GitHub account, or the empty string for none.

    Empty rather than an exception: it is the state an unset token already produced, so a
    deployment with no App configured answers on public endpoints rather than failing on the
    first command anybody runs.
    """

    async def token_for(self, owner: str) -> str: ...


class LooksUpRepository(Protocol):
    """Resolving a repository by owner and name, which is all the link commands need."""

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...


class ListsOpenItems(LooksUpRepository, Protocol):
    """Every open pull request or issue on a repository, which is all `/refresh` needs.

    A resolved repository rather than an owner and a name, so the current name is used rather
    than a stale one, and because `list_open_issues` cannot work without it: GitHub's issue rows
    carry no repository object at all.
    """

    async def list_open_pull_requests(
        self, repository: RepositorySnapshot
    ) -> Sequence[PullRequestSnapshot]: ...

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]: ...


class LooksUpUsers(Protocol):
    """Asking who holds a GitHub login, by the id that outlasts it.

    Nothing in `shannon/` consumes this today. `/link` did, back when it took a login somebody
    typed and had to find out whether anybody held it; issue #144 took the typing away, so the
    question stopped being asked. Kept rather than deleted because it is a correct, tested thing
    the client can do and the next feature that starts from a name will want it, and because an
    orphan with no note on it is what rots. `user_id` below is its only implementation.
    """

    async def user_id(self, login: str) -> int | None: ...


class ReadsCommits(Protocol):
    """What a push did to a branch, which is all the commit announcer needs.

    Both answer None rather than raising when GitHub has nothing. A SHA that has been collected
    never comes back, so a retry would spend sixteen attempts over two hours on the same answer.
    """

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None: ...

    async def commit_stats(self, owner: str, name: str, sha: str) -> CommitStats | None: ...


class ReadsChecks(Protocol):
    """Every CI job on one commit, which is all the check announcer needs.

    None rather than raising when GitHub has nothing, for the reason `ReadsCommits` gives.
    """

    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...


class ReadsReviews(Protocol):
    """Every review on one pull request, which is all the approval round-up needs.

    Asked of GitHub rather than remembered, because nothing here can remember it correctly: a
    review is rewritten in place when it is dismissed, and `pull_request_review.dismissed` is not
    an action this bot subscribes to. A stored verdict would go stale with nothing saying so.

    None rather than raising when GitHub has nothing, for the reason `ReadsCommits` gives.
    """

    async def list_reviews(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewSnapshot] | None: ...


class GitHubClient(ListsOpenItems, LooksUpUsers, ReadsChecks, ReadsCommits, ReadsReviews, Protocol):
    """The GitHub calls the rest of the project is allowed to make.

    Commands and services depend on this rather than on httpx, so nothing outside this module
    knows GitHub is reached over HTTP.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...

    async def user_id(self, login: str) -> int | None: ...

    async def user_login(self, account_id: int) -> str | None: ...

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot: ...

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None: ...

    async def commit_stats(self, owner: str, name: str, sha: str) -> CommitStats | None: ...

    async def list_open_pull_requests(
        self, repository: RepositorySnapshot
    ) -> Sequence[PullRequestSnapshot]: ...

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]: ...

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...

    async def list_labels(self, owner: str, name: str) -> Sequence[str]: ...

    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None: ...

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def request_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def remove_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def add_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def remove_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None: ...

    async def can_be_assigned(self, owner: str, name: str, login: str) -> bool: ...

    # Untyped bodies, for the project endpoints, which answer with arrays and are parsed by a
    # module that checks every field it touches. Declared here because the wiring hands this same
    # object to the board reader, and a stand-in without them would fail on the first poll.
    async def get_json(self, path: str, *, owner: str = "", **params: str | int) -> object: ...

    def get_pages(
        self, path: str, *, owner: str = "", **params: str | int
    ) -> AsyncIterator[object]: ...


class HttpGitHubClient:
    def __init__(
        self,
        *,
        tokens: SuppliesTokens | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # A supplier rather than a token: each call carries a credential minted for the account
        # it is about, so the header is decided per request. None means no App is configured and
        # every request goes out unauthenticated, which is what an empty token already did.
        self._tokens = tokens
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=_headers(),
            # Renaming a repository or its owner turns every lookup of the old name into a 301,
            # as does moving an issue between repositories. httpx does not follow redirects
            # unless told to, and an unfollowed one surfaces as "GitHub could not be reached".
            # Safe with a token: httpx drops Authorization on a cross-origin redirect.
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpGitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        payload = await self._get(f"/repos/{owner}/{name}", owner)
        snapshot = mapping.repository(payload)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable repository for {owner}/{name}"
            )
        return snapshot

    async def user_id(self, login: str) -> int | None:
        """Who holds this login, by GitHub's own numeric id, or None if nobody does.

        A public endpoint, so it answers with no token set. The id rather than a yes, because
        GitHub frees a login when it is renamed and lets anybody take it; storing the id is what
        lets a mention built later be checked against the person somebody meant. Only "not there"
        becomes None, because anything else leaves a person who can be told to try again.
        """
        try:
            payload = await self._get(f"/users/{quote(login, safe='')}")
        except GitHubNotFoundError:
            return None
        found = payload.get("id")
        return found if isinstance(found, int) else None

    async def user_login(self, account_id: int) -> str | None:
        """Which login this account answers to now, or None if GitHub has no such account.

        The inverse of the call above, and here for the reason that one stores an id at all: a
        login is a label GitHub reassigns, and the account behind it is the thing that lasts. A
        stored login whose owner has since renamed reads to GitHub as a stranger, or as nobody,
        and a caller holding the id can find out which.

        An int rather than a string, so there is nothing to escape. Every sibling here quotes
        what it interpolates and has a test standing behind it; this one cannot need one, which
        is worth saying rather than leaving as an omission a reader has to work out.

        Anonymous, like the call above, because the endpoint is public. That puts it on the
        hourly budget GitHub gives one address rather than the larger one it gives an
        installation, so a caller that would make it often should pass an owner and authenticate
        it instead.
        """
        try:
            payload = await self._get(f"/user/{account_id}")
        except GitHubNotFoundError:
            return None
        found = payload.get("login")
        return found if isinstance(found, str) else None

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None:
        """What happened between two commits, from the older one's point of view.

        The SHAs are quoted: they arrive off a webhook payload, and a path segment is where an
        unvalidated value would decide which endpoint gets called. Only the first page is read,
        so a push of more than 250 commits has its list cut while `total_commits` stays right,
        which is what the announcer subtracts from to report the overflow. A 404 is permanent.
        """
        path = f"/repos/{owner}/{name}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        try:
            payload = await self._get(path, owner)
        except GitHubNotFoundError:
            logger.info("GitHub has no compare for %s/%s %s...%s", owner, name, base, head)
            return None
        return mapping.commit_range(payload)

    async def commit_stats(self, owner: str, name: str, sha: str) -> CommitStats | None:
        """How much one commit changed.

        A call each, because the commit rows inside a compare carry no `stats` block, and the
        compare's own totals cover the whole range rather than any one commit.
        """
        try:
            payload = await self._get(f"/repos/{owner}/{name}/commits/{quote(sha, safe='')}", owner)
        except GitHubNotFoundError:
            logger.info("GitHub has no commit %s on %s/%s", sha, owner, name)
            return None
        return mapping.commit_stats(payload)

    async def list_check_runs(self, owner: str, name: str, sha: str) -> Sequence[CheckRun] | None:
        """Every CI job on one commit, across every checks provider the repository uses.

        The commit rather than the suite: a suite belongs to one app, so a repository running
        GitHub Actions beside anything else has several finishing at different moments.
        `filter=latest` is GitHub's default, stated because it is what makes a re-run replace its
        predecessor. The page body is an object with the list under `check_runs`.
        """
        found: list[CheckRun] = []
        path = f"{_repository(owner, name)}/commits/{quote(sha, safe='')}/check-runs"
        try:
            async for body in self.get_pages(
                path, owner=owner, filter="latest", per_page=LIST_PAGE_SIZE
            ):
                found.extend(mapping.check_runs(body))
        except GitHubNotFoundError:
            logger.info("GitHub has no commit %s on %s/%s, so no checks", sha, owner, name)
            return None
        return found

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        payload = await self._get(f"/repos/{owner}/{name}/pulls/{number}", owner)

        # The PR response embeds its own repository under base.repo, which saves a second call.
        base = payload.get("base")
        repo = mapping.repository(base.get("repo") if is_json_object(base) else None)
        if repo is None:
            repo = await self.get_repository(owner, name)

        snapshot = mapping.pull_request(payload, repo)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable pull request for {owner}/{name}#{number}"
            )
        return snapshot

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot:
        payload = await self._get(f"/repos/{owner}/{name}/issues/{number}", owner)

        # GitHub serves pull requests from this endpoint as well, so a number that turns out to
        # be a pull request is reported as no such issue rather than tracked as one.
        if mapping.is_pull_request(payload):
            raise GitHubNotFoundError(f"{owner}/{name}#{number} is a pull request, not an issue")

        # Unlike the pull request endpoint, this one carries no repository object, only a URL.
        repo = mapping.repository(payload.get("repository")) or await self.get_repository(
            owner, name
        )

        snapshot = mapping.issue(payload, repo)
        if snapshot is None:
            raise GitHubUnavailableError(
                f"GitHub returned an unusable issue for {owner}/{name}#{number}"
            )
        return snapshot

    async def list_open_pull_requests(
        self, repository: RepositorySnapshot
    ) -> Sequence[PullRequestSnapshot]:
        """Every open pull request, whole.

        The pulls endpoint rather than the issues one, which answers with pull requests in the
        issue shape: no requested reviewers, no teams, no repository on the base. Mirroring from
        those would open every thread saying nobody had been asked to review.
        """
        return await self._open_items(
            f"/repos/{repository.owner}/{repository.name}/pulls", repository, mapping.pull_request
        )

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]:
        """Every open issue, with the pull requests GitHub mixes in dropped."""
        return await self._open_items(
            f"/repos/{repository.owner}/{repository.name}/issues", repository, _an_issue
        )

    async def _open_items(
        self,
        path: str,
        repository: RepositorySnapshot,
        parse: Callable[[Any, RepositorySnapshot], _Item | None],
    ) -> list[_Item]:
        """Read a list endpoint whole, once each.

        Sorted by what moved most recently, because a run that reaches a cap should spend it on
        the items somebody is actually working on, and because that leaves the quietest ones in
        the tail that `MAX_PAGES` cuts off.

        Deduplicated here rather than by the caller. GitHub's own documentation says a list that
        is edited while it is being paged can hand the same row back on two pages, and the caller
        reads what is already mirrored once at the start, so a repeat would open two threads for
        one item and be counted twice on the way out.
        """
        found: dict[int, _Item] = {}
        async for body in self.get_pages(
            path,
            owner=repository.owner,
            state="open",
            per_page=LIST_PAGE_SIZE,
            sort="updated",
            direction="desc",
        ):
            for row in body if is_json_list(body) else []:
                item = parse(row, repository)
                if item is not None and item.github_object_id not in found:
                    found[item.github_object_id] = item
        return list(found.values())

    async def permission_for(self, owner: str, name: str, login: str) -> str:
        """What one GitHub account may do to one repository: admin, write, read or none.

        The login must be one GitHub vouched for a moment ago, not one out of `user_links`,
        which records only a claim somebody made about themselves. GitHub maps `maintain` onto
        `write` and `triage` onto `read`, so these four are the whole ladder. A 404 means not a
        collaborator rather than an error.
        """
        path = (
            f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/collaborators/{quote(login, safe='')}/permission"
        )
        try:
            payload = await self._get(path, owner)
        except GitHubNotFoundError:
            return "none"

        permission = payload.get("permission")
        return permission if isinstance(permission, str) else "none"

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None:
        """Say something on an item, as the App rather than as a person.

        The issues endpoint serves pull requests too, so one method covers both here and below.
        Nothing is read back: GitHub sends this straight back as an `issue_comment` delivery,
        and what recognises the echo is a marker in the body rather than the comment's id.
        """
        await self._send(
            "POST",
            f"{_repository(owner, name)}/issues/{number}/comments",
            owner,
            json={"body": body},
        )

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Put a label on an item.

        GitHub creates a label the repository does not have rather than refusing, which is what
        lets a server use the workflow without setting five labels up by hand first.
        """
        await self._send(
            "POST",
            f"{_repository(owner, name)}/issues/{number}/labels",
            owner,
            json={"labels": [label]},
        )

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Take a label off an item, treating one that is not there as done.

        Removals are computed from a snapshot read a moment earlier, so a 404 means the end
        state is already the wanted one.
        """
        path = f"{_repository(owner, name)}/issues/{number}/labels/{quote(label, safe='')}"
        with contextlib.suppress(GitHubNotFoundError):
            await self._send("DELETE", path, owner)

    async def request_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        """Ask the named accounts to review a pull request.

        Its own endpoint, because a reviewer is not an assignee. It answers 422 for an account
        that is not a collaborator, for the author, and for somebody already asked, which is why
        `_raise_for_status` tells a refusal from an outage.
        """
        await self._send(
            "POST",
            f"{_repository(owner, name)}/pulls/{number}/requested_reviewers",
            owner,
            json={"reviewers": list(logins)},
        )

    async def remove_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        """Withdraw a review request, treating one that was never made as done."""
        with contextlib.suppress(GitHubNotFoundError):
            await self._send(
                "DELETE",
                f"{_repository(owner, name)}/pulls/{number}/requested_reviewers",
                owner,
                json={"reviewers": list(logins)},
            )

    async def add_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        """Put the named accounts on an issue.

        GitHub does not refuse somebody who cannot be assigned: it drops them and answers 201 as
        though it had not. `can_be_assigned` is asked first for that reason.
        """
        await self._send(
            "POST",
            f"{_repository(owner, name)}/issues/{number}/assignees",
            owner,
            json={"assignees": list(logins)},
        )

    async def remove_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        """Take the named accounts off an issue, treating one not on it as done."""
        with contextlib.suppress(GitHubNotFoundError):
            await self._send(
                "DELETE",
                f"{_repository(owner, name)}/issues/{number}/assignees",
                owner,
                json={"assignees": list(logins)},
            )

    async def can_be_assigned(self, owner: str, name: str, login: str) -> bool:
        """Whether GitHub would actually put this account on an item in this repository.

        Asked because the assignee write is silent about it. 204 means yes and 404 means no, and
        both are answers rather than failures.
        """
        path = f"{_repository(owner, name)}/assignees/{quote(login, safe='')}"
        try:
            await self._send("GET", path, owner)
        except GitHubNotFoundError:
            return False
        return True

    async def list_labels(self, owner: str, name: str) -> Sequence[str]:
        """Every label this repository has, by name.

        Read so a typed label can be checked before `add_label` creates it: one typo would add a
        label to the repository for good, and nothing here can delete one. Paged, because a
        half-read list would refuse a label that exists.
        """
        found: list[str] = []
        async for body in self.get_pages(
            f"{_repository(owner, name)}/labels", owner=owner, per_page=LIST_PAGE_SIZE
        ):
            for row in body if is_json_list(body) else []:
                label = row.get("name") if is_json_object(row) else None
                if isinstance(label, str) and label:
                    found.append(label)
        return found

    async def list_reviews(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewSnapshot] | None:
        """Every review submitted on one pull request, oldest first as GitHub sends them.

        Read rather than assembled from the webhooks that arrive, because the two disagree: a
        dismissed review is the same row with a different state, and `dismissed` is not an action
        this bot subscribes to, so a tally kept here would go on counting an approval that had
        been taken back.

        Paged, because a pull request argued over for a week runs past one page and a half-read
        list is the shape that reports agreement nobody reached.

        A resolved repository rather than an owner and a name, because `mapping.review` wants one
        for the snapshots it builds and the caller already holds it, which is the reason
        `ListsOpenItems` takes one.
        """
        found: list[ReviewSnapshot] = []
        path = f"{_repository(repository.owner, repository.name)}/pulls/{number}/reviews"
        try:
            async for body in self.get_pages(path, owner=repository.owner, per_page=LIST_PAGE_SIZE):
                for row in body if is_json_list(body) else []:
                    review = mapping.review(row, repository, item_number=number)
                    if review is not None:
                        found.append(review)
        except GitHubNotFoundError:
            # A pull request that is gone is gone, so retrying the delivery sixteen times over two
            # hours would spend them all on the same answer. Distinct from the empty list, which
            # is a pull request nobody has reviewed yet.
            return None
        return found

    async def _send(self, method: str, path: str, owner: str = "", **kwargs: Any) -> None:
        """A write, whose answer is only ever whether it worked.

        Redirects are followed here rather than by the transport. GitHub answers 301 after a
        rename, and httpx re-issues a redirected POST as a bodyless GET, so a label write
        against a renamed repository fetched the label list, was answered 200, and wrote nothing
        that anything downstream could tell from success. A stale name is ordinary: nothing
        corrects `repositories.repo_name` until an item webhook arrives.
        """
        try:
            headers = await self._authorization(owner)
            response = await self._client.request(
                method, path, follow_redirects=False, headers=headers, **kwargs
            )
            for _ in range(MAX_WRITE_REDIRECTS):
                if not response.is_redirect:
                    break
                path = _redirect_target(response, path)
                response = await self._client.request(
                    method, path, follow_redirects=False, headers=headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        # A redirect still standing after that lands in the catch-all as "GitHub returned 301",
        # which is retryable and loud, and for a chain that never resolves is the right answer.
        _raise_for_status(response, path)

    async def get_pages(
        self, path: str, *, owner: str = "", **params: str | int
    ) -> AsyncIterator[object]:
        """Every page of a list endpoint, following GitHub's own Link header.

        The project endpoints paginate by cursor: there is no `page` parameter, and asking for
        page two by number silently returns the first page again, so a caller that counted pages
        would mirror every card twice. The cursor is opaque, so the header is followed as given.
        """
        url: str | None = path
        for _ in range(MAX_PAGES):
            if url is None:
                return
            try:
                response = await self._client.get(
                    url, params=params or None, headers=await self._authorization(owner)
                )
            except httpx.HTTPError as exc:
                raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

            _raise_for_status(response, path)
            try:
                yield response.json()
            except ValueError as exc:
                raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

            # The next URL carries the cursor already, so the original parameters must not be
            # sent again beside it.
            url = response.links.get("next", {}).get("url")
            params = {}

        # A Link header that points at itself, or a list that never ends, would otherwise keep
        # this reading for as long as the process lives. Bounded rather than trusted: the cursor
        # is opaque, so there is nothing to inspect to tell the two apart.
        #
        # Only when something was actually left. This used to hang off the loop's `else`, which
        # runs whenever the range is exhausted, so a list of exactly `MAX_PAGES` pages was read
        # whole and reported as cut short. A warning that fires when nothing is wrong is worse
        # than no warning: it teaches whoever reads the log to skip the line, and the one time
        # it means a board is being truncated looks exactly like the times it does not.
        if url is not None:
            logger.warning("stopped following pages of %s after %s of them", path, MAX_PAGES)

    async def get_json(self, path: str, *, owner: str = "", **params: str | int) -> object:
        """Whatever GitHub answers at a path, list or object alike.

        The project endpoints are parsed by a module that checks every field it touches, so
        this hands the body over as it came.
        """
        try:
            response = await self._client.get(
                path, params=params or None, headers=await self._authorization(owner)
            )
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        _raise_for_status(response, path)

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

    async def _authorization(self, owner: str) -> dict[str, str]:
        """The credential for calls about one account, or nothing at all.

        Nothing rather than an empty bearer: `Bearer ` is malformed and GitHub answers 401,
        where no header at all is anonymous and public endpoints answer. An empty owner means
        a call that is not about a repository, and those endpoints are public.
        """
        if self._tokens is None or not owner:
            return {}
        token = await self._tokens.token_for(owner)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str, owner: str = "") -> JsonObject:
        try:
            response = await self._client.get(path, headers=await self._authorization(owner))
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        _raise_for_status(response, path)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

        if not is_json_object(payload):
            raise GitHubUnavailableError(f"GitHub returned an unexpected body for {path}")
        return payload


def _repository(owner: str, name: str) -> str:
    """The path segment naming one repository, with both halves escaped.

    A repository name is GitHub's to shape, and a stray slash in one would read as another path
    segment and send the write somewhere else.
    """
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _headers() -> dict[str, str]:
    """The headers every request carries whoever it is about.

    `Authorization` is not among them: it depends on which account the request concerns, so
    `_authorization` below builds it per request.
    """
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "shannon-bot",
    }


def _redirect_target(response: httpx.Response, path: str) -> str:
    """Where a redirected write goes instead, if it can go anywhere at all.

    Following one by hand means deciding who the Authorization header is handed to. GitHub
    answers a rename with the same host and a new path, so anything else is refused.
    """
    location = response.headers.get("location")
    if not location:
        raise GitHubUnavailableError(f"GitHub redirected {path} without saying where")

    target = response.url.join(location)
    here = response.url
    if (target.scheme, target.host, target.port) != (here.scheme, here.host, here.port):
        raise GitHubUnavailableError(f"GitHub redirected {path} to another host, {target.host}")
    return str(target)


def _raise_for_status(response: httpx.Response, path: str) -> None:
    if response.is_success:
        return

    status = response.status_code
    if status == 404:
        raise GitHubNotFoundError(f"GitHub has nothing at {path}")
    if status == 429 or _is_rate_limited(response):
        raise GitHubRateLimitError("GitHub rate limit reached", retry_after=_retry_after(response))
    if status in {401, 403}:
        raise GitHubAuthError(f"GitHub refused the request for {path} ({status})")
    # Above the catch-all, because a 422 is the opposite of what the catch-all means. GitHub read
    # the request and declined it, so retrying sends the same refusal back, and it is nearly always
    # the caller's to fix: a reviewer who is not a collaborator, the item's own author, somebody
    # already asked. Under the catch-all all of those read as "GitHub could not be reached".
    if status == 422:
        raise GitHubRefusedError(_refusal(response, path))
    raise GitHubUnavailableError(f"GitHub returned {status} for {path}")


def _refusal(response: httpx.Response, path: str) -> str:
    """GitHub's own words for why it would not do something.

    Read rather than replaced: the reasons are specific, numerous and GitHub's to change. A body
    that is not the expected shape falls back to naming the path.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"GitHub refused the request for {path}"

    said = payload.get("message") if is_json_object(payload) else None
    return said if isinstance(said, str) and said else f"GitHub refused the request for {path}"


def _is_rate_limited(response: httpx.Response) -> bool:
    """Whether a 403 is GitHub asking for a wait rather than refusing outright.

    Two limits, answering differently. The primary one is the hourly budget and says so in the
    counter. The secondary one is about how fast requests arrive, spends no budget, and marks
    itself only by asking for a wait, with the counter beside it often nowhere near zero. So
    reading the counter alone misses the limit this bot actually trips, which is the poller
    moving cards: filed as a refusal it loses the wait, and carrying on at the ordinary
    interval is how GitHub's documentation says an integration gets banned. A 429 is already a
    rate limit whatever else it carries, and the caller handles it.
    """
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "retry-after" in response.headers


def _retry_after(response: httpx.Response) -> int | None:
    """Seconds to wait before trying again.

    The two headers are different kinds of number. `retry-after` is already a delay;
    `x-ratelimit-reset` is the epoch second the window reopens, so it is turned into a delay
    against GitHub's own `date` header, which is the clock it was measured on.
    """
    delay = response.headers.get("retry-after")
    if delay and delay.isdigit():
        return int(delay)

    reset = response.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        return max(0, int(reset) - _served_at(response))
    return None


def _served_at(response: httpx.Response) -> int:
    served = response.headers.get("date")
    if served:
        with contextlib.suppress(TypeError, ValueError):
            return int(parsedate_to_datetime(served).timestamp())
    return int(time.time())
