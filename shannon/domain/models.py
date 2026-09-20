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
    """A GitHub account referenced by a PR or issue."""

    login: str
    github_user_id: int | None = None
    # The picture GitHub shows for this account, where it sent one. Carried for the thumbnail on a
    # panel and read nowhere else, so an account without one costs a panel its picture and nothing
    # else. Issue #116.
    #
    # Accepted only as an `https://` URL. Discord fetches a thumbnail itself and answers the whole
    # message with a 400 if it cannot, so a malformed one here does not lose a picture, it loses
    # the block that carried it.
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    color: str | None = None


@dataclass(frozen=True, slots=True)
class LabelMove:
    """One label going on or coming off an item, which GitHub reports one at a time.

    Its own type rather than a pair of strings because both halves are needed together and
    neither means anything alone: the name says which label, and nothing else in the delivery
    says whether it arrived or left. Four labels applied at once are four of these, in four
    deliveries, because that is how GitHub sends them.

    What the two classifiers make of the name is carried rather than worked out again wherever
    it is needed. Two reasons. The renderer would otherwise have to reach into `github` to find
    out what a label means, which is a policy decision made in a module whose job is deciding
    what a reader sees. And a classification stored once cannot come out differently from the
    one the delivery was read with.

    Two fields rather than a `kind` beside them, because a kind and a level can disagree and
    these cannot: `priority is not UNSET` is the whole of the question "is this a priority
    label", answered by the same parser that answers it everywhere else, and a `kind` of
    PRIORITY beside a level of UNSET would be representable and mean nothing. A label cannot be
    both, which `test_the_two_groups_cannot_both_claim_a_label` pins rather than assumes.
    """

    name: str
    added: bool
    # UNSET where the name says nothing about priority, which is the ordinary case. Read off the
    # name whichever way the label is moving: `urgent` is a priority label coming off as much as
    # going on, and the line that says so has to know which group it belongs to either way.
    priority: Priority = Priority.UNSET
    # None where the name is not one of the five statuses. Exact spellings only, which is the
    # rule `status_of` already holds to and the reason it is asked rather than guessed at.
    status: Status | None = None


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    github_repo_id: int
    owner: str
    name: str
    html_url: str
    # Three states rather than two, and None is the useful one: it means GitHub did not say. A
    # webhook payload always carries the flag, but a body that has been trimmed or comes from a
    # cache may not, and reading a missing field as public would state something nobody checked.
    private: bool | None = None

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
    # What somebody wrote when they opened it. Empty where they wrote nothing, which GitHub
    # sends as a null rather than as an empty string, and which the block reads as a section to
    # leave out rather than as one to render blank.
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

    # Who this very event asked, as opposed to who is on the pull request. GitHub names them at
    # the top level of a `review_requested` payload and nowhere else, and it only sends one for a
    # party that was not already requested, which makes it the only thing that separates "asked
    # again" from "still asked" when the list either side is identical. Carried apart the way the
    # two lists are, because which of them was asked decides which role's row is reopened.
    person_asked_now: Actor | None = None
    team_asked_now: Actor | None = None

    merged: bool = False

    # The commit the pull request currently points at. Issue #112 needs it to tell a CI result
    # about this pull request from one about a commit it has since moved off: a new push cancels
    # the run before it, and that run completes as `cancelled` looking like a failure.
    #
    # Empty where it was not read, which is every snapshot built from the ISSUES shape of a pull
    # request. That shape carries no head at all, and a caller comparing against an empty string
    # is asking a question it has no answer to rather than being told the wrong one.
    head_sha: str = ""
    # Whether it is still being written. GitHub runs CI on a draft like any other pull request,
    # and ringing reviewers about work nobody has asked them to look at yet is the wrong end of
    # the feature.
    draft: bool = False

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
    board, and nothing else worth three more lines. No author, no assignees, no labels, no
    state, so the inherited fields keep their empty defaults and `priority` reads UNSET off an
    empty label list.

    A draft does have a description on GitHub's side, and this does not carry it. Reading one
    would mean a field on `BoardItem` and a second read in the poller, for a block the
    requirements fix at three lines.

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
    # GitHub marks a pull request inside a comment payload, so the kind is known and worth
    # carrying rather than being rediscovered downstream. Required, and above the fields that
    # have defaults so that it has to be: the one place a note's kind is used is the read that
    # finds its item, and a missing kind there does not fail, it matches nothing, which reads
    # as an item nobody tracks and drops the comment for good.
    object_type: ObjectType
    author: Actor | None = None
    created_at: datetime | None = None

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


@dataclass(frozen=True, slots=True)
class ReviewCommentSnapshot:
    """One inline comment left on a pull request's diff.

    Its own kind rather than a `CommentSnapshot` carrying a few more fields, because the two come
    off different events and GitHub numbers them separately. A review comment and an issue comment
    can share an id, and one key space would take the second for the first and drop it.

    Where the comment points is carried as GitHub reports it, and nothing here tries to improve on
    it: `line` is the end of the range as the branch stands now, `start_line` is set only on a
    multi-line comment, and `original_line` is where it was written, which is the only one left
    once the diff has moved under it.
    """

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

    # Only pull requests have review comments. Fixed rather than passed in, because the rebuild
    # that mends a deleted thread branches on this field and its other arm reads the pull request
    # as an issue. GitHub serves that happily, and it would open a second thread for the same item.
    object_type: ObjectType = field(default=ObjectType.PR, init=False)

    @property
    def note_key(self) -> str:
        """A key space of its own, for the reason the class docstring gives."""
        return f"review-comment:{self.comment_id}"


@dataclass(frozen=True, slots=True)
class CommitRef:
    """One commit as the compare endpoint describes it, which is everything but the numbers.

    Carried apart from `Commit` because this is what decides whether a commit is announced at all,
    and that decision is made before anything is spent reading it. A push that merges the default
    branch in is filtered down to nothing off this alone, at the cost of the one call that listed
    them.
    """

    sha: str
    message: str
    # The GitHub ACCOUNT, which GitHub resolves from the commit's email address and which is None
    # when no account holds it.
    #
    # Deliberately not the name written into the commit itself. That is free text set by
    # `git config user.name`, so anybody who can push to the branch could put a colleague's name
    # against their own work, and a thread is exactly where that would be believed.
    author: Actor | None
    # More than one parent is a merge. A merge is announced by nothing: the commits it brings in
    # are each judged on their own terms, and the merge itself says only that one branch caught up
    # with another.
    merge: bool


@dataclass(frozen=True, slots=True)
class CommitStats:
    """How much one commit changed."""

    additions: int
    deletions: int
    # Counted off the list of files rather than read from a field, because GitHub sends no such
    # field on a commit. It caps that list at three hundred entries, so a commit touching more
    # understates its file count while its additions and deletions stay exact. The two halves of
    # this are not equally trustworthy and only one of them can be wrong.
    changed_files: int


@dataclass(frozen=True, slots=True)
class CommitRange:
    """What one push did to a branch, as the compare endpoint answers it."""

    # GitHub's own word: "ahead", "behind", "diverged" or "identical". Kept as the word rather
    # than reduced to a flag, because two of the four mean the branch was rewritten and the
    # caller is the one that says which two.
    status: str
    commits: tuple[CommitRef, ...]
    # GitHub's count, which can exceed the list beside it: the compare endpoint stops listing at
    # two hundred and fifty commits. Kept so that a line saying how many were not announced is
    # right on a push that large, where counting the list would understate it.
    total: int


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit, as a line in a thread has to say it: who, what, and how much."""

    sha: str
    message: str
    author: Actor | None
    stats: CommitStats

    @property
    def note_key(self) -> str:
        """Keyed on the commit, which is the opposite of the two lines beside it in the thread.

        A label going on is a fact about one delivery, so the tag line keys on the delivery. A
        commit is a fact about a SHA, and one delivery carries several of them. Keying a push on
        its delivery would let a delivery that posted three commits and then failed turn the other
        two away for good on the retry, with the delivery reported handled.
        """
        return f"commit:{self.sha}"

    @property
    def title(self) -> str:
        """The subject line, which is git's own convention rather than anything invented here."""
        return self.message.split("\n", 1)[0].strip()

    @property
    def description(self) -> str:
        """Everything under the subject. Empty for a commit written as one line, which is most."""
        _, _, rest = self.message.partition("\n")
        return rest.strip()


# What GitHub calls a job that worked, and what it calls one that broke. Everything else it can
# say is neither: `skipped`, `cancelled`, `neutral`, `stale`, and a run carrying no conclusion at
# all. Issue #112 asked for two lists and gets three, because this repository's own `Publish` job
# comes back `skipped` on every pull request, and folding that in with the failures would report a
# broken build on every green one and ring the author instead of the reviewers.
#
# `stale` is in the third list rather than among the failures on purpose: it means GitHub gave up
# on the run, which says nothing about the code.
SUCCEEDED = frozenset({"success"})
BROKEN = frozenset({"failure", "timed_out", "action_required"})


@dataclass(frozen=True, slots=True)
class CheckRun:
    """One CI job on one commit, as the checks endpoint describes it."""

    check_run_id: int
    name: str
    # Whether GitHub has finished with this run. Kept as the word rather than a flag, so the
    # caller can test it against `completed` rather than against a list of the pending words it
    # happened to know about when it was written.
    status: str
    # GitHub's own word, kept as the word rather than reduced to a flag. There are eight of them
    # and which bucket each falls in is the two frozensets above, written down once.
    conclusion: str
    # The job's log page. The one thing somebody wants from a failure, which is why only failures
    # are rendered with it.
    html_url: str


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Every check on one commit, once they have all finished.

    The whole commit rather than one suite. A suite is per app, so a repository running GitHub
    Actions beside anything else has several, each completing separately, and reporting one of
    them would be reporting part of the answer.
    """

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
        """Everything that neither worked nor broke, which is mostly jobs that never ran."""
        return tuple(run for run in self.runs if run.conclusion not in SUCCEEDED | BROKEN)

    @property
    def total(self) -> int:
        return len(self.runs)

    @property
    def passed(self) -> bool:
        """Whether this is worth telling the reviewers about.

        Not `succeeded == total`. A job that did not run cannot have failed, and on a repository
        with a job that is always skipped that reading is never true, so the reviewers would never
        be told anything. One success is still required, or a suite where every job skipped would
        read as a pass.
        """
        return not self.broken and bool(self.succeeded)

    @property
    def worth_saying(self) -> bool:
        """Whether anything actually ran. Nothing did on a docs-only push through a path filter,
        and a message saying so is noise in a thread nobody asked to have narrated."""
        return bool(self.succeeded or self.broken)

    @property
    def note_key(self) -> str:
        """Keyed on the set of runs, which is what makes a re-run say so and a retry not.

        Two parts, and the second is the one that is not obvious. The largest id alone would be
        enough for one checks app: re-running rotates the ids, so the key moves and the new result
        is announced. It is a second app that breaks it. Run ids are handed out when a run is
        CREATED, so a provider whose runs were created earlier and finish later leaves the largest
        id exactly where it was, finds the claim taken, and its results are never announced at all.
        The count moves when the largest id does not.

        Both ends of the claim read the same set, so a retried delivery computes the same key and
        is turned away, which is the whole point of claiming.
        """
        return f"checks:{len(self.runs)}:{max((run.check_run_id for run in self.runs), default=0)}"


@runtime_checkable
class ItemNote(Protocol):
    """Something posted into a tracked item's thread that is not its metadata.

    Comments and reviews both satisfy this, which is what lets one mirror handle both.
    """

    repository: RepositorySnapshot
    item_number: int
    author: Actor | None
    object_type: ObjectType
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

    # Read-only, all of them, which is a statement about the implementations rather than a
    # precaution. Every snapshot in this project is a frozen dataclass and nothing writes through
    # this protocol. Declared as plain attributes they were writable, and a writable member is one
    # a frozen class cannot offer, so strictly nothing satisfied this at all. Nothing noticed
    # because nothing checked.
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
