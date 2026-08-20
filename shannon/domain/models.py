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
        """Priority as GitHub has it, which is a label. Same rule for every kind of item."""
        return parse_priority(self.label_names)


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestSnapshot(ItemSnapshot):
    reviewers: tuple[Actor, ...] = ()
    # Teams asked for a review, carried apart from the people asked rather than among them.
    #
    # Apart all the way down, and each half of that was learned the hard way. They are told in
    # different words, because Discord writes a role mention differently from a person's and a
    # slug rendered as a login resolves to nobody. They are looked up in a different table,
    # because `/link` lets anybody bind a name to their own account without GitHub being asked,
    # so a slug looked up among logins is how somebody becomes the `security` team. And they are
    # closed by a different rule: a person's request ends when they submit a review, a team's
    # when GitHub drops it from `requested_teams`, which deletes the row.
    reviewer_teams: tuple[Actor, ...] = ()
    merged: bool = False

    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def display_state(self) -> str:
        """Open, closed, or merged. GitHub carries merging as a flag beside the state.

        Do not shorten `super(PullRequestSnapshot, self)` to a bare `super()`: the `slots=True`
        decorator rebuilds the class, and the bare form's closure cell still points at the one
        it replaced, which raises on Python before 3.14.
        """
        if self.merged:
            return "merged"
        return super(PullRequestSnapshot, self).display_state


@dataclass(frozen=True, slots=True, kw_only=True)
class IssueSnapshot(ItemSnapshot):
    closed_at: datetime | None = None

    object_type: ObjectType = field(default=ObjectType.ISSUE, init=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class TicketSnapshot(ItemSnapshot):
    """A draft item on a GitHub project board, which belongs to no repository of its own.

    The requirements give it a block of three lines against the eleven a pull request gets, and
    that is the shape of the thing rather than an omission: a draft has a title, a place on a
    board, and nothing else. No author, no assignees, no labels, no state, so the inherited
    fields keep their empty defaults and `priority` reads UNSET off an empty label list.

    `repository` is the one the guild registered, not one the ticket belongs to. It is carried
    because resolving a Discord guild goes through a repository row and there is no other route,
    which is a constraint of the schema rather than a claim about where the ticket lives.
    """

    # What the board says, as a column name rather than one of our own statuses. The mapping
    # between the two is a policy decision and is made where the policies are.
    column: str | None = None
    project_number: int | None = None

    object_type: ObjectType = field(default=ObjectType.TICKET, init=False)


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

    @property
    def note_key(self) -> str:
        return f"comment:{self.comment_id}"


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
    def note_key(self) -> str:
        return f"review:{self.review_id}"

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
    # What a renderer reads. Declared here so the seam that renders a note can say what it needs
    # instead of taking Any and hoping.
    body: str
    html_url: str
    created_at: datetime | None

    @property
    def note_key(self) -> str:
        """What identifies this note, kind included.

        The kind has to be in the key. GitHub numbers comments and reviews separately, so the
        two can collide, and a review that happened to share a number with a comment would
        otherwise be taken for one already posted and dropped.
        """
        ...


@runtime_checkable
class TrackedSnapshot(Protocol):
    """What the sync path needs from any GitHub object it mirrors.

    Pull requests and issues both satisfy this, which is what lets one sync service handle
    both. A third object type only has to satisfy it too, so this lists what the sync path reads
    and not what an implementation happens to store. `state` and `labels` are absent for that
    reason: nothing reads them, because everything goes through `display_state`, `label_names`
    and `priority`, which are what the two kinds of item disagree about.
    """

    repository: RepositorySnapshot
    github_object_id: int
    number: int
    title: str
    html_url: str
    author: Actor | None
    assignees: tuple[Actor, ...]
    updated_at: datetime | None
    action: str | None
    object_type: ObjectType
    closed: bool

    @property
    def label_names(self) -> tuple[str, ...]: ...

    @property
    def display_state(self) -> str: ...

    @property
    def priority(self) -> Priority: ...
