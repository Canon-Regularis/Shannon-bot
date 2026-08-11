from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from shannon.domain.enums import ObjectType, Priority
from shannon.domain.priority import parse_priority


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """Owner and name pulled out of a GitHub link, plus the object number if the link had one."""

    owner: str
    name: str
    number: int | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class Actor:
    """A GitHub account referenced by a PR or issue."""

    login: str
    github_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    color: str | None = None


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    github_repo_id: int
    owner: str
    name: str
    html_url: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True)
class PullRequestSnapshot:
    """Everything the sync path needs about a PR, independent of where it was read from.

    Both the REST client and the webhook parser produce this, so downstream code never
    branches on the source of the data.
    """

    repository: RepositorySnapshot
    github_object_id: int
    number: int
    title: str
    html_url: str
    state: str
    author: Actor | None = None
    assignees: tuple[Actor, ...] = ()
    reviewers: tuple[Actor, ...] = ()
    labels: tuple[Label, ...] = ()
    merged: bool = False
    updated_at: datetime | None = None
    action: str | None = None

    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(label.name for label in self.labels)

    @property
    def display_state(self) -> str:
        """Where the pull request stands: open, closed, or merged.

        GitHub only reports open or closed and carries merging as a separate flag, so the
        three-way answer is derived once here rather than in every caller.
        """
        if self.merged:
            return "merged"
        return (self.state or "open").lower()

    @property
    def priority(self) -> Priority:
        """MVP 2 does not read priority off pull request labels; MVP 3 owns that."""
        return Priority.UNSET


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """Everything the sync path needs about an issue, independent of where it was read from.

    The REST client and the webhook parser both produce this, the same arrangement the pull
    request side uses.
    """

    repository: RepositorySnapshot
    github_object_id: int
    number: int
    title: str
    html_url: str
    state: str
    author: Actor | None = None
    assignees: tuple[Actor, ...] = ()
    labels: tuple[Label, ...] = ()
    updated_at: datetime | None = None
    closed_at: datetime | None = None
    action: str | None = None

    object_type: ObjectType = field(default=ObjectType.ISSUE, init=False)

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(label.name for label in self.labels)

    @property
    def display_state(self) -> str:
        return (self.state or "open").lower()

    @property
    def closed(self) -> bool:
        return self.display_state == "closed"

    @property
    def priority(self) -> Priority:
        """Issues carry their priority as a GitHub label."""
        return parse_priority(self.label_names)


@dataclass(frozen=True, slots=True)
class CommentSnapshot:
    """A GitHub comment, and the number of the item it was left on.

    The item is identified by number rather than by id: GitHub reports a pull request's issue
    id in comment payloads, which never matches the pull request id stored against the tracked
    item, while the number matches for both kinds.
    """

    repository: RepositorySnapshot
    item_number: int
    comment_id: int
    html_url: str
    body: str
    author: Actor | None = None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """A submitted pull request review.

    `state` is lowercased on the way in: webhooks send `approved`, the REST API sends
    `APPROVED`, and nothing downstream should have to know that.
    """

    repository: RepositorySnapshot
    item_number: int
    review_id: int
    html_url: str
    body: str
    state: str
    author: Actor | None = None
    created_at: datetime | None = None

    @property
    def verdict(self) -> str:
        return (self.state or "").lower()


@runtime_checkable
class ItemNote(Protocol):
    """Something posted into a tracked item's thread that is not its metadata.

    Comments and reviews both satisfy this, which is what lets one mirror handle both.
    """

    repository: RepositorySnapshot
    item_number: int
    author: Actor | None


@runtime_checkable
class TrackedSnapshot(Protocol):
    """What the sync path needs from any GitHub object it mirrors.

    Pull requests and issues both satisfy this, which is what lets one sync service handle
    both. A third object type only has to satisfy it too.
    """

    repository: RepositorySnapshot
    github_object_id: int
    number: int
    title: str
    html_url: str
    state: str
    author: Actor | None
    assignees: tuple[Actor, ...]
    labels: tuple[Label, ...]
    updated_at: datetime | None
    action: str | None
    object_type: ObjectType

    @property
    def label_names(self) -> tuple[str, ...]: ...

    @property
    def display_state(self) -> str: ...

    @property
    def priority(self) -> Priority: ...
