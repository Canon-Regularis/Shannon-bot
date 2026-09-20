from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from shannon.domain.enums import ObjectType, Priority, Status
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
    login: str
    github_user_id: int | None = None
    # Accepted only as an `https://` URL. Discord fetches the thumbnail itself and answers the
    # whole message with a 400 if it cannot, so a malformed one costs the panel the block that
    # carried it rather than just its picture.
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    color: str | None = None


@dataclass(frozen=True, slots=True)
class LabelMove:
    """One label going on or coming off, which GitHub reports one at a time.

    Four labels applied at once arrive as four deliveries. A label cannot be both a priority and a
    status, which `test_the_two_groups_cannot_both_claim_a_label` pins.
    """

    name: str
    added: bool
    # Read off the name whichever way the label is moving: `urgent` is a priority label coming
    # off as much as going on. UNSET where the name says nothing about priority.
    priority: Priority = Priority.UNSET
    # None unless the name is one of the five statuses, spelled exactly as `status_of` has them.
    status: Status | None = None


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    github_repo_id: int
    owner: str
    name: str
    html_url: str
    # None means GitHub did not say. A webhook payload always carries the flag, but a trimmed or
    # cached body may not, and reading a missing field as public would state something unchecked.
    private: bool | None = None

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True, slots=True, kw_only=True)
class ItemSnapshot:
    """What every GitHub object the bot mirrors has in common.

    Keyword-only so a subclass can add fields without minding where they land in the ordering.
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
    # Empty where they wrote nothing, which GitHub sends as a null rather than an empty string,
    # and which the block reads as a section to leave out rather than one to render blank.
    body: str = ""

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
        return parse_priority(self.label_names)


@dataclass(frozen=True, slots=True, kw_only=True)
class PullRequestSnapshot(ItemSnapshot):
    reviewers: tuple[Actor, ...] = ()
    # Kept apart from the people asked. Discord writes a role mention differently from a person's;
    # `/link` binds a login without GitHub being asked, so a slug looked up among logins is how
    # somebody becomes the `security` team; a team's request closes when GitHub drops the slug.
    reviewer_teams: tuple[Actor, ...] = ()

    # Who this event asked, as opposed to who is on the pull request. GitHub names them at the top
    # level of a `review_requested` payload and only for a party not already requested, which is
    # the only thing separating "asked again" from "still asked" when the lists either side match.
    person_asked_now: Actor | None = None
    team_asked_now: Actor | None = None

    merged: bool = False

    # Issue #112 needs it to tell a CI result about this pull request from one about a commit it
    # has moved off: a new push cancels the run before it, which completes as `cancelled` and
    # looks like a failure. Empty on snapshots built from the ISSUES shape, which carries no head.
    head_sha: str = ""
    # GitHub runs CI on a draft like any other pull request, so the checks path has to know.
    draft: bool = False

    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def display_state(self) -> str:
        """Open, closed, or merged; GitHub carries merging as a flag beside the state.

        Do not shorten to a bare `super()`: `slots=True` rebuilds the class and the bare form's
        closure cell still points at the one it replaced, which raises before Python 3.14.
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
    """A draft item on a GitHub project board, belonging to no repository of its own.

    Inherited author, assignee, label and state fields keep their empty defaults. `repository` is
    the one the guild registered, because resolving a Discord guild goes through a repository row.
    """

    # The board's own column name, not one of our statuses; the mapping lives with the policies.
    column: str | None = None
    project_number: int | None = None

    object_type: ObjectType = field(default=ObjectType.TICKET, init=False)


@dataclass(frozen=True, slots=True)
class CommentSnapshot:
    """A GitHub comment, and the number of the item it was left on.

    By number rather than id: GitHub reports a pull request's issue id in comment payloads,
    which never matches the pull request id stored against the tracked item."""

    repository: RepositorySnapshot
    item_number: int
    comment_id: int
    html_url: str
    body: str
    # Required, and above the fields with defaults so that it has to be: the read that finds a
    # note's item does not fail on a missing kind, it matches nothing, which reads as an item
    # nobody tracks and drops the comment for good. GitHub marks a pull request in the payload.
    object_type: ObjectType
    author: Actor | None = None
    created_at: datetime | None = None

    @property
    def note_key(self) -> str:
        return f"comment:{self.comment_id}"


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """A submitted pull request review.

    `state` is lowercased on the way in: webhooks send `approved`, the REST API `APPROVED`.
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


@dataclass(frozen=True, slots=True)
class ReviewCommentSnapshot:
    """One inline comment left on a pull request's diff, numbered separately from issue comments.

    The two can share an id and need separate key spaces. `line` is the end of the range as the
    branch stands now, `start_line` is set only on a multi-line comment, and `original_line` is
    where it was written, the only one left once the diff has moved under it."""

    repository: RepositorySnapshot
    item_number: int
    comment_id: int
    html_url: str
    body: str
    path: str
    line: int | None = None
    start_line: int | None = None
    original_line: int | None = None
    # Set on a reply into an existing thread and absent on the comment that opened it.
    in_reply_to_id: int | None = None
    author: Actor | None = None
    created_at: datetime | None = None

    # Fixed rather than passed in: the rebuild that mends a deleted thread branches on this field,
    # and its other arm reads the pull request as an issue, which GitHub serves happily and which
    # would open a second thread for the same item.
    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def note_key(self) -> str:
        return f"review-comment:{self.comment_id}"


@dataclass(frozen=True, slots=True)
class CommitRef:
    """One commit as the compare endpoint describes it, which is everything but the numbers.

    Separate from `Commit` because whether a commit is announced is decided off this alone,
    before anything is spent reading its stats."""

    sha: str
    message: str
    # The GitHub ACCOUNT, resolved from the commit's email address and None when no account holds
    # it. Deliberately not the name written into the commit: that is free text set by
    # `git config user.name`, so anybody who can push could put a colleague's name on their work.
    author: Actor | None
    # More than one parent. A merge itself is announced by nothing: the commits it brings in are
    # each judged on their own terms.
    merge: bool


@dataclass(frozen=True, slots=True)
class CommitStats:
    additions: int
    deletions: int
    # Counted off the list of files, because GitHub sends no such field on a commit. That list
    # caps at three hundred entries, so a larger commit understates its file count while its
    # additions and deletions stay exact.
    changed_files: int


@dataclass(frozen=True, slots=True)
class CommitRange:
    """What one push did to a branch, as the compare endpoint answers it."""

    # GitHub's own word: "ahead", "behind", "diverged" or "identical". Kept as the word because
    # two of the four mean the branch was rewritten and the caller is the one that says which two.
    status: str
    commits: tuple[CommitRef, ...]
    # GitHub's count, which can exceed the list beside it: the compare endpoint stops listing at
    # two hundred and fifty commits, and counting the list would understate a push that large.
    total: int


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    message: str
    author: Actor | None
    stats: CommitStats

    @property
    def note_key(self) -> str:
        """Keyed on the SHA rather than on the delivery that carried it.

        One delivery carries several commits, so keying on it would let a delivery that posted
        three and then failed turn the other two away for good on the retry.
        """
        return f"commit:{self.sha}"

    @property
    def title(self) -> str:
        return self.message.split("\n", 1)[0].strip()

    @property
    def description(self) -> str:
        _, _, rest = self.message.partition("\n")
        return rest.strip()


# Everything else GitHub can say is neither: `skipped`, `cancelled`, `neutral`, `stale`, and a run
# with no conclusion at all. Three buckets rather than two because this repository's own `Publish`
# job comes back `skipped` on every pull request, and `stale` means GitHub gave up, not the code.
SUCCEEDED = frozenset({"success"})
BROKEN = frozenset({"failure", "timed_out", "action_required"})


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One CI job on one commit, as the checks endpoint describes it."""

    check_run_id: int
    name: str
    # Whether GitHub has finished. Callers test against `completed`; the pending words are a list
    # GitHub can add to.
    status: str
    # GitHub's own word, one of eight; the two frozensets above say which bucket each falls in.
    conclusion: str
    # The job's log page, rendered only for failures.
    html_url: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Every check on one commit, once they have all finished.

    The whole commit rather than one suite: a suite is per app, so a repository running GitHub
    Actions beside anything else has several, each completing separately."""

    sha: str
    runs: tuple[CheckRun, ...]

    @property
    def succeeded(self) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.runs if run.conclusion in SUCCEEDED)

    @property
    def broken(self) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.runs if run.conclusion in BROKEN)

    @property
    def other(self) -> tuple[CheckRun, ...]:
        return tuple(run for run in self.runs if run.conclusion not in SUCCEEDED | BROKEN)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def passed(self) -> bool:
        """Whether this is worth telling the reviewers about.

        Not `succeeded == total`: a repository with an always-skipped job would never read as a
        pass. One success is still required, or a suite where every job skipped would pass."""
        return not self.broken and bool(self.succeeded)

    @property
    def worth_saying(self) -> bool:
        """Whether anything ran at all. Nothing does on a docs-only push through a path filter."""
        return bool(self.succeeded or self.broken)

    @property
    def note_key(self) -> str:
        """Keyed on the set of runs, so a re-run says so and a retry does not.

        The largest id alone is not enough: ids are handed out when a run is CREATED, so a second
        checks app whose runs were created earlier and finish later leaves the largest id where it
        was, finds the claim taken, and is never announced. The count moves when the id does not."""
        return f"checks:{len(self.runs)}:{max((run.check_run_id for run in self.runs), default=0)}"


@runtime_checkable
class ItemNote(Protocol):
    """Something posted into a tracked item's thread that is not its metadata."""

    repository: RepositorySnapshot
    item_number: int
    author: Actor | None
    object_type: ObjectType
    body: str
    html_url: str
    created_at: datetime | None

    @property
    def note_key(self) -> str:
        """What identifies this note, kind included.

        GitHub numbers comments and reviews separately, so two notes can share a number."""
        ...


@runtime_checkable
class TrackedSnapshot(Protocol):
    """What the sync path needs from any GitHub object it mirrors.

    `state` and `labels` are absent because nothing reads them: everything goes through
    `display_state`, `label_names` and `priority`, which are what the kinds of item disagree on."""

    # Properties rather than plain attributes: a plain attribute is writable, and a frozen
    # dataclass cannot offer a writable member, so nothing would satisfy this protocol at all.
    @property
    def repository(self) -> RepositorySnapshot: ...

    @property
    def github_object_id(self) -> int: ...

    @property
    def number(self) -> int: ...

    @property
    def title(self) -> str: ...

    @property
    def html_url(self) -> str: ...

    @property
    def author(self) -> Actor | None: ...

    @property
    def assignees(self) -> tuple[Actor, ...]: ...

    @property
    def updated_at(self) -> datetime | None: ...

    @property
    def action(self) -> str | None: ...

    @property
    def object_type(self) -> ObjectType: ...

    @property
    def closed(self) -> bool: ...

    @property
    def body(self) -> str: ...

    @property
    def label_names(self) -> tuple[str, ...]: ...

    @property
    def display_state(self) -> str: ...

    @property
    def priority(self) -> Priority: ...


# Owner, name, number: how every caller outside this package addresses an item on GitHub.
Fetcher = Callable[[str, str, int], Awaitable[TrackedSnapshot]]
