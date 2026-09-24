from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from typing import Any, TypeVar

from shannon.domain.models import (
    CheckRun,
    CommitRange,
    CommitStats,
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
    ReviewSnapshot,
)
from shannon.github.errors import GitHubNotFoundError

# The same shape the real client uses, so one helper answers for both stores.
_Item = TypeVar("_Item", PullRequestSnapshot, IssueSnapshot)


class FakeGitHubClient:
    """GitHubClient backed by dictionaries, for tests that must not touch the network.

    Every method of the Protocol, including `get_issue`, which was missing for long enough that
    nothing could drive `/issue` through this at all and so nothing ever did. A Protocol is
    structural and unchecked at runtime, so the gap was silent.

    The label writes are held rather than counted, because the workflow reads an item back and
    a fake that forgot what it was told would let a test pass on a change that never landed.
    """

    def __init__(
        self,
        *,
        repositories: dict[str, RepositorySnapshot] | None = None,
        pull_requests: dict[tuple[str, int], PullRequestSnapshot] | None = None,
        issues: dict[tuple[str, int], IssueSnapshot] | None = None,
        users: dict[str, int] | None = None,
        logins: dict[int, str] | None = None,
        compares: dict[tuple[str, str], CommitRange | None] | None = None,
        commits: dict[str, CommitStats | None] | None = None,
    ) -> None:
        # What a push did, keyed by the pair of SHAs asked about, and how much each commit
        # changed, keyed by its own. A key holding None is how a test says GitHub has collected
        # it: a missing key is a test that forgot to stock the fake, and the two want telling
        # apart. Stocked with nothing, both answer None, which is the quiet path.
        self.compares = compares or {}
        self.commits = commits or {}
        self.compare_calls: list[tuple[str, str, str]] = []
        # Which commits had their numbers read. The filter that drops merges and other people's
        # work runs before these calls, and counting the lines in a thread cannot show that: a
        # push that announces nothing looks identical whether it skipped the reads or made ten
        # of them and threw the answers away.
        self.stats_calls: list[tuple[str, str]] = []
        # Every login exists unless a test says otherwise, because almost no test is about a
        # login that does not. `{}` is how a test says the account is not there, and a mapping
        # rather than a set because what `/link` needs is the account's id, not a yes.
        self.users = users
        self.user_calls: list[str] = []
        # Which login each account id answers to now, for a caller following a rename. Derived
        # from `users` where a test stocked that, so the two directions cannot disagree about one
        # person. The default above cannot be inverted, because it makes an id out of a checksum
        # of the login and a checksum does not run backwards, so an id in neither map is an
        # account GitHub no longer has, which is the other thing this call can say.
        self.logins = logins or {found: name for name, found in (users or {}).items()}
        self.login_calls: list[int] = []
        self.repositories = repositories or {}
        self.pull_requests = pull_requests or {}
        self.issues = issues or {}
        self.repository_calls: list[str] = []
        # A pair of events for a test that needs to stop a caller here. The real client makes a
        # network round trip at this point, which is the window two overlapping commands
        # interleave in, and a test that waits out a guess at how long that takes is a test that
        # passes on a busy machine for the wrong reason.
        self.before_read: tuple[asyncio.Event, asyncio.Event] | None = None
        self.pull_request_calls: list[tuple[str, int]] = []
        self.issue_calls: list[tuple[str, int]] = []
        # Which lists were asked for, so a test can say a refresh narrowed to issues never went
        # near the pulls endpoint. Counting threads afterwards cannot show that: a repository
        # whose pull requests are all mirrored already looks the same either way.
        self.list_calls: list[tuple[str, str]] = []
        # Labels the fake has been told to write, keyed the same way the snapshots are, so a
        # read after a write sees what the write did.
        self.labels: dict[tuple[str, int], list[str]] = {
            key: [label.name for label in snapshot.labels]
            for store in (self.pull_requests, self.issues)
            for key, snapshot in store.items()
        }
        self.label_calls: list[tuple[str, tuple[str, int], str]] = []
        # Bodies for the untyped endpoints, keyed by path, and what was asked of them.
        self.bodies: dict[str, Any] = {}
        self.json_calls: list[tuple[str, dict[str, Any]]] = []
        # Which account each untyped read was authorised as. Separate from the calls above so the
        # existing assertions on paths and parameters do not have to change shape, and worth
        # recording at all because an empty owner is an anonymous request: on a private
        # repository that is the difference between reading it and being told it does not exist.
        self.json_owners: list[str] = []
        # What each login may do, for `/unregister`. Anything not named here is an admin.
        self.permissions: dict[str, str] = {}
        self.permission_calls: list[tuple[str, str]] = []
        # Logins a test has declared to be people rather than organisations, and every
        # account this was asked about. Only `/link_team` asks.
        self.personal_accounts: set[str] = set()
        self.organisation_calls: list[str] = []
        self.error: Exception | None = None
        # Raised by the label writes alone, leaving the reads working. `error` fails every
        # call including the read that comes first, which is no use for showing what a
        # refused WRITE leaves behind: nothing has happened yet when the read fails.
        self.write_error: Exception | None = None
        # Raised by the two calls a refusal leans on, each alone. `error` fails everything
        # including the reads that come first, which is no use for showing what happens when the
        # ONE extra question cannot be put: the command is meant to carry on without its answer.
        self.login_error: Exception | None = None
        self.permission_error: Exception | None = None
        # Who has been put on which item, and every call that tried. The calls are recorded before
        # any staged failure is raised, so a test can assert what was asked for as well as what
        # landed.
        self.reviewers: dict[tuple[str, int], list[str]] = {}
        self.assignees: dict[tuple[str, int], list[str]] = {}
        self.people_calls: list[tuple[str, tuple[str, int], tuple[str, ...]]] = []
        # Who GitHub would refuse to assign. Anything not named here can be assigned, because that
        # is the ordinary case and a fake that refused by default would make every test say so.
        self.unassignable: set[str] = set()
        # What labels the repository itself has, by `owner/name`. Empty by default and not
        # stocked with anything plausible, so a test of the label command that forgot to say
        # which labels exist fails loudly rather than passing on a guess.
        self.repo_labels: dict[str, list[str]] = {}
        self.label_list_calls: list[str] = []
        self.assignable_calls: list[tuple[str, str]] = []
        # Every comment written, as (owner/name, number, body). Issue #103.
        self.comments: list[tuple[str, int, str]] = []
        # Checks on a commit, keyed by sha. Issue #112. A key holding None means GitHub has
        # collected the commit, which is a different answer from a missing key: that one means
        # the test forgot to stock the fake, and it comes back empty so the test says so.
        self.check_runs: dict[str, Sequence[CheckRun] | None] = {}
        self.check_run_calls: list[tuple[str, str]] = []
        # Reviews on a pull request, keyed by (owner/name, number). Issue #155. A key holding
        # None is a pull request GitHub no longer has, which is a different answer from a missing
        # key: that one is a test that forgot to stock the fake, and it comes back empty.
        self.reviews: dict[tuple[str, int], Sequence[ReviewSnapshot] | None] = {}
        self.review_calls: list[tuple[str, int]] = []

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        full_name = f"{owner}/{name}"
        self.repository_calls.append(full_name)
        if self.error is not None:
            raise self.error
        try:
            return self.repositories[full_name.lower()]
        except KeyError:
            raise GitHubNotFoundError(f"GitHub has nothing at /repos/{full_name}") from None

    async def user_id(self, login: str) -> int | None:
        self.user_calls.append(login)
        if self.error is not None:
            raise self.error
        if self.users is None:
            # Anybody, and a stable id per login so two calls about one person agree. A checksum
            # rather than `hash`, which is salted per process: `resolve_many` drops a mention
            # when a stored id disagrees with the item's, so a colliding id here is a test that
            # flips between runs and cannot be reproduced, because the seed has moved on. Above
            # every id the fixtures hand out, so a login nobody thought about cannot land on one
            # somebody wrote down.
            return 1_000_000_000 + zlib.crc32(login.lower().encode())
        return {name.lower(): found for name, found in self.users.items()}.get(login.lower())

    async def user_login(self, account_id: int) -> str | None:
        self.login_calls.append(account_id)
        if self.error is not None:
            raise self.error
        if self.login_error is not None:
            raise self.login_error
        return self.logins.get(account_id)

    async def get_pull_request(self, owner: str, name: str, number: int) -> PullRequestSnapshot:
        key = (f"{owner}/{name}".lower(), number)
        self.pull_request_calls.append(key)
        await self._hold()
        if self.error is not None:
            raise self.error
        try:
            return self.pull_requests[key]
        except KeyError:
            raise GitHubNotFoundError(
                f"GitHub has nothing at /repos/{owner}/{name}/pulls/{number}"
            ) from None

    async def _hold(self) -> None:
        if self.before_read is None:
            return
        reached, release = self.before_read
        reached.set()
        await release.wait()

    async def get_issue(self, owner: str, name: str, number: int) -> IssueSnapshot:
        key = (f"{owner}/{name}".lower(), number)
        self.issue_calls.append(key)
        if self.error is not None:
            raise self.error
        try:
            return self.issues[key]
        except KeyError:
            raise GitHubNotFoundError(
                f"GitHub has nothing at /repos/{owner}/{name}/issues/{number}"
            ) from None

    async def compare_commits(
        self, owner: str, name: str, base: str, head: str
    ) -> CommitRange | None:
        self.compare_calls.append((f"{owner}/{name}".lower(), base, head))
        if self.error is not None:
            raise self.error
        return self.compares.get((base, head))

    async def commit_stats(self, owner: str, name: str, sha: str) -> CommitStats | None:
        self.stats_calls.append((f"{owner}/{name}".lower(), sha))
        if self.error is not None:
            raise self.error
        return self.commits.get(sha)

    async def list_open_pull_requests(
        self, repository: RepositorySnapshot
    ) -> Sequence[PullRequestSnapshot]:
        return self._open("pulls", self.pull_requests, repository)

    async def list_open_issues(self, repository: RepositorySnapshot) -> Sequence[IssueSnapshot]:
        return self._open("issues", self.issues, repository)

    def _open(
        self, kind: str, store: Mapping[tuple[str, int], _Item], repository: RepositorySnapshot
    ) -> list[_Item]:
        """The open half of whatever this fake was stocked with, for the named repository.

        Filtered on `closed` rather than returning everything, so a test can put a closed item in
        the store and say out loud that a refresh does not reach for it. Sorted by number, because
        the real client sorts by what moved last and a test asserting on the order of what was
        mirrored needs an order it can predict.
        """
        self.list_calls.append((kind, repository.full_name))
        if self.error is not None:
            raise self.error
        wanted = repository.full_name.lower()
        return [
            snapshot
            for (full_name, _), snapshot in sorted(store.items())
            if full_name == wanted and not snapshot.closed
        ]

    async def list_labels(self, owner: str, name: str) -> Sequence[str]:
        key = f"{owner}/{name}".lower()
        self.label_list_calls.append(key)
        if self.error is not None:
            raise self.error
        return list(self.repo_labels.get(key, []))

    async def list_check_runs(self, owner: str, name: str, sha: str) -> Sequence[CheckRun] | None:
        self.check_run_calls.append((f"{owner}/{name}".lower(), sha))
        if self.error is not None:
            raise self.error
        return self.check_runs.get(sha, [])

    async def list_reviews(
        self, repository: RepositorySnapshot, number: int
    ) -> Sequence[ReviewSnapshot] | None:
        key = (repository.full_name.lower(), number)
        self.review_calls.append(key)
        if self.error is not None:
            raise self.error
        return self.reviews.get(key, [])

    async def add_comment(self, owner: str, name: str, number: int, body: str) -> None:
        # Refused before it is recorded, unlike the label writes below. `comments` is read as
        # what GitHub HOLDS rather than what it was asked for, and a test about an outage means
        # to say nothing was published.
        if self.write_error is not None:
            raise self.write_error
        if self.error is not None:
            raise self.error
        self.comments.append((f"{owner}/{name}".lower(), number, body))

    async def add_label(self, owner: str, name: str, number: int, label: str) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.label_calls.append(("add", key, label))
        if self.write_error is not None:
            raise self.write_error
        if self.error is not None:
            raise self.error
        self.labels.setdefault(key, []).append(label)
        self._restate(key)

    async def remove_label(self, owner: str, name: str, number: int, label: str) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.label_calls.append(("remove", key, label))
        if self.write_error is not None:
            raise self.write_error
        if self.error is not None:
            raise self.error
        self.labels[key] = [
            held for held in self.labels.get(key, []) if held.casefold() != label.casefold()
        ]
        self._restate(key)

    async def request_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.people_calls.append(("request_reviewers", key, tuple(logins)))
        self._refuse_if_staged()
        self.reviewers.setdefault(key, []).extend(logins)

    async def remove_reviewers(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.people_calls.append(("remove_reviewers", key, tuple(logins)))
        self._refuse_if_staged()
        wanted = {login.casefold() for login in logins}
        self.reviewers[key] = [
            held for held in self.reviewers.get(key, []) if held.casefold() not in wanted
        ]

    async def add_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.people_calls.append(("add_assignees", key, tuple(logins)))
        self._refuse_if_staged()
        # Silently dropped, exactly as GitHub does it. A test that forgets the assignability
        # check should see the same nothing-happened a user would.
        self.assignees.setdefault(key, []).extend(
            login for login in logins if self._would_assign(login)
        )

    async def remove_assignees(
        self, owner: str, name: str, number: int, logins: Sequence[str]
    ) -> None:
        key = (f"{owner}/{name}".lower(), number)
        self.people_calls.append(("remove_assignees", key, tuple(logins)))
        self._refuse_if_staged()
        wanted = {login.casefold() for login in logins}
        self.assignees[key] = [
            held for held in self.assignees.get(key, []) if held.casefold() not in wanted
        ]

    async def can_be_assigned(self, owner: str, name: str, login: str) -> bool:
        self.assignable_calls.append((f"{owner}/{name}".lower(), login))
        self._refuse_if_staged()
        return self._would_assign(login)

    def _would_assign(self, login: str) -> bool:
        """Whether this fake's GitHub would take them, decided the way GitHub decides it.

        Off the permission, because an assignee needs write access or better. These two used to
        be unrelated pieces of state with opposite defaults, so no test could tell a refusal for
        no access apart from a refusal for any other reason, which is the confusion issue #133
        was reported as.

        `unassignable` stays as the override, because "has write access and GitHub refuses
        anyway" is a real answer the permission alone cannot express, and it is the one case a
        refusal has no good explanation for.
        """
        if login.casefold() in self.unassignable:
            return False
        return self.permissions.get(login.lower(), "admin") not in {"none", "read"}

    def _refuse_if_staged(self) -> None:
        if self.write_error is not None:
            raise self.write_error
        if self.error is not None:
            raise self.error

    def set_labels(self, key: tuple[str, int], names: list[str]) -> None:
        """Arrange the labels an item is already carrying, the way a repository would have them.

        Through this rather than by assigning to `labels` directly, or the snapshot handed back
        by the next read still carries whatever the fake was built with and the arrangement
        never reaches the code under test.
        """
        self.labels[key] = list(names)
        self._restate(key)

    async def is_organisation(self, owner: str) -> bool:
        """Whether an account is an organisation, out of a set.

        Answers True unless a test says otherwise. Teams are the only thing that asks, and a
        team mapping made against a personal account is refused - so a default of False
        would refuse every team test in the suite for a reason none of them is about.
        """
        self.organisation_calls.append(owner.lower())
        if self.error is not None:
            raise self.error
        return owner.lower() not in self.personal_accounts

    async def permission_for(self, owner: str, name: str, login: str) -> str:
        """What one account may do to one repository, out of a dictionary.

        Answers `admin` unless a test says otherwise, because almost no test here is about
        somebody who is not one, and `none` is what GitHub answers for an account with no
        relationship to the repository.
        """
        self.permission_calls.append((f"{owner}/{name}".lower(), login))
        if self.permission_error is not None:
            raise self.permission_error
        if self.error is not None:
            raise self.error
        return self.permissions.get(login.lower(), "admin")

    async def get_json(self, path: str, *, owner: str = "", **params: str | int) -> object:
        """Whatever this fake was told to answer with at a path, or an empty list.

        Here because the protocol declares it, which is the point of the conformance table: the
        wiring hands this same object to the project board reader, so a fake without these
        builds a container that dies on the first poll rather than failing at the seam.
        """
        self.json_calls.append((path, params))
        self.json_owners.append(owner)
        if self.error is not None:
            raise self.error
        return self.bodies.get(path, [])

    async def get_pages(
        self, path: str, *, owner: str = "", **params: str | int
    ) -> AsyncIterator[object]:
        self.json_calls.append((path, params))
        self.json_owners.append(owner)
        if self.error is not None:
            raise self.error
        yield self.bodies.get(path, [])

    def _restate(self, key: tuple[str, int]) -> None:
        """Put the labels back on the stored snapshot, so a later fetch agrees with the writes.

        Without this the fake would answer every read with the labels it was built with, and a
        test could set a status twice and see the second call believe the first never happened.
        """
        held = tuple(Label(name=name) for name in self.labels.get(key, []))
        for store in (self.pull_requests, self.issues):
            if key in store:
                store[key] = replace(store[key], labels=held)


class ClosingGitHub(FakeGitHubClient):
    """Records that Container.aclose reached it, which is how closing is observed.

    `raises` stages the case where the HTTP client throws on the way out and the database pool
    must still be released.
    """

    def __init__(self, *, raises: bool = False) -> None:
        super().__init__()
        self.raises = raises
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True
        if self.raises:
            raise RuntimeError("the HTTP pool had already gone")
