from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shannon.domain.enums import ObjectType, Priority


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
        """MVP 1 does not read priority off PR labels; MVP 3 owns that."""
        return Priority.UNSET
