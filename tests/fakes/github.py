from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from shannon.domain.models import (
    IssueSnapshot,
    Label,
    PullRequestSnapshot,
    RepositorySnapshot,
)
from shannon.github.errors import GitHubNotFoundError


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
        users: set[str] | None = None,
    ) -> None:
        # Every login exists unless a test says otherwise, because almost no test is about a
        # login that does not. `set()` is how a test says the account is not there.
        self.users = users
        self.user_calls: list[str] = []
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
        self.error: Exception | None = None
        # Raised by the label writes alone, leaving the reads working. `error` fails every
        # call including the read that comes first, which is no use for showing what a
        # refused WRITE leaves behind: nothing has happened yet when the read fails.
        self.write_error: Exception | None = None

    async def get_repository(self, owner: str, name: str) -> RepositorySnapshot:
        full_name = f"{owner}/{name}"
        self.repository_calls.append(full_name)
        if self.error is not None:
            raise self.error
        try:
            return self.repositories[full_name.lower()]
        except KeyError:
            raise GitHubNotFoundError(f"GitHub has nothing at /repos/{full_name}") from None

    async def user_exists(self, login: str) -> bool:
        self.user_calls.append(login)
        if self.error is not None:
            raise self.error
        return True if self.users is None else login.lower() in {u.lower() for u in self.users}

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

    def set_labels(self, key: tuple[str, int], names: list[str]) -> None:
        """Arrange the labels an item is already carrying, the way a repository would have them.

        Through this rather than by assigning to `labels` directly, or the snapshot handed back
        by the next read still carries whatever the fake was built with and the arrangement
        never reaches the code under test.
        """
        self.labels[key] = list(names)
        self._restate(key)

    async def get_json(self, path: str, **params: Any) -> Any:
        """Whatever this fake was told to answer with at a path, or an empty list.

        Here because the protocol declares it, which is the point of the conformance table: the
        wiring hands this same object to the project board reader, so a fake without these
        builds a container that dies on the first poll rather than failing at the seam.
        """
        self.json_calls.append((path, params))
        if self.error is not None:
            raise self.error
        return self.bodies.get(path, [])

    async def get_pages(self, path: str, **params: Any) -> AsyncIterator[Any]:
        self.json_calls.append((path, params))
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
