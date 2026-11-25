"""The GitHub calls this project makes, and the narrow handles it makes them through.

Each protocol below names only what one caller needs, so a handle that can put a reviewer on a
pull request cannot also read a commit or write a label. They are declared here, beside the
implementation, rather than beside the services that consume them, which reach the database and
would drag storage in behind HTTP.

How a request reaches GitHub - credentials, pages, redirects, and how an answer is filed - is
`transport.py`, issue #271. What is here is what each endpoint means.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Sequence
from typing import Protocol, TypeVar
from urllib.parse import quote

import httpx

from shannon.domain.json import JsonObject, is_json_list, is_json_object
from shannon.domain.models import (
    CheckRun,
    CommitRange,
    CommitRef,
    CommitStats,
    IssueSnapshot,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewCommentSnapshot,
    ReviewSnapshot,
)
from shannon.github import mapping
from shannon.github.errors import GitHubNotFoundError, GitHubUnavailableError
from shannon.github.people import NO_ACCESS, Standing
from shannon.github.transport import DEFAULT_BASE_URL, GitHubTransport, SuppliesTokens

logger = logging.getLogger(__name__)

# GitHub's maximum, and the right end of the range to be at. A backlog is read whole, so round
# trips are the thing to spend the fewest of; there is no partial answer worth asking for.
LIST_PAGE_SIZE = 100

# Constrained rather than bound, so reading the pulls endpoint gives back pull requests and the
# issues endpoint gives back issues. One paging helper serves both and neither caller has to say
# which of the two it got.
_Item = TypeVar("_Item", PullRequestSnapshot, IssueSnapshot)


def _an_issue(payload: object, repository: RepositorySnapshot) -> IssueSnapshot | None:
    """An issue row, or None for the pull requests GitHub mixes into the issues endpoint."""
    if mapping.is_pull_request(payload):
        return None
    return mapping.issue(payload, repository)


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


class ReadsWhoWroteIt(Protocol):
    """The commits on one pull request, which is how a CI result finds who to ring.

    A review asking for changes reads them too, issue #263: the commits a file's history is kept
    to, and the authors the review names when it cannot name them file by file.

    Its own protocol rather than a third method on `ReadsCommits`: that one is about what a PUSH
    did to a branch and answers from a range of two SHAs, while this answers from a pull request
    number and is read for the people rather than the changes.
    """

    async def list_pull_request_commits(
        self, owner: str, name: str, number: int
    ) -> Sequence[CommitRef] | None: ...


class ReadsReviewComments(Protocol):
    """Every inline comment on one pull request, which is how a reply finds whoever it answers.

    Issue #231. A reply's webhook names the comment it was left under and nothing about who wrote
    that, or who else has answered it since, and nothing here keeps a note's author - so GitHub is
    asked, which also reaches the threads that were already open before anybody asked this.

    A review asking for changes reads them too, issue #263, to pick out its own and with them the
    files it is on: its webhook names none of them.

    None rather than raising when GitHub has nothing, for the reason `ReadsCommits` gives.
    """

    async def list_review_comments(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewCommentSnapshot] | None: ...


class ReadsFileHistory(Protocol):
    """Which commits touched one file, read back from one commit. Issue #263.

    How a review asking for changes finds who made each file it commented on: the pull request's
    own commits, kept to the ones in this list, are that file's authors on the pull request.

    None rather than raising when GitHub has nothing, for the reason `ReadsCommits` gives.
    """

    async def list_commits_on_path(
        self, owner: str, name: str, *, sha: str, file_path: str
    ) -> Sequence[CommitRef] | None: ...


class GitHubClient(
    ListsOpenItems,
    LooksUpUsers,
    ReadsCommits,
    ReadsFileHistory,
    ReadsReviewComments,
    ReadsWhoWroteIt,
    Protocol,
):
    """The GitHub calls the rest of the project is allowed to make.

    Commands and services depend on this rather than on httpx, so nothing outside this module
    knows GitHub is reached over HTTP.
    """

    async def user_login(self, account_id: int) -> str | None: ...

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot: ...

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot: ...

    async def permission_for(self, owner: str, name: str, login: str) -> str: ...

    async def collaborator_standing(self, owner: str, name: str, login: str) -> Standing: ...

    async def is_organisation(self, owner: str) -> bool: ...

    async def list_labels(self, owner: str, name: str) -> Sequence[str]: ...

    async def label_colours(self, owner: str, name: str) -> dict[str, str]: ...

    async def create_label(self, owner: str, name: str, label: str, colour: str) -> None: ...

    async def update_label(
        self, owner: str, name: str, label: str, *, new_name: str, colour: str
    ) -> None: ...

    # Declared here rather than through a role of their own, because their consumers declare
    # their own: the check announcer's `ReadsChecksAndItems` and the approval round-up's
    # `ReadsAPullRequestAndItsReviews`. Each answers None rather than raising when GitHub has
    # nothing, for the reason `ReadsCommits` gives.
    async def list_check_runs(
        self, owner: str, name: str, sha: str
    ) -> Sequence[CheckRun] | None: ...

    async def list_reviews(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewSnapshot] | None: ...

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


class HttpGitHubClient:
    """The typed GitHub operations, each one a path, a mapping and a policy for 404.

    Over a `GitHubTransport`, which the container shares with the board reader. Issue #271: the
    board reader used to be handed this whole client for its raw reads, which is why
    `GitHubClient` carried them and every service holding one could GET any path.

    `transport` is that shared one. Without it the client builds its own out of the rest, and
    closes it with itself; one handed in is the wiring's to close.
    """

    def __init__(
        self,
        *,
        tokens: SuppliesTokens | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 10.0,
        http_client: httpx.AsyncClient | None = None,
        transport: GitHubTransport | None = None,
    ) -> None:
        self._owns_transport = transport is None
        self._transport = transport or GitHubTransport(
            tokens=tokens, base_url=base_url, timeout=timeout, http_client=http_client
        )

    async def aclose(self) -> None:
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> HttpGitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        payload = await self._get(_repository(owner, name), owner)
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
        path = f"{_repository(owner, name)}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
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
        path = f"{_repository(owner, name)}/commits/{quote(sha, safe='')}"
        try:
            payload = await self._get(path, owner)
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
            async for body in self._transport.get_pages(
                path, owner=owner, filter="latest", per_page=LIST_PAGE_SIZE
            ):
                found.extend(mapping.check_runs(body))
        except GitHubNotFoundError:
            logger.info("GitHub has no commit %s on %s/%s, so no checks", sha, owner, name)
            return None
        return found

    async def list_pull_request_commits(
        self, owner: str, name: str, number: int
    ) -> Sequence[CommitRef] | None:
        """Every commit on one pull request, read for the accounts that WROTE them.

        A CI result rings whoever put the code there (#164), and `CommitRef.author` is the GitHub
        account GitHub itself resolved from the commit's email address - the only name on a
        commit that anybody else chose. Since issue #263 the SHAs are read too: a review asking
        for changes keeps a file's history to the pull request's own commits by them, and checks
        that the commit it was made on is still one of them. The message comes along because the
        row carries it, and nothing here reads it.

        Paged, for the reason `list_reviews` is paged: a branch argued over for a week runs past
        one page, and a half-read list here is the shape that silently leaves a contributor out -
        which is the whole of what this read exists to prevent. `compare_commits` takes the other
        bargain and documents it, because a push announcement that lists nine of ten commits is
        still a true announcement; a contributor who is not rung is not a smaller version of the
        feature, it is the bug.

        None for a pull request that is gone, which is not the empty list: that is a pull request
        whose commits GitHub could link to nobody, and the caller treats the two differently.
        """
        found: list[CommitRef] = []
        path = f"{_repository(owner, name)}/pulls/{number}/commits"
        try:
            async for body in self._transport.get_pages(path, owner=owner, per_page=LIST_PAGE_SIZE):
                for row in body if is_json_list(body) else []:
                    commit = mapping.commit_ref(row)
                    if commit is not None:
                        found.append(commit)
        except GitHubNotFoundError:
            logger.info("GitHub has no commits for %s/%s#%s", owner, name, number)
            return None
        return found

    async def list_commits_on_path(
        self, owner: str, name: str, *, sha: str, file_path: str
    ) -> Sequence[CommitRef] | None:
        """The commits that touched one file, newest first from `sha`. Issue #263.

        ONE page, on purpose, where `list_pull_request_commits` reads them all. History runs
        newest first from the commit a review was made on, so the pull request's own commits are
        the first ones listed; the caller only keeps those, and a page of a hundred holds them
        unless more than a hundred commits on the base branch touched the same file in between.
        Paging past that would cost a read per hundred commits of a file's whole history to find
        a name that is, at worst, missing from one line - and a review asking for changes reads
        this once per file it commented on.

        Sent through `read_json` with the query as a mapping rather than through `get_json` or
        `get_pages`. Both of those take the endpoint as `path` and the query as keywords, so a
        query parameter that is itself called `path` cannot be passed to either; httpx encodes it
        there, a space, a `#` and anything outside ASCII included.

        None for a 404, which is a commit or a repository GitHub does not have: final, for the
        reason `ReadsCommits` gives. A refusal or an outage is raised for the caller to judge.
        """
        endpoint = f"{_repository(owner, name)}/commits"
        try:
            body = await self._transport.read_json(
                endpoint,
                query={"sha": sha, "path": file_path, "per_page": LIST_PAGE_SIZE},
                owner=owner,
            )
        except GitHubNotFoundError:
            logger.info("GitHub has no history of %s at %s on %s/%s", file_path, sha, owner, name)
            return None
        parsed = (mapping.commit_ref(row) for row in (body if is_json_list(body) else []))
        return [commit for commit in parsed if commit is not None]

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        payload = await self._get(f"{_repository(owner, name)}/pulls/{number}", owner)

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
        payload = await self._get(f"{_repository(owner, name)}/issues/{number}", owner)

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
            f"{_repository(repository.owner, repository.name)}/pulls",
            repository,
            mapping.pull_request,
        )

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]:
        """Every open issue, with the pull requests GitHub mixes in dropped."""
        return await self._open_items(
            f"{_repository(repository.owner, repository.name)}/issues", repository, _an_issue
        )

    async def _open_items(
        self,
        path: str,
        repository: RepositorySnapshot,
        parse: Callable[[object, RepositorySnapshot], _Item | None],
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
        async for body in self._transport.get_pages(
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

        The permission half of `collaborator_standing`, for the callers that only ever restrict
        and so have no use for the account it names.
        """
        return (await self.collaborator_standing(owner, name, login)).permission

    async def collaborator_standing(self, owner: str, name: str, login: str) -> Standing:
        """What one account may do to one repository, and which account GitHub answered about.

        Issue #94. The same endpoint and the same installation token as `permission_for`, read
        once for both halves: GitHub names the collaborator it found in `user`, and that id is
        what tells a proved account from whoever holds its old login now. Not `user_id`, which
        would ask the same thing anonymously on the hourly budget one address gets.

        A 404 is no access and no account rather than an error. A body that leaves either half
        out answers none for that half: no permission refuses, and no account matches nobody.
        """
        path = f"{_repository(owner, name)}/collaborators/{quote(login, safe='')}/permission"
        try:
            payload = await self._get(path, owner)
        except GitHubNotFoundError:
            return Standing(NO_ACCESS, None)

        permission = payload.get("permission")
        user = payload.get("user")
        account_id = user.get("id") if is_json_object(user) else None
        return Standing(
            permission if isinstance(permission, str) else NO_ACCESS,
            account_id if isinstance(account_id, int) else None,
        )

    async def is_organisation(self, owner: str) -> bool:
        """Whether this account is an organisation rather than a person.

        `GET /users/{owner}` answers for an organisation as readily as for a person and
        says which in `type`, so one Metadata-only call settles it and no permission has
        to be added to the App - which matters, because granting one suspends event
        delivery to the installation until somebody accepts.

        Anything it cannot read reads as a person. That is the answer that refuses, and
        refusing on a blip is the right way round here: a team mapping made against a
        personal account never matches anything and is silent for ever, while a refusal
        is one command to run again.
        """
        try:
            payload = await self._get(f"/users/{quote(owner, safe='')}", owner)
        except GitHubNotFoundError:
            return False
        return payload.get("type") == "Organization"

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None:
        """Say something on an item, as the App rather than as a person.

        The issues endpoint serves pull requests too, so one method covers both here and below.
        Nothing is read back: GitHub sends this straight back as an `issue_comment` delivery,
        and what recognises the echo is a marker in the body rather than the comment's id.
        """
        await self._transport.send(
            "POST",
            f"{_repository(owner, name)}/issues/{number}/comments",
            owner,
            json={"body": body},
        )

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Put a label on an item.

        GitHub creates a label the repository does not have rather than refusing, so an ordinary
        label needs no setup. The workflow's own status and priority labels are made to exist in
        their colours first (`LabelDefinitions.ensure`, issue #246), because a label created here
        comes out grey.
        """
        await self._transport.send(
            "POST",
            f"{_repository(owner, name)}/issues/{number}/labels",
            owner,
            json={"labels": [label]},
        )

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None:
        """Take a label off an item, treating one that is not there as done.

        Removals are computed from a snapshot read a moment earlier, so a 404 means the label is
        not on the item any more - usually because somebody took it off, which is the wanted end
        state. Since issue #246 it can also mean the label was renamed in place under it, and is
        on the item still under its new name. For a priority `workflow._renamed_since` takes that
        one off too; a status label is left to the next move, which takes every other one off.
        """
        path = f"{_repository(owner, name)}/issues/{number}/labels/{quote(label, safe='')}"
        with contextlib.suppress(GitHubNotFoundError):
            await self._transport.send("DELETE", path, owner)

    async def request_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        """Ask the named accounts to review a pull request.

        Its own endpoint, because a reviewer is not an assignee. It answers 422 for an account
        that is not a collaborator, for the author, and for somebody already asked, which is why
        `_raise_for_status` tells a refusal from an outage.
        """
        await self._transport.send(
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
            await self._transport.send(
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
        await self._transport.send(
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
            await self._transport.send(
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
            await self._transport.send("GET", path, owner)
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
        async for body in self._transport.get_pages(
            f"{_repository(owner, name)}/labels", owner=owner, per_page=LIST_PAGE_SIZE
        ):
            for row in body if is_json_list(body) else []:
                label = row.get("name") if is_json_object(row) else None
                if isinstance(label, str) and label:
                    found.append(label)
        return found

    async def label_colours(self, owner: str, name: str) -> dict[str, str]:
        """Every label this repository has, with its colour as six hex digits. Issue #246.

        Its own read rather than a second answer from `list_labels`, because the one asker is the
        step that makes sure this bot's own labels look the way they should, and it wants the
        colour where every other caller wants the name alone.
        """
        found: dict[str, str] = {}
        async for body in self._transport.get_pages(
            f"{_repository(owner, name)}/labels", owner=owner, per_page=LIST_PAGE_SIZE
        ):
            for row in body if is_json_list(body) else []:
                if not is_json_object(row):
                    continue
                label, colour = row.get("name"), row.get("color")
                if isinstance(label, str) and label:
                    found[label] = colour.lower() if isinstance(colour, str) else ""
        return found

    async def create_label(self, owner: str, name: str, label: str, colour: str) -> None:
        """Add a label to the repository itself, in a colour. Issue #246.

        GitHub answers 422 for a name the repository already has, which arrives here as
        `GitHubRefusedError` and is the caller's to read as "it is there already".
        """
        await self._transport.send(
            "POST",
            f"{_repository(owner, name)}/labels",
            owner,
            json={"name": label, "color": colour},
        )

    async def update_label(
        self, owner: str, name: str, label: str, *, new_name: str, colour: str
    ) -> None:
        """Rename and recolour one of the repository's labels. Issue #246.

        A rename moves the label on every item carrying it at once, which is what lets an old
        label become a new one without touching a single issue.
        """
        await self._transport.send(
            "PATCH",
            f"{_repository(owner, name)}/labels/{quote(label, safe='')}",
            owner,
            json={"new_name": new_name, "color": colour},
        )

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
            async for body in self._transport.get_pages(
                path, owner=repository.owner, per_page=LIST_PAGE_SIZE
            ):
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

    async def list_review_comments(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewCommentSnapshot] | None:
        """Every inline comment on one pull request, oldest first as GitHub sends them. Issue #231.

        The whole list rather than one thread of it, because GitHub has no endpoint for a thread:
        a reply carries the id of the comment that opened its thread, and the caller picks that
        thread out of everything here by it.

        Paged, because a long review runs past one page and a half-read list is the shape in
        which somebody being answered goes unrung with nothing saying so.

        Each row is the same object a `pull_request_review_comment` delivery carries as its
        `comment`, so `mapping.review_comment` reads both and the two cannot drift apart.
        """
        found: list[ReviewCommentSnapshot] = []
        path = f"{_repository(repository.owner, repository.name)}/pulls/{number}/comments"
        try:
            async for body in self._transport.get_pages(
                path, owner=repository.owner, per_page=LIST_PAGE_SIZE
            ):
                for row in body if is_json_list(body) else []:
                    comment = mapping.review_comment(row, repository, item_number=number)
                    if comment is not None:
                        found.append(comment)
        except GitHubNotFoundError:
            # Final, for the reason `list_reviews` gives. Distinct from the empty list, which is a
            # pull request with no inline comment on it at all.
            return None
        return found

    async def _get(self, path: str, owner: str = "") -> JsonObject:
        """`get_json` for an endpoint that answers with one object, refusing any other shape."""
        payload = await self._transport.get_json(path, owner=owner)
        if not is_json_object(payload):
            raise GitHubUnavailableError(f"GitHub returned an unexpected body for {path}")
        return payload


def _repository(owner: str, name: str) -> str:
    """The path segment naming one repository, with both halves escaped.

    A repository name is GitHub's to shape, and a stray slash in one would read as another path
    segment and send the request somewhere else - a read as well as a write, issue #271: a read
    sent elsewhere answers about a repository nobody asked for.
    """
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
