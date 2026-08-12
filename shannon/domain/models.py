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


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemSnapshot:
    """What every GitHub object the bot mirrors has in common.

    The REST client and the webhook parsers both produce these, so downstream code never
    branches on where the data came from. Keyword-only so that a subclass can add its own
    fields without having to care where they land in the ordering.
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
    action: str | None = None

    @property
    def label_names(self) -> tuple[str, ...]:
        return tuple(label.name for label in self.labels)

    @property
    def display_state(self) -> str:
        return (self.state or "open").lower()

    @property
    def closed(self) -> bool:
        """Anything that is not open. A merged pull request counts, because it is closed too."""
        return self.display_state != "open"

    @property
    def priority(self) -> Priority:
        """Where the item's priority comes from, when it has one at all."""
        return Priority.UNSET


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestSnapshot(ItemSnapshot):
    reviewers: tuple[Actor, ...] = ()
    merged: bool = False

    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def display_state(self) -> str:
        """Open, closed, or merged.

        GitHub reports only open or closed and carries merging as a separate flag, so the
        three-way answer is derived once here rather than in every caller.

        `super()` is spelled out rather than left bare. A `slots=True` dataclass is rebuilt
        into a new class by the decorator, and the bare form reads the class off a cell that
        still points at the one it replaced, which raises on Python before 3.14. Naming the
        class here resolves it at call time, so it finds the class that actually exists.
        """
        if self.merged:
            return "merged"
        return super(PullRequestSnapshot, self).display_state


@dataclass(frozen=True, slots=True, kw_only=True)
class IssueSnapshot(ItemSnapshot):
    closed_at: datetime | None = None

    object_type: ObjectType = field(default=ObjectType.ISSUE, init=False)

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
    # GitHub marks a pull request inside a comment payload, so the kind is known and worth
    # carrying rather than being rediscovered downstream.
    object_type: ObjectType | None = None


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

    # Only pull requests have reviews.
    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def verdict(self) -> str:
        return (self.state or "").lower()


@runtime_checkable
class ItemNote(Protocol):
    """Something posted into a tracked item's thread that is not its metadata.

    Comments and reviews both satisfy this, which is what lets one mirror handle both.
    `object_type` is None when the event does not say which kind of item it belongs to.
    """

    repository: RepositorySnapshot
    item_number: int
    author: Actor | None
    object_type: ObjectType | None


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
