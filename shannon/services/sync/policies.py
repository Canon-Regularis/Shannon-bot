from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from shannon.discord_bot import formatting
from shannon.domain.board import status_from_column
from shannon.domain.enums import ActorRole, ObjectType, Priority, Status
from shannon.domain.models import (
    Actor,
    IssueSnapshot,
    PullRequestSnapshot,
    TicketSnapshot,
    TrackedSnapshot,
)


class SyncPolicy(Protocol):
    """Everything that differs between the kinds of GitHub object being mirrored.

    The sync service holds the orchestration that is the same for all of them; a policy holds
    the handful of decisions that are not. Adding a third kind means adding a policy, not
    another copy of the orchestration.
    """

    object_type: ObjectType

    # Where this kind of item's threads go when nobody has mapped a channel for it. /register
    # only ever maps pull requests, so without a fallback an issue has nowhere to go until
    # somebody runs /set_channel, and nothing appears to be happening. None means no fallback:
    # if it is not mapped, it is not posted.
    channel_fallback: ObjectType | None

    # Whether this kind's DONE means somebody shut the thread. True only where the lock is taken
    # by a command and kept in the row, which makes the row the only place a replacement thread
    # can learn it should come back shut. False where DONE is derived from something outside
    # Discord and no lock was ever taken for it, because then shutting a replacement would
    # invent a lock the original never had.
    lock_lives_in_the_row: bool

    def render(
        self,
        snapshot: TrackedSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str: ...

    def assignments(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]: ...

    def asked_again(self, snapshot: TrackedSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Who this event has just asked for, as opposed to who is on the item.

        Separate from `assignments` because the two answer different questions. That one is a
        list to be matched; this one is an event, and an event is the only thing that can tell a
        request made again from a request never withdrawn.
        """
        ...

    def status_for(self, snapshot: TrackedSnapshot, current: Status) -> Status: ...

    def locked(self, snapshot: TrackedSnapshot) -> bool | None:
        """Whether the thread should be locked, or None to leave it as it is."""
        ...

    def thread_name(self, snapshot: TrackedSnapshot) -> str:
        """What the item's Discord thread is called.

        Here rather than in the renderer because it is the one piece of the thread that is not
        the metadata block, and because a ticket has no number to lead with while the other two
        are found in a channel list by theirs.
        """
        ...


class PullRequestPolicy:
    object_type = ObjectType.PR
    channel_fallback = None
    # `/set_done` is the only thing that locks one, and it writes the status to the row. No
    # payload can say a pull request is finished, so the row is all a replacement thread has.
    lock_lives_in_the_row = True

    def render(
        self,
        snapshot: PullRequestSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_pull_request(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: PullRequestSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
            ActorRole.REVIEWER: snapshot.reviewers,
            ActorRole.REVIEWER_TEAM: snapshot.reviewer_teams,
        }

    def asked_again(self, snapshot: PullRequestSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Whoever `review_requested` named at the top level, under the role they were asked as.

        Empty for every other action, because no other payload carries one.
        """
        return {
            ActorRole.REVIEWER: [snapshot.person_asked_now] if snapshot.person_asked_now else [],
            ActorRole.REVIEWER_TEAM: ([snapshot.team_asked_now] if snapshot.team_asked_now else []),
        }

    def status_for(self, snapshot: PullRequestSnapshot, current: Status) -> Status:
        """Closing a pull request does not move its workflow status; MVP 3 owns that."""
        return current

    def locked(self, snapshot: PullRequestSnapshot) -> bool | None:
        """Pull request threads are never locked automatically; MVP 3 owns that."""
        return None

    def thread_name(self, snapshot: PullRequestSnapshot) -> str:
        return formatting.thread_name(snapshot)


class IssuePolicy:
    object_type = ObjectType.ISSUE
    channel_fallback = ObjectType.PR
    # `locked` reads it straight off the payload, so a replacement is shut by the ordinary path
    # and has nothing to learn from the row.
    lock_lives_in_the_row = False

    def render(
        self,
        snapshot: IssueSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_issue(
            snapshot, status=status, priority=priority, mentions=mentions
        )

    def assignments(self, snapshot: IssueSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Issues have no reviewers, so that role is never written for them."""
        return {
            ActorRole.AUTHOR: [snapshot.author] if snapshot.author else [],
            ActorRole.ASSIGNEE: snapshot.assignees,
        }

    def asked_again(self, snapshot: IssueSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nothing. An issue has no reviewers, so nothing about one can be asked twice."""
        return {}

    def status_for(self, snapshot: IssueSnapshot, current: Status) -> Status:
        """A closed issue is done, and reopening one undoes that.

        Reopening only resets a status of DONE rather than forcing NOT_REVIEWED on every open
        issue, so that MVP 3's status commands are not overwritten on the next webhook.
        """
        if snapshot.closed:
            return Status.DONE
        if current is Status.DONE:
            return Status.NOT_REVIEWED
        return current

    def locked(self, snapshot: IssueSnapshot) -> bool | None:
        return snapshot.closed

    def thread_name(self, snapshot: IssueSnapshot) -> str:
        return formatting.thread_name(snapshot)


class TicketPolicy:
    """A draft item on a project board, which is a thing with a name and a column and no more.

    No channel fallback, unlike issues. An issue with nowhere to go is a mistake, because
    /register maps pull requests and forgetting /set_channel is easy; a board is something
    somebody chose to mirror, and putting draft items into the pull request channel uninvited
    would be a surprise rather than a kindness.
    """

    object_type = ObjectType.TICKET
    channel_fallback = None
    # A card in the Done column is DONE on the row, put there by the board rather than by
    # anybody, and its thread was never locked. See `locked` for why it must not be.
    lock_lives_in_the_row = False

    def render(
        self,
        snapshot: TicketSnapshot,
        *,
        status: Status,
        priority: Priority,
        mentions: Mapping[str, int],
    ) -> str:
        return formatting.format_ticket(snapshot, status=status)

    def assignments(self, snapshot: TicketSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nobody. A draft item carries no author, assignee or reviewer to record or to ping."""
        return {}

    def asked_again(self, snapshot: TicketSnapshot) -> Mapping[ActorRole, Sequence[Actor]]:
        """Nothing, for the same reason: there is nobody on a card to ask."""
        return {}

    def status_for(self, snapshot: TicketSnapshot, current: Status) -> Status:
        """The board is the source: its column is the status, and moving the card is the change.

        A column nobody has taught us leaves the status where it was. Falling back to a default
        would move real work backwards every time the board is read.
        """
        return status_from_column(snapshot.column) or current

    def locked(self, snapshot: TicketSnapshot) -> bool | None:
        """Left alone. A board column is not a closed state, and a ticket that moves back out of
        Done would be locked in a thread nobody could answer in."""
        return None

    def thread_name(self, snapshot: TicketSnapshot) -> str:
        """No number in front. A draft item has none, and the board is where it is found."""
        return snapshot.title.strip() or "Untitled ticket"


def channel_fallbacks() -> dict[ObjectType, ObjectType]:
    """Which kinds fall back to another kind's channel, read off the policies themselves.

    So that anything else needing the answer asks the policies rather than restating the rule.
    `/set_channel` needs it to say where the threads already open actually went, which for a
    server that has never mapped issues is the pull request channel and not nowhere.
    """
    return {
        policy.object_type: policy.channel_fallback
        for policy in (PullRequestPolicy(), IssuePolicy(), TicketPolicy())
        if policy.channel_fallback is not None
    }
