from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx

from shannon.domain.models import (
    CommitRange,
    CommitStats,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.github import mapping
from shannon.github.errors import (
    GitHubAuthError,
    GitHubNotFoundError,
    GitHubRateLimitError,
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
    """An issue row, or None for the pull requests GitHub mixes into the issues endpoint.

    Named rather than written inline, because the paging helper takes one parser and the pulls
    side passes `mapping.pull_request` straight in.
    """
    if mapping.is_pull_request(payload):
        return None
    return mapping.issue(payload, repository)


class SuppliesTokens(Protocol):
    """A bearer token for calls about one GitHub account, or the empty string for none.

    Declared here rather than beside the thing that implements it, because this is the module that
    depends on the shape. The implementation lives in `github/installations.py` and reaches the
    database; a client that imported it would drag the whole storage layer in behind an HTTP
    wrapper, and nothing in here should know that installations are stored at all.

    Empty rather than an exception, and a good deal rests on that. It is exactly the state this
    client already modelled for an unset token: no `Authorization` header at all, public endpoints
    answer, private ones report as missing. So a deployment with no App configured behaves as one
    with no token used to, rather than failing on the first command anybody runs.
    """

    async def token_for(self, owner: str) -> str: ...


class LooksUpRepository(Protocol):
    """Resolving a repository by owner and name.

    Split out because that is all the link commands need of GitHub directly. Fetching the item
    itself is a closure the wiring builds, so nothing has to hold a handle that can read every
    pull request in order to check one repository still exists.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...


class ListsOpenItems(LooksUpRepository, Protocol):
    """Every open pull request or issue on a repository, which is all `/refresh` needs.

    Its own protocol for the reason `LooksUpRepository` is: mirroring a backlog should not need a
    handle that can write a label. `get_repository` comes with it because a list may only be asked
    for against a repository the caller has already resolved, and that is the call that resolves
    one.

    The repository is passed in rather than an owner and a name. It makes that rule unforgeable,
    it means the current name is used rather than a stale one, and `list_open_issues` cannot work
    without it: GitHub's issue rows carry no repository object at all.
    """

    async def list_open_pull_requests(
        self, repository: RepositorySnapshot
    ) -> Sequence[PullRequestSnapshot]: ...

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]: ...


class LooksUpUsers(Protocol):
    """Asking who holds a GitHub login, which is all `/link` needs of GitHub.

    Its own protocol rather than the whole client, so binding a name to a Discord account cannot
    reach anything that reads a pull request or writes a label.
    """

    async def user_id(self, login: str) -> int | None: ...


class ReadsCommits(Protocol):
    """What a push did to a branch, which is all the commit announcer needs of GitHub.

    Its own protocol for the same reason as the two above. This one runs on every push to every
    open pull request, which makes it the busiest reader in the project, and a handle that could
    also write a label is a handle that could write one by accident on the noisiest path there is.

    Both answer None rather than raising when GitHub has nothing. A SHA that has been collected
    never comes back, so a retry would spend sixteen attempts over two hours to say the same
    thing, and the caller can carry on with the commits it can read.
    """

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None: ...

    async def commit_stats(self, owner: str, name: str, sha: str) -> CommitStats | None: ...


class GitHubClient(ListsOpenItems, LooksUpUsers, ReadsCommits, Protocol):
    """The GitHub calls the rest of the project is allowed to make.

    Commands and services depend on this rather than on httpx, so nothing outside this module
    knows GitHub is reached over HTTP.
    """

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot: ...

    async def user_id(self, login: str) -> int | None: ...

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

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None: ...

    # Untyped bodies, for the project endpoints, which answer with arrays and are parsed by a
    # module that checks every field it touches. Declared here because the wiring hands this
    # same object to the board reader, and a stand-in that satisfied the protocol without them
    # would build a container that fails on the first poll rather than at the seam.
    async def get_json(self, path: str, *, owner: str = "", **params: Any) -> Any: ...

    def get_pages(self, path: str, *, owner: str = "", **params: Any) -> AsyncIterator[Any]: ...


class HttpGitHubClient:
    def __init__(
        self,
        *,
        tokens: SuppliesTokens | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # A supplier rather than a token, because there is no longer one token. Each call carries
        # a credential minted for the account it is about, so the header cannot be baked into the
        # client the way it was: it is decided per request, from the owner the caller already had
        # in its hand.
        #
        # None means no App is configured, and every request then goes out unauthenticated. That
        # is deliberately the same behaviour an empty `SHANNON_GITHUB_TOKEN` used to produce.
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

        The endpoint is public, so this answers with no token set and answers the same for
        somebody who can only be seen through a private repository.

        The id rather than a yes, because a login is not an identity: GitHub frees one the moment
        it is renamed or deleted and lets anybody take it. Storing what was asked for alongside
        the name is what lets a mention built later be checked against the person somebody meant.

        Only "not there" is turned into an answer. Anything else GitHub says is a reason the
        question could not be put, and the caller has a person in front of it who can be told to
        try again, which is a better outcome than binding a name nothing will ever match.
        """
        try:
            payload = await self._get(f"/users/{quote(login, safe='')}")
        except GitHubNotFoundError:
            return None
        found = payload.get("id")
        return found if isinstance(found, int) else None

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None:
        """What happened between two commits, from the older one's point of view.

        The SHAs are quoted. They arrive off a webhook payload, and a path segment is the one
        place where a value nobody validated decides which endpoint gets called.

        Only the first page is read, so a push of more than 250 commits has its list cut while
        `total_commits` stays right. That is what the count is for: the announcer subtracts what
        it said from GitHub's own total, so the overflow is reported rather than lost.

        Gone means gone. A branch deleted between the push and this call, or a base rewritten out
        of existence, never comes back, and a retry only delays the deliveries behind it.
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

        A call each, because the commit rows inside a compare carry no `stats` block. The
        alternative is the compare's own totals, which cover the whole range against the merge
        base and would be attributed to whichever commit happened to be rendered.
        """
        try:
            payload = await self._get(f"/repos/{owner}/{name}/commits/{quote(sha, safe='')}", owner)
        except GitHubNotFoundError:
            logger.info("GitHub has no commit %s on %s/%s", sha, owner, name)
            return None
        return mapping.commit_stats(payload)

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        payload = await self._get(f"/repos/{owner}/{name}/pulls/{number}", owner)

        # The PR response embeds its own repository under base.repo, which saves a second call.
        base = payload.get("base") if isinstance(payload, dict) else None
        repo = mapping.repository(base.get("repo") if isinstance(base, dict) else None)
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

        The pulls endpoint rather than the issues one, which also answers with pull requests. A
        pull request row there is the issue shape: no requested reviewers, no requested teams and
        no repository on the base. Mirroring from those would open a thread saying nobody had been
        asked to review, on every pull request, and the only way back would be a second call per
        item, which is the cost this whole method exists to avoid.
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
            for row in body if isinstance(body, list) else []:
                item = parse(row, repository)
                if item is not None and item.github_object_id not in found:
                    found[item.github_object_id] = item
        return list(found.values())

    async def permission_for(self, owner: str, name: str, login: str) -> str:
        """What one GitHub account may do to one repository: admin, write, read or none.

        Read for `/unregister`, and the login handed in must be one GitHub itself vouched for a
        moment ago rather than one out of `user_links`: that table records a claim somebody made
        about themselves, so a check built on it proves nothing at all.

        GitHub maps `maintain` onto `write` and `triage` onto `read` before answering, so the four
        values here are the whole ladder.

        A 404 is "not a collaborator" rather than an error. GitHub answers it for an account it
        has never heard of and for one with no relationship to the repository, and both mean the
        same thing to the caller: this person may not do that.
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

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Put a label on an item.

        The issues endpoint serves pull requests too, so one method covers both. GitHub creates
        a label this repository does not have yet rather than refusing, which is what lets a
        server start using the workflow without setting five labels up by hand first.
        """
        await self._send(
            "POST",
            f"/repos/{owner}/{name}/issues/{number}/labels",
            owner,
            json={"labels": [label]},
        )

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Take a label off an item, treating one that is not there as done.

        Removals are computed from a snapshot read a moment earlier, and anything can have
        happened since. A 404 here means the end state is the wanted one, and failing the
        command over it would leave the caller retrying towards where they already are.
        """
        path = f"/repos/{owner}/{name}/issues/{number}/labels/{quote(label, safe='')}"
        with contextlib.suppress(GitHubNotFoundError):
            await self._send("DELETE", path, owner)

    async def _send(self, method: str, path: str, owner: str = "", **kwargs: Any) -> None:
        """A write, whose answer is only ever whether it worked.

        Redirects are followed here rather than by the transport, because httpx follows one the
        way the RFC allows and not the way a write needs. A 301 is what GitHub answers after a
        rename, and on a POST httpx re-issues it as a bodyless GET, so putting a label on a
        renamed repository fetched the label list, was answered 200, and wrote nothing. Nothing
        downstream can tell that from success: the command replies that it worked, the status
        goes into the row and into the thread, and no later delivery re-derives status from
        labels, so the item keeps the label it had. The DELETE beside it is not downgraded and
        does land, which leaves the item with its old status label stripped and no new one.

        The stale name is ordinary rather than rare. Nothing corrects `repositories.repo_name`
        until an item webhook arrives, and no `repository` event is registered at all.
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
        # which is retryable and loud. That is the answer the write path had before this, and
        # for a chain that never resolves it is still the right one.
        _raise_for_status(response, path)

    async def get_pages(self, path: str, *, owner: str = "", **params: Any) -> AsyncIterator[Any]:
        """Every page of a list endpoint, following GitHub's own Link header.

        The project endpoints paginate by cursor rather than by page number: there is no `page`
        parameter, and the cursor for the next page is only ever given in the Link header. Asking
        for page two by number is not an error, it is silently the first page again, so a caller
        that counted pages would read the same cards over and over and mirror each of them twice.

        Following the header rather than building the next URL, because the cursor is opaque and
        the shape of it is GitHub's business.
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

    async def get_json(self, path: str, *, owner: str = "", **params: Any) -> Any:
        """Whatever GitHub answers at a path, list or object alike.

        The typed readers above each know what they asked for and refuse anything else. The
        project endpoints answer with arrays and are parsed by a module that checks every field
        it touches, so this hands the body over as it came and leaves the judging to them.
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

        Nothing rather than an empty bearer: a header reading `Bearer ` is a malformed credential
        and GitHub answers 401 to it, where no header at all is an anonymous request that public
        endpoints answer. The second is what a deployment with no App configured wants.

        An empty owner means a call that is not about a repository - the user lookup behind
        `/link` is the only one - and those endpoints are public.
        """
        if self._tokens is None or not owner:
            return {}
        token = await self._tokens.token_for(owner)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _get(self, path: str, owner: str = "") -> dict[str, Any]:
        try:
            response = await self._client.get(path, headers=await self._authorization(owner))
        except httpx.HTTPError as exc:
            raise GitHubUnavailableError(f"Could not reach GitHub: {exc}") from exc

        _raise_for_status(response, path)

        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubUnavailableError(f"GitHub returned a non-JSON body for {path}") from exc

        if not isinstance(payload, dict):
            raise GitHubUnavailableError(f"GitHub returned an unexpected body for {path}")
        return payload


def _headers() -> dict[str, str]:
    """The headers every request carries whoever it is about.

    `Authorization` is deliberately not among them any more. It used to be, because there was one
    token for everything; now it depends on which account the request concerns, so it is built per
    request by `_authorization` below.
    """
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "shannon-bot",
    }


def _redirect_target(response: httpx.Response, path: str) -> str:
    """Where a redirected write goes instead, if it can go anywhere at all.

    Following one by hand means deciding for oneself who the Authorization header is handed to,
    which is the job httpx was doing. GitHub answers a rename with the same host and a new path,
    so anything else is refused rather than trusted with the token.
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
    raise GitHubUnavailableError(f"GitHub returned {status} for {path}")


def _is_rate_limited(response: httpx.Response) -> bool:
    """Whether a 403 is GitHub asking for a wait rather than refusing outright.

    GitHub has two limits and they answer differently. The primary one is the hourly budget, and
    a spent budget says so in the counter. The secondary one is about how fast requests arrive,
    does not spend the budget at all, and marks itself only by asking for a wait: the counter
    beside it is untouched and often nowhere near zero.

    Reading the counter alone therefore recognised the limit this bot will almost never reach and
    missed the one it actually trips. A write costs several times what a read does against the
    secondary allowance, and a board with a handful of cards moving does exactly that, so it is
    the poller that finds this limit. Filed as a refusal it lost the wait GitHub had asked for,
    the poller's one backoff could not fire, and it carried on at its ordinary interval, which
    GitHub's own documentation says is how an integration gets banned. Everybody running a
    command was meanwhile told to go and check a token that was perfectly healthy.

    A 429 is already a rate limit whatever else it carries, and is handled by the caller.
    """
    if response.status_code != 403:
        return False
    if response.headers.get("x-ratelimit-remaining") == "0":
        return True
    return "retry-after" in response.headers


def _retry_after(response: httpx.Response) -> int | None:
    """Seconds to wait before trying again.

    The two headers GitHub can answer with are not the same kind of number. `retry-after` is
    already a delay; `x-ratelimit-reset` is the epoch second the window reopens, so returning
    it unchanged would report a wait of about fifty-six years. It is turned into a delay
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
